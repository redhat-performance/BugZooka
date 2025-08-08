import argparse
import logging
import os
import io
import signal
import sys
import time

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from src.config import SLACK_BOT_TOKEN, SLACK_CHANNEL_ID, JEDI_BOT_SLACK_USER_ID, get_product_config
from src.constants import (
    MAX_CONTEXT_SIZE,
    MAX_PREVIEW_CONTENT,
    SLACK_POLL_INTERVAL,
)
from src.logging import configure_logging
from src.log_analyzer import (
    download_and_analyze_logs,
    filter_errors_with_llm,
    run_agent_analysis,
)
from src.inference import (
    InferenceAPIUnavailableError,
    AgentAnalysisLimitExceededError,
)

from src.utils import get_slack_message_blocks


class SlackMessageFetcher:
    """Continuously fetches new messages from a Slack channel and logs them."""

    def __init__(self, channel_id, logger, poll_interval=600):
        """Initialize Slack client and channel details."""
        self.slack_bot_token = SLACK_BOT_TOKEN
        self.channel_id = channel_id
        self.logger = logger
        self.poll_interval = poll_interval  # How often to fetch messages
        self.last_seen_timestamp = None  # Track the latest message timestamp

        if not self.slack_bot_token:
            self.logger.error("Missing SLACK_BOT_TOKEN environment variable.")
            sys.exit(1)

        self.client = WebClient(token=self.slack_bot_token)
        self.running = True  # Control flag for loop

        # Handle SIGINT (Ctrl+C) for graceful exit
        signal.signal(signal.SIGINT, self.shutdown)

    def _filter_new_messages(self, messages):
        """Filter messages to only include new ones that haven't been processed."""
        new_messages = []
        for msg in reversed(messages):  # Oldest first
            ts = msg.get("ts")  # Message timestamp
            self.logger.debug(f"Checking message with timestamp: {ts}")

            replies = self.client.conversations_replies(channel=self.channel_id, ts=ts)
            messages_in_thread = replies.get("messages", [])
            bot_replied = any(
                reply.get("user") == JEDI_BOT_SLACK_USER_ID for reply in messages_in_thread[1:]  # skip the parent msg at index 0
            )
            if self.last_seen_timestamp is None or float(ts) > float(self.last_seen_timestamp):
                if bot_replied:
                    self.logger.debug(f"Skipping message with timestamp {ts} due to bot replied")
                else:
                    new_messages.append(msg)
            else:
                self.logger.debug(
                    f"Skipping message with timestamp {ts} due to timestamp filter"
                    )
        return new_messages

    def _send_error_logs_preview(self, errors_list, categorization_message, max_ts, is_install_issue=False):
        """Send error logs preview to Slack (either as message or file)."""
        errors_log_preview = "\n".join(errors_list or [])[:MAX_PREVIEW_CONTENT]
        errors_list_string = "\n".join(errors_list or [])[:MAX_CONTEXT_SIZE]

        if len(errors_list_string) > MAX_PREVIEW_CONTENT:
            preview_message = (
                f":checking: *Error Logs Preview ({categorization_message})*\n"
                "Here are the first few lines of the error log:\n"
                f"```{errors_log_preview.strip()}```\n"
                "_(Log preview truncated. Full log attached below.)_"
            )
            self.logger.info("📤 Uploading full error log with preview message")
            log_bytes = io.BytesIO(errors_list_string.strip().encode("utf-8"))
            self.client.files_upload_v2(
                channel=self.channel_id,
                file=log_bytes,
                filename="full_errors.log",
                title="Full Error Log",
                thread_ts=max_ts,
                initial_comment=preview_message,
            )
        else:
            self.logger.info("📤 Trying to just send the preview message")
            message_block = get_slack_message_blocks(
                markdown_header=f":checking: *Error Logs Preview ({categorization_message})*\n",
                preformatted_text=f"{errors_log_preview.strip()}",
            )
            self.client.chat_postMessage(
                channel=self.channel_id,
                text="Error Logs Preview",
                blocks=message_block,
                thread_ts=max_ts,
            )

        if is_install_issue:
            retrigger_message = (
                "This appears to be an installation or maintenance issue. "
                "Please re-trigger the run."
            )
            message_block = get_slack_message_blocks(
                markdown_header=":repeat: *Re-trigger Suggested*\n",
                preformatted_text=retrigger_message,
            )
            self.client.chat_postMessage(
                channel=self.channel_id,
                text="Re-trigger Suggested",
                blocks=message_block,
                thread_ts=max_ts,
            )

    def _send_analysis_result(self, response, max_ts):
        """Send the final analysis result to Slack."""
        message_block = get_slack_message_blocks(
            markdown_header=":fast_forward: *Implications to understand (AI generated) *\n",
            preformatted_text=response,
        )
        self.logger.info("Posting analysis summary to Slack")
        self.client.chat_postMessage(
            channel=self.channel_id,
            text="Implications summary",
            blocks=message_block,
            thread_ts=max_ts,
        )

    def _send_analysis_unavailable_message(self, max_ts):
        """Send message when analysis is unavailable due to inference API issues."""
        fallback_message = (
            "The inference API is currently unavailable. "
            "Raw error logs have been provided above for manual review."
        )
        message_block = get_slack_message_blocks(
            markdown_header=":warning: *Analysis Unavailable*\n",
            preformatted_text=fallback_message,
        )
        self.client.chat_postMessage(
            channel=self.channel_id,
            text="Analysis Unavailable",
            blocks=message_block,
            thread_ts=max_ts,
        )

    def _process_message(self, msg, product, ci_system, product_config, enable_inference):
        """Process a single message through the complete pipeline."""
        user = msg.get("user", "Unknown")
        text = msg.get("text", "No text available")
        ts = msg.get("ts")

        self.logger.info(f"📩 New message from {user}: {text} at ts {ts}")

        if "failure" not in text.lower():
            self.logger.info("Not a failure job, skipping")
            return ts

        # Extract and download logs
        errors_list, categorization_message, requires_llm, is_install_issue = download_and_analyze_logs(text, ci_system)
        if errors_list is None:
            return ts

        # Send error logs preview first
        self._send_error_logs_preview(errors_list, categorization_message, ts, is_install_issue)

        if is_install_issue or not enable_inference:
            return ts

        try:
            # Process with LLM
            error_summary = filter_errors_with_llm(
                errors_list, requires_llm, product_config
            )

            # Run agent analysis
            analysis_response = run_agent_analysis(
                error_summary, product, product_config
            )

            # Send final analysis
            self._send_analysis_result(analysis_response, ts)

        except InferenceAPIUnavailableError as e:
            self.logger.warning(
                "Skipping LLM analysis due to inference API unavailability: %s", e
            )
            self._send_analysis_unavailable_message(ts)
        except AgentAnalysisLimitExceededError as e:
            self.logger.warning(
                "Skipping agent analysis due to iteration/time limits: %s", e
            )
            self._send_analysis_unavailable_message(ts)

        return ts

    def fetch_messages(self, **kwargs):
        """Fetches only the latest messages from the Slack channel."""
        try:
            product = kwargs["product"]
            ci_system = kwargs["ci"]
            product_config = kwargs["product_config"]
            enable_inference = kwargs["enable_inference"]

            params = {"channel": self.channel_id, "limit": 1}
            if self.last_seen_timestamp:
                params["oldest"] = self.last_seen_timestamp

            response = self.client.conversations_history(**params)
            messages = response.get("messages", [])

            if not messages:
                self.logger.info("⏳ No new messages.")
                return

            # Filter to get only new messages
            new_messages = self._filter_new_messages(messages)

            if not new_messages:
                self.logger.info("⏳ No new messages.")
                return

            max_ts = self.last_seen_timestamp or "0"

            try:
                for msg in new_messages:
                    ts = msg.get("ts")

                    if ts and float(ts) > float(max_ts):
                        max_ts = ts

                    processed_ts = self._process_message(
                        msg, product, ci_system, product_config, enable_inference
                    )

                    if processed_ts and float(processed_ts) > float(self.last_seen_timestamp or 0):
                        self.logger.info(
                            f"Updating last_seen_timestamp from {self.last_seen_timestamp} to {processed_ts}"
                        )
                        self.last_seen_timestamp = processed_ts

            except Exception as e:
                self.logger.error(
                    f"Failure in execution. Making sure fallback is applied: {e}"
                )
                self.logger.info(
                    f"Updating last_seen_timestamp from {self.last_seen_timestamp} to {max_ts}"
                )
                self.last_seen_timestamp = max_ts

        except SlackApiError as e:
            self.logger.error(f"❌ Slack API Error: {e.response['error']}")
        except Exception as e:
            self.logger.error(f"⚠️ Unexpected Error: {str(e)}")

    def run(self, **kwargs):
        """
        Continuously fetch only new messages every X seconds until interrupted.

        :param kwargs: arguments to run the application.
        """
        self.logger.info(
            f"🚀 Starting Slack Message Fetcher for Channel: {self.channel_id}"
        )
        try:
            while self.running:
                self.fetch_messages(**kwargs)
                time.sleep(self.poll_interval)  # Wait before next fetch
        except Exception as e:
            self.logger.error(f"Unexpected failure: {str(e)}")
        finally:
            self.logger.info("👋 Shutting down gracefully.")

    def shutdown(self):
        """Handles graceful shutdown on user interruption."""
        self.logger.info("🛑 Received exit signal. Stopping message fetcher...")
        self.running = False
        sys.exit(0)


def str_to_bool(value):
    """Convert string ENV value to bool."""
    return str(value).lower() == "true"


# export PYTHONPATH=$(pwd)/src:$PYTHONPATH
if __name__ == "__main__":
    VALID_LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    parser = argparse.ArgumentParser(description="Slack Log Analyzer Bot")

    parser.add_argument(
        "--product",
        type=str,
        default=os.environ.get("PRODUCT"),
        help="Product type (e.g., openshift, ansible)",
    )
    parser.add_argument(
        "--ci", type=str, default=os.environ.get("CI"), help="CI system name"
    )
    parser.add_argument(
        "--log-level",
        type=str,
        choices=VALID_LOG_LEVELS,
        default=os.environ.get("LOG_LEVEL", "INFO").upper(),
        help="Logging level (e.g., DEBUG, INFO, WARNING, ERROR, CRITICAL)."
        " Can also be set via LOG_LEVEL env var",
    )
    parser.add_argument(
        "--enable-inference",
        action="store_true",
        default=str_to_bool(os.environ.get("ENABLE_INFERENCE", "false")),
        help="Enable inference mode. Can also be set via ENABLE_INFERENCE env var (true/false).",
    )

    args = parser.parse_args()
    configure_logging(args.log_level)
    logger = logging.getLogger(__name__)
    missing_args = []
    if not args.product:
        missing_args.append("product or PRODUCT")
    if not args.ci:
        missing_args.append("ci or CI")
    if missing_args:
        logger.error("Missing required arguments or env vars: {%s}", missing_args)
        sys.exit(1)

    kwargs = {
        "product": args.product.upper(),
        "ci": args.ci.upper(),
        "product_config": get_product_config(product_name=args.product.upper()),
        "enable_inference": args.enable_inference,
    }

    fetcher = SlackMessageFetcher(
        channel_id=SLACK_CHANNEL_ID, logger=logger, poll_interval=SLACK_POLL_INTERVAL
    )
    fetcher.run(**kwargs)
