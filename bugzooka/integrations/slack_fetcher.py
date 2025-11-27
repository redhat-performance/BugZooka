import io
import time
import re
import os

from slack_sdk.errors import SlackApiError

from bugzooka.core.config import (
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
    build_summary_sections,
)
from bugzooka.analysis.prompts import RAG_AWARE_PROMPT
from bugzooka.integrations.inference import (
    InferenceAPIUnavailableError,
    AgentAnalysisLimitExceededError,
    ask_inference_api,
)
from bugzooka.integrations.rag_client_util import get_rag_context
from bugzooka.integrations.slack_client_base import SlackClientBase
from bugzooka.core.utils import (
    to_job_history_url,
    fetch_job_history_stats,
    extract_job_details,
    check_url_ok,
)
from typing import Dict, Tuple, Optional, List, Any


class SlackMessageFetcher(SlackClientBase):
    """Continuously fetches new messages from a Slack channel and logs them."""

    def __init__(self, channel_id, logger, poll_interval=600):
        """Initialize Slack client and channel details."""
        # Initialize base class (handles WebClient, channel_id, logger, running flag, signal handler)
        super().__init__(channel_id, logger)
        
        self.poll_interval = poll_interval  # How often to fetch messages
        self.last_seen_timestamp = None  # Track the latest message timestamp


    def _sanitize_job_text(self, text: str) -> str:
        """
        Clean up noisy Slack job notification text.

        Removes:
        - Slack emoji codes like :emoji_name:
        - Leading "Job " token
        - Trailing "ended with ..." clauses
        - Common job prefix "periodic-ci-openshift-eng-ocp-qe-perfscale-ci-main-"
        - Leading/trailing asterisks
        - Extra whitespace
        """
        if not text:
            return text
        cleaned = text
        # Remove Slack emojis
        cleaned = re.sub(r":[A-Za-z0-9_+\-]+:", "", cleaned)
        # Remove leading "Job "
        cleaned = re.sub(r"^\s*job\s+", "", cleaned, flags=re.IGNORECASE)
        # Remove trailing or inline "ended with ..." clauses
        cleaned = re.sub(
            r"\s*ended with[^\.!\n]*[\.!]?", "", cleaned, flags=re.IGNORECASE
        )
        # Remove common job prefix
        cleaned = re.sub(
            r"^\*?periodic-ci-openshift-eng-ocp-qe-perfscale-ci-main-", "", cleaned
        )
        # Remove asterisks at word boundaries (for leftover Slack formatting)
        cleaned = re.sub(r"\b\*+|\*+\b", "", cleaned)
        # Collapse multiple spaces into one
        cleaned = re.sub(r"\s{2,}", " ", cleaned)
        cleaned = cleaned.strip()

        return cleaned


    def _handle_job_history(
        self,
        thread_ts: str,
        current_message: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Convert the current message's View URL to job-history URL, fetch metadata,
        and post a response in the same thread.

        Returns the thread timestamp used for posting.
        """
        try:
            # Extract job details directly from the triggering message
            source_text = (current_message or {}).get("text") or ""
            view_url, _ = extract_job_details(source_text)
            if not view_url:
                self.client.chat_postMessage(
                    channel=self.channel_id,
                    text="Couldn't extract job URL from this message.",
                    thread_ts=thread_ts,
                )
                return thread_ts

            # Convert to job-history URL
            job_history_url = to_job_history_url(view_url)
            if not job_history_url:
                self.client.chat_postMessage(
                    channel=self.channel_id,
                    text="Couldn't convert view URL to a job-history URL.",
                    thread_ts=thread_ts,
                )
                return thread_ts

            # Verify the job-history URL is reachable
            ok, status_code = check_url_ok(job_history_url, timeout=10)
            if not ok:
                header = ":warning: *Job History Unavailable*\n"
                body_lines = [
                    f"URL: <{job_history_url}|Open Job History>",
                    f"HTTP Status: {status_code if status_code is not None else 'unknown'}",
                    "The job history page is not accessible right now.",
                ]
                message_block = self.get_slack_message_blocks(
                    markdown_header=header,
                    content_text="\n".join(body_lines),
                    use_markdown=True,
                )
                self.client.chat_postMessage(
                    channel=self.channel_id,
                    text="Job history unavailable",
                    blocks=message_block,
                    thread_ts=thread_ts,
                )
                return thread_ts

            # Fetch job-history stats
            (
                failure_count,
                total_count,
                failure_rate,
                status_emoji,
            ) = fetch_job_history_stats(job_history_url)

            # Prepare Slack message
            header = ":prow: *Job History*\n"
            body_lines = [
                f"URL: <{job_history_url}|Open Job History>",
                f"Failures: {failure_count} / {total_count}  ({failure_rate:.0f}%)  {status_emoji}",
            ]
            message_block = self.get_slack_message_blocks(
                markdown_header=header,
                content_text="\n".join(body_lines),
                use_markdown=True,
            )

            self.client.chat_postMessage(
                channel=self.channel_id,
                text="Job history",
                blocks=message_block,
                thread_ts=thread_ts,
            )
        except Exception as e:
            self.logger.error("job-history command failed: %s", e)
            self.client.chat_postMessage(
                channel=self.channel_id,
                text="Failed to fetch job history.",
                thread_ts=thread_ts,
            )
        return thread_ts

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
            message_block = self.get_slack_message_blocks(
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
            message_block = self.get_slack_message_blocks(
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
        message_block = self.get_slack_message_blocks(
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
        message_block = self.get_slack_message_blocks(
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
                "limit": 2000,
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
                job_url, job_name = extract_job_details(text)
                if not job_url or not job_name:
                    continue
                total_jobs += 1
                text_lower = text.lower()

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
                        cleaned_text = self._sanitize_job_text(text)
                        message_with_link = (
                            f"{cleaned_text} | <{permalink}|Permalink>"
                            if permalink
                            else cleaned_text
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

    def _is_rag_enabled(self) -> bool:
        """Check if RAG data exists under /rag."""
        rag_dir = os.getenv("RAG_DB_PATH", "/rag")
        if not os.path.isdir(rag_dir):
            return False
        # Check for expected RAG artifacts (JSON index/store files)
        return any(f.name.endswith(".json") for f in os.scandir(rag_dir))

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
        m = re.fullmatch(
            r"(?:summarise|summarize)\s+(\d+)([mhd])(?:\s+verbose)?", text_lower
        )
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

        # Add job-history info in the thread after the preview
        self._handle_job_history(thread_ts=ts, current_message=msg)

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

            # Optionally augment with RAG-aware prompt when RAG_IMAGE is set
            combined_response = analysis_response
            try:
                if self._is_rag_enabled():
                    self.logger.info(
                        "RAG data detected — augmenting analysis with RAG context."
                    )
                    rag_top_k = int(os.getenv("RAG_TOP_K", "3"))
                    rag_query = f"Provide context relevant to the following errors:\n{error_summary}"
                    rag_context = get_rag_context(rag_query, top_k=rag_top_k)
                    if rag_context:
                        rag_user = RAG_AWARE_PROMPT["user"].format(
                            rag_context=rag_context,
                            error_list=error_summary,
                        )
                        rag_messages = [
                            {"role": "system", "content": RAG_AWARE_PROMPT["system"]},
                            {"role": "user", "content": rag_user},
                            {
                                "role": "assistant",
                                "content": RAG_AWARE_PROMPT["assistant"],
                            },
                        ]
                        rag_resp = ask_inference_api(
                            messages=rag_messages,
                            url=product_config["endpoint"][product],
                            api_token=product_config["token"][product],
                            model=product_config["model"][product],
                        )
                        combined_response = (
                            f"{analysis_response}\n\n"
                            f"💡 **RAG-Informed Insights:**\n{rag_resp}"
                        )
                else:
                    self.logger.info("No RAG data found — skipping RAG augmentation.")
            except Exception as e:
                self.logger.warning("RAG augmentation failed/skipped: %s", e)

            # Send final analysis (possibly augmented)
            self._send_analysis_result(combined_response, ts)

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

            # Build posting sections in summarizer to keep this class thin
            sections = build_summary_sections(
                counts,
                total_jobs,
                total_failures,
                version_counts=version_counts,
                version_type_counts=version_type_counts,
                version_type_messages=version_type_messages,
                verbose=verbose,
            )

            CHUNK_LIMIT = 11900
            for header, content in sections:
                chunks = self.chunk_text(content, CHUNK_LIMIT)
                for chunk in chunks:
                    message_block = self.get_slack_message_blocks(
                        markdown_header=" ",  # no header line
                        content_text=chunk,
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

    def shutdown(self, *args):
        """Handles graceful shutdown on user interruption."""
        if not self.running:
            return
            
        self.logger.info("🛑 Received exit signal. Stopping message fetcher...")
        # Call parent class shutdown (will set running=False and exit)
        super().shutdown(*args)
