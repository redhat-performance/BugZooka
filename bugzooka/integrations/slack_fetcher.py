import io
import signal
import sys
import time
import re

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from bugzooka.core.config import (
    SLACK_BOT_TOKEN,
    JEDI_BOT_SLACK_USER_ID,
    SUMMARY_LOOKBACK_SECONDS,
)
from bugzooka.core.constants import (
    MAX_CONTEXT_SIZE,
    MAX_PREVIEW_CONTENT,
)
from bugzooka.analysis.log_analyzer import (
    download_and_analyze_logs,
    filter_errors_with_llm,
    run_agent_analysis,
)
from bugzooka.analysis.log_summarizer import (
    classify_failure_type,
    render_failure_breakdown,
)
from bugzooka.integrations.inference import (
    InferenceAPIUnavailableError,
    AgentAnalysisLimitExceededError,
)
from typing import Dict, Tuple, Optional, List


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

    def _get_slack_message_blocks(
        self, markdown_header, content_text, use_markdown=False
    ):
        """
        Prepares a slack message building blocks

        :param markdown_header: markdown header to be displayed
        :param content_text: text message content (preformatted or markdown)
        :param use_markdown: if True, render content as markdown; if False, use preformatted text
        :return: a sanitized version of text blocks
        """
        header_block = {
            "type": "section",
            "text": {"type": "mrkdwn", "text": markdown_header},
        }

        if use_markdown:
            content_block = {
                "type": "markdown",
                "text": content_text.strip(),
            }
        else:
            content_block = {
                "type": "rich_text",
                "block_id": "error_logs_block",
                "elements": [
                    {
                        "type": "rich_text_preformatted",
                        "elements": [{"type": "text", "text": content_text.strip()}],
                        "border": 0,
                    }
                ],
            }

        return [header_block, content_block]

    def _filter_new_messages(self, messages):
        """Filter messages to only include new ones that haven't been processed."""
        new_messages = []
        for msg in reversed(messages):  # Oldest first
            ts = msg.get("ts")  # Message timestamp
            self.logger.debug(f"Checking message with timestamp: {ts}")

            replies = self.client.conversations_replies(channel=self.channel_id, ts=ts)
            messages_in_thread = replies.get("messages", [])
            bot_replied = any(
                reply.get("user") == JEDI_BOT_SLACK_USER_ID
                for reply in messages_in_thread[1:]  # skip the parent msg at index 0
            )
            if self.last_seen_timestamp is None or float(ts) > float(
                self.last_seen_timestamp
            ):
                if bot_replied:
                    self.logger.debug(
                        f"Skipping message with timestamp {ts} due to bot replied"
                    )
                else:
                    new_messages.append(msg)
            else:
                self.logger.debug(
                    f"Skipping message with timestamp {ts} due to timestamp filter"
                )
        return new_messages

    def _send_error_logs_preview(
        self, errors_list, categorization_message, max_ts, is_install_issue=False
    ):
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
            message_block = self._get_slack_message_blocks(
                markdown_header=f":checking: *Error Logs Preview ({categorization_message})*\n",
                content_text=f"{errors_log_preview.strip()}",
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
            message_block = self._get_slack_message_blocks(
                markdown_header=":repeat: *Re-trigger Suggested*\n",
                content_text=retrigger_message,
            )
            self.client.chat_postMessage(
                channel=self.channel_id,
                text="Re-trigger Suggested",
                blocks=message_block,
                thread_ts=max_ts,
            )

    def _send_analysis_result(self, response, max_ts):
        """Send the final analysis result to Slack."""
        message_block = self._get_slack_message_blocks(
            markdown_header=":fast_forward: *Implications to understand (AI generated) *\n",
            content_text=response,
            use_markdown=True,
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
        message_block = self._get_slack_message_blocks(
            markdown_header=":warning: *Analysis Unavailable*\n",
            content_text=fallback_message,
        )
        self.client.chat_postMessage(
            channel=self.channel_id,
            text="Analysis Unavailable",
            blocks=message_block,
            thread_ts=max_ts,
        )

    def _summarize_messages_in_range(
        self,
        oldest_ts: str,
        latest_ts: str,
        product: str,
        ci_system: str,
        product_config,
    ) -> Tuple[
        int,
        int,
        Dict[str, int],
        Dict[str, int],
        Dict[str, Dict[str, int]],
        Dict[str, Dict[str, List[str]]],
    ]:
        """
        Iterate Slack history over [oldest_ts, latest_ts], analyze failures, and aggregate counts.
        Returns (total_jobs, total_failures, counts_by_type)
        """
        total_jobs = 0
        total_failures = 0
        counts: Dict[str, int] = {}
        version_counts: Dict[str, int] = {}
        version_type_counts: Dict[str, Dict[str, int]] = {}
        version_type_messages: Dict[str, Dict[str, List[str]]] = {}

        cursor = None
        current_latest: Optional[str] = latest_ts
        tried_without_latest = False
        while True:
            params = {
                "channel": self.channel_id,
                "oldest": oldest_ts,
                "limit": 200,
            }
            if cursor:
                params["cursor"] = cursor
            if current_latest:
                params["latest"] = current_latest

            response = self.client.conversations_history(**params)
            messages = response.get("messages", [])
            if not messages:
                # Fallback: if we requested with a latest bound and got nothing, retry without latest
                if current_latest and not tried_without_latest:
                    tried_without_latest = True
                    current_latest = None
                    cursor = None
                    continue
                break

            for msg in messages:
                text = msg.get("text", "")
                text_lower = text.lower()
                # Count only messages that look like job results
                if "job" in text_lower and "ended with" in text_lower:
                    total_jobs += 1

                # Robust failure detection (case-insensitive, tolerate punctuation/emojis)
                if "ended with" in text_lower and "failure" in text_lower:
                    total_failures += 1
                    # Extract OpenShift version like 4.19, 4.20, etc., if present
                    vm = re.search(r"\b4\.\d{1,2}\b", text_lower)
                    v = vm.group(0) if vm else None
                    if v:
                        version_counts[v] = version_counts.get(v, 0) + 1
                    analysis = download_and_analyze_logs(text, ci_system)
                    (
                        errors_list,
                        categorization_message,
                        _requires_llm,
                        is_install_issue,
                    ) = analysis
                    if errors_list is None:
                        category = "unknown"
                    else:
                        category = classify_failure_type(
                            errors_list, categorization_message, is_install_issue
                        )

                    counts[category] = counts.get(category, 0) + 1
                    if v:
                        # Try to fetch permalink for this Slack message
                        permalink = None
                        try:
                            pl_resp = self.client.chat_getPermalink(
                                channel=self.channel_id, message_ts=msg.get("ts")
                            )
                            permalink = pl_resp.get("permalink")
                        except Exception:
                            permalink = None
                        message_with_link = (
                            f"{text} -- <{permalink}|Permalink>" if permalink else text
                        )
                        version_type_counts.setdefault(v, {})[category] = (
                            version_type_counts.setdefault(v, {}).get(category, 0) + 1
                        )
                        version_type_messages.setdefault(v, {}).setdefault(
                            category, []
                        ).append(message_with_link)

            if not response.get("has_more"):
                break
            cursor = response.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        return (
            total_jobs,
            total_failures,
            counts,
            version_counts,
            version_type_counts,
            version_type_messages,
        )

    def _process_message(
        self, msg, product, ci_system, product_config, enable_inference
    ):
        """Process a single message through the complete pipeline."""
        user = msg.get("user", "Unknown")
        text = msg.get("text", "No text available")
        ts = msg.get("ts")
        text_lower = text.lower()

        self.logger.info(f"📩 New message from {user}: {text} at ts {ts}")

        # Dynamic summarize trigger: summarize <time> (e.g., 20m, 1h, 2d)
        m = re.fullmatch(r"(?:summarise|summarize)\s+(\d+)([mhd])(?:\s+verbose)?", text_lower)
        if m:
            value, unit = m.group(1), m.group(2)
            factor = {"m": 60, "h": 3600, "d": 86400}[unit]
            lookback = int(value) * factor
            verbose = "verbose" in text_lower
            self.logger.info("Triggering time summary on demand for %s%s", value, unit)
            self.post_time_summary(
                product=product,
                ci=ci_system,
                product_config=product_config,
                thread_ts=ts,
                lookback_seconds=lookback,
                verbose=verbose,
            )
            return ts

        # No weekly trigger; dynamic summarize only

        if "failure" not in text_lower:
            self.logger.info("Not a failure job, skipping")
            return ts

        # Extract and download logs
        (
            errors_list,
            categorization_message,
            requires_llm,
            is_install_issue,
        ) = download_and_analyze_logs(text, ci_system)
        if errors_list is None:
            return ts

        # Send error logs preview first
        self._send_error_logs_preview(
            errors_list, categorization_message, ts, is_install_issue
        )

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

                    if processed_ts and float(processed_ts) > float(
                        self.last_seen_timestamp or 0
                    ):
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

    def post_time_summary(self, **kwargs):
        """
        Fetch messages from the last lookback_seconds, aggregate failures by type, and post a summary.
        """
        try:
            product = kwargs["product"]
            ci_system = kwargs["ci"]
            product_config = kwargs["product_config"]
            thread_ts: Optional[str] = kwargs.get("thread_ts")
            lookback_seconds: int = kwargs.get(
                "lookback_seconds", SUMMARY_LOOKBACK_SECONDS
            )
            verbose: bool = kwargs.get("verbose", False)

            now = time.time()
            oldest = f"{now - lookback_seconds:.6f}"
            latest = f"{now:.6f}"

            (
                total_jobs,
                total_failures,
                counts,
                version_counts,
                version_type_counts,
                version_type_messages,
            ) = self._summarize_messages_in_range(
                oldest_ts=oldest,
                latest_ts=latest,
                product=product,
                ci_system=ci_system,
                product_config=product_config,
            )

            summary_text = render_failure_breakdown(
                counts,
                total_jobs,
                total_failures,
                version_counts=version_counts,
                version_type_counts=version_type_counts,
                version_type_messages=version_type_messages if verbose else None,
            )

            message_block = self._get_slack_message_blocks(
                markdown_header=":star: *Summary* :star:\n",
                content_text=summary_text,
                use_markdown=True,
            )

            self.client.chat_postMessage(
                channel=self.channel_id,
                text="Summary",
                blocks=message_block,
                thread_ts=thread_ts,
            )
        except SlackApiError as e:
            self.logger.error(f"❌ Slack API Error (summary): {e.response['error']}")
        except Exception as e:
            self.logger.error(f"⚠️ Unexpected Error in summary: {str(e)}")

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
