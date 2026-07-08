"""
Slack Socket Mode integration for real-time event listening.
This integration uses WebSockets to listen for @ mentions of the bot in real-time.
Mentions are processed asynchronously using a thread pool for concurrent handling.
"""

import concurrent.futures
import logging
import re
import sys
import time
from threading import Lock, Event
from typing import Dict, Any, Set

import anyio

from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

from bugzooka.core.config import (
    SLACK_APP_TOKEN,
    JEDI_BOT_SLACK_USER_ID,
)
from bugzooka.analysis.pr_analyzer import analyze_pr_with_gemini
from bugzooka.analysis.nightly_regression_analyzer import analyze_nightly_regression
from bugzooka.analysis.perf_summary_analyzer import (
    analyze_performance,
    parse_perf_summary_args,
)
from bugzooka.analysis.general_query_analyzer import analyze_general_query
from bugzooka.core.conversation import ConversationManager
from bugzooka.integrations.slack_client_base import SlackClientBase
from bugzooka.integrations.image_collector import (
    ImageCollector,
    set_collector,
    reset_collector,
)
from bugzooka import telemetry


class SlackSocketListener(SlackClientBase):
    """
    Real-time Slack listener using Socket Mode.
    Listens for @ mentions of the bot and processes messages asynchronously in real-time.
    """

    def __init__(self, logger: logging.Logger, max_workers: int = 5):
        """
        Initialize Socket Mode client.

        :param logger: Logger instance
        :param max_workers: Maximum number of concurrent mention handlers (default: 5)
        """
        # Initialize base class (handles WebClient, logger, running flag, signal handler)
        super().__init__(logger)

        self.slack_app_token = SLACK_APP_TOKEN

        if not self.slack_app_token:
            self.logger.error("Missing SLACK_APP_TOKEN environment variable.")
            sys.exit(1)

        # Initialize Socket Mode client (uses self.client from base class)
        self.socket_client = SocketModeClient(
            app_token=self.slack_app_token,
            web_client=self.client,
        )

        # Initialize thread pool for async processing
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="mention-handler-",
        )

        # Track messages being processed to avoid duplicates
        self.processing_lock = Lock()
        self.processing_messages: Set[str] = set()

        # Conversation history for multi-turn interactions
        self.conversation_manager = ConversationManager()

    def _should_process_message(self, event: Dict[str, Any]) -> bool:
        """
        Determine if a message should be processed.
        Accepts app_mention events and thread reply messages to known conversations.

        :param event: Slack event data
        :return: True if message should be processed
        """
        event_type = event.get("type")

        # Don't process messages from the bot itself
        if event.get("user") == JEDI_BOT_SLACK_USER_ID:
            self.logger.debug("Ignoring message from bot itself")
            return False

        # Always process direct @mentions
        if event_type == "app_mention":
            return True

        # Process thread replies to conversations we're already tracking
        if event_type == "message":
            thread_ts = event.get("thread_ts")
            channel = event.get("channel")
            if thread_ts and channel:
                msgs = self.conversation_manager.get_messages(channel, thread_ts)
                if msgs:
                    self.logger.debug("Processing thread reply to active conversation")
                    return True

        return False

    @staticmethod
    def _clean_mention_text(text: str) -> str:
        """Remove bot @mention tags from message text."""
        return re.sub(r"<@\w+>\s*", "", text).strip()

    def _run_command(
        self,
        *,
        command_name: str,
        channel: str,
        thread_ts: str,
        user: str,
        ack_text: str,
        handler: Any,
    ) -> None:
        _start = time.time()
        _success = False
        _error_message = None
        _error_type = None
        extra_telemetry: Dict[str, Any] = {}
        try:
            self.client.chat_postMessage(
                channel=channel, text=ack_text, thread_ts=thread_ts
            )
            extra_telemetry = handler()
            _success = True
        except Exception as e:
            self.logger.error("Error processing %s: %s", command_name, e, exc_info=True)
            self.client.chat_postMessage(
                channel=channel,
                text=f"An error occurred: {str(e)}",
                thread_ts=thread_ts,
            )
            _error_message = str(e)
            _error_type = type(e).__name__
        finally:
            telemetry.emit(
                {
                    "command": command_name,
                    "trigger_type": "user_initiated",
                    "channel_id": channel,
                    "user_id": user,
                    "success": _success,
                    "error_message": _error_message,
                    "error_type": _error_type,
                    "duration_ms": int((time.time() - _start) * 1000),
                    "retry_count": 0,
                    **extra_telemetry,
                }
            )

    def _handle_pr_analysis(
        self, text: str, channel: str, thread_ts: str, user: str
    ) -> Dict[str, Any]:
        analysis_result = anyio.run(analyze_pr_with_gemini, text, channel)
        message_content = analysis_result["message"]
        separator = "=" * 80

        if separator in message_content:
            sections = message_content.split(separator)
            if sections:
                self.client.chat_postMessage(
                    channel=channel,
                    text=f":robot_face: *PR Performance Analysis (AI generated)*\n\n{sections[0].strip()}",
                    thread_ts=thread_ts,
                )
            for i, section in enumerate(sections[1:], start=1):
                section = section.strip()
                if section:
                    self.client.chat_postMessage(
                        channel=channel, text=section, thread_ts=thread_ts
                    )
                    self.logger.debug("Sent section %d of PR analysis", i)
        else:
            self.client.chat_postMessage(
                channel=channel,
                text=f":robot_face: *PR Performance Analysis (AI generated)*\n\n{message_content}",
                thread_ts=thread_ts,
            )

        extra: Dict[str, Any] = {}
        if analysis_result["success"]:
            org, repo, pr_numbers, version = analysis_result["pr_info"]
            extra["pr_repo"] = f"{org}/{repo}"
            self.logger.info(
                "Sent PR analysis for %s/%s %s (OpenShift %s) to %s",
                org,
                repo,
                ", ".join(f"#{p}" for p in pr_numbers),
                version,
                user,
            )
        else:
            self.logger.warning("PR analysis failed: %s", analysis_result["message"])

        try:
            from bugzooka.integrations.inference_client import get_inference_client

            client = get_inference_client()
            extra["total_tokens"] = client.last_total_tokens
            extra["tool_calls_count"] = client.last_tool_calls_count
        except Exception:
            self.logger.debug("Unable to read inference telemetry", exc_info=True)
        return extra

    def _handle_nightly_inspection(
        self, text: str, channel: str, thread_ts: str, user: str
    ) -> Dict[str, Any]:
        analysis_result = anyio.run(analyze_nightly_regression, text, channel)
        message_content = analysis_result["message"]

        self.client.chat_postMessage(
            channel=channel,
            text=f"*Nightly Regression Analysis*\n\n{message_content}",
            thread_ts=thread_ts,
        )

        extra: Dict[str, Any] = {}
        if analysis_result["success"]:
            nightly_info = analysis_result.get("nightly_info")
            if nightly_info:
                extra["nightly_version"] = nightly_info[0]
                self.logger.info(
                    "Sent nightly regression analysis for %s to %s",
                    nightly_info[0],
                    user,
                )
            else:
                self.logger.info("Sent nightly regression analysis to %s", user)
        else:
            self.logger.warning(
                "Nightly regression analysis failed: %s", analysis_result["message"]
            )
        return extra

    def _handle_perf_summary(
        self, text: str, channel: str, thread_ts: str, user: str
    ) -> Dict[str, Any]:
        configs, versions, lookback_days, use_all_configs = parse_perf_summary_args(
            text
        )

        result = anyio.run(
            analyze_performance,
            configs,
            versions,
            lookback_days,
            use_all_configs,
            channel,
        )

        if result["success"]:
            messages = result.get("messages", [])
            for msg in messages:
                self.client.chat_postMessage(
                    channel=channel, text=msg, thread_ts=thread_ts
                )
            self.logger.info(
                "Sent performance summary to %s (%d message(s))", user, len(messages)
            )
        else:
            self.client.chat_postMessage(
                channel=channel,
                text=result.get("message", "Unknown error"),
                thread_ts=thread_ts,
            )
            self.logger.warning("Performance summary failed: %s", result.get("message"))

        return {"configs_count": len(configs), "versions_count": len(versions)}

    def _handle_general_query(
        self,
        event: Dict[str, Any],
        text: str,
        channel: str,
        ts: str,
        user: str,
    ) -> None:
        thread_ts = event.get("thread_ts", ts)
        clean_text = self._clean_mention_text(text)
        collector = ImageCollector()
        # Must set collector inside the worker thread — ThreadPoolExecutor
        # does not propagate the calling thread's contextvars.
        token = set_collector(collector)

        def handler() -> Dict[str, Any]:
            self.conversation_manager.append_user_message(
                channel, thread_ts, clean_text
            )
            conversation_messages = self.conversation_manager.get_messages(
                channel, thread_ts
            )

            analysis_result = anyio.run(
                analyze_general_query, conversation_messages, channel
            )
            result_text = analysis_result.get("message", "")

            if analysis_result.get("success"):
                self.conversation_manager.append_assistant_message(
                    channel, thread_ts, result_text
                )
                chunks = self.chunk_text(result_text)
                for chunk in chunks:
                    self.client.chat_postMessage(
                        channel=channel, text=chunk, thread_ts=thread_ts
                    )

                if collector.has_images():
                    for img in collector.get_images():
                        try:
                            self.client.files_upload_v2(
                                channel=channel,
                                content=collector.decode_image(img),
                                filename=img["filename"],
                                title=f"Chart from {img['tool_name']}",
                                thread_ts=thread_ts,
                            )
                        except Exception as upload_err:
                            self.logger.error("Failed to upload image: %s", upload_err)

                self.logger.info(
                    "Sent general query response to %s (%d chunk(s))",
                    user,
                    len(chunks),
                )
            else:
                self.client.chat_postMessage(
                    channel=channel, text=result_text, thread_ts=thread_ts
                )
                self.logger.warning("General query failed: %s", result_text)

            extra: Dict[str, Any] = {"is_followup": event.get("thread_ts") is not None}
            try:
                from bugzooka.integrations.inference_client import get_inference_client

                client = get_inference_client()
                extra["total_tokens"] = client.last_total_tokens
                extra["tool_calls_count"] = client.last_tool_calls_count
            except Exception:
                self.logger.debug("Unable to read inference telemetry", exc_info=True)
            return extra

        try:
            self._run_command(
                command_name="general_query",
                channel=channel,
                thread_ts=thread_ts,
                user=user,
                ack_text=":hourglass_flowing_sand: Thinking...",
                handler=handler,
            )
        finally:
            collector.clear()
            reset_collector(token)

    def _process_mention(self, event: Dict[str, Any]) -> None:
        """
        Process an @ mention of the bot.
        Routes to specialized handlers for structured commands,
        or falls back to the general agentic query handler.
        """
        if not self._should_process_message(event):
            return

        user: str = event.get("user", "Unknown")
        ts: str = event.get("ts", "")
        channel: str = event.get("channel", "")
        text: str = event.get("text", "")
        text_lower = text.lower()

        self.logger.info("Processing mention from %s at ts %s", user, ts)

        if "analyze pr" in text_lower:
            self._run_command(
                command_name="analyze_pr",
                channel=channel,
                thread_ts=ts,
                user=user,
                ack_text="🔍 Analyzing PR performance... This may take a few moments.",
                handler=lambda: self._handle_pr_analysis(text, channel, ts, user),
            )
        elif "inspect" in text_lower:
            self._run_command(
                command_name="inspect_nightly",
                channel=channel,
                thread_ts=ts,
                user=user,
                ack_text=":mag: Analyzing nightly build for regressions... This may take a few moments.",
                handler=lambda: self._handle_nightly_inspection(
                    text, channel, ts, user
                ),
            )
        elif "performance summary" in text_lower:
            self._run_command(
                command_name="perf_summary",
                channel=channel,
                thread_ts=ts,
                user=user,
                ack_text="📊 Gathering performance summary... This may take a moment.",
                handler=lambda: self._handle_perf_summary(text, channel, ts, user),
            )
        else:
            self._handle_general_query(event, text, channel, ts, user)

    def _submit_mention_for_processing(self, event: Dict[str, Any]) -> None:
        """
        Submit mention to thread pool for async processing with duplicate detection.
        This wrapper ensures the same message isn't processed multiple times concurrently.

        :param event: Slack event data
        """
        ts = event.get("ts")

        # Guard against missing timestamp
        if ts is None:
            self.logger.warning("Event missing timestamp, skipping")
            return

        # Check if already processing this message
        with self.processing_lock:
            if ts in self.processing_messages:
                self.logger.debug(f"Already processing message {ts}, skipping")
                return
            self.processing_messages.add(ts)

        try:
            # Do the actual work
            self._process_mention(event)
        except Exception as e:
            self.logger.error(
                f"Unhandled error in mention handler for {ts}: {e}", exc_info=True
            )
        finally:
            # Remove from processing set
            with self.processing_lock:
                self.processing_messages.discard(ts)

    def _process_socket_request(
        self, client: SocketModeClient, req: SocketModeRequest
    ) -> None:
        """
        Process incoming Socket Mode requests.
        Acknowledges immediately and submits mentions for async processing.

        :param client: Socket Mode client
        :param req: Socket Mode request
        """
        self.logger.debug(f"Received Socket Mode request: {req.type}")

        # Always acknowledge the request immediately
        response = SocketModeResponse(envelope_id=req.envelope_id)
        client.send_socket_mode_response(response)

        # Handle events_api requests
        if req.type == "events_api":
            event = req.payload.get("event", {})
            event_type = event.get("type")

            self.logger.debug(f"Received event type: {event_type}")

            if not event.get("bot_id") and self._should_process_message(event):
                ts = event.get("ts")
                channel = event.get("channel")

                # Add eyes emoji reaction for instant visual feedback
                try:
                    self.client.reactions_add(
                        name="eyes",
                        channel=channel,
                        timestamp=ts,
                    )
                    self.logger.debug(f"👀 Added eyes reaction to message {ts}")
                except Exception as e:
                    self.logger.warning(f"Failed to add eyes reaction: {e}")

                # Submit to thread pool for async processing
                future = self.executor.submit(
                    self._submit_mention_for_processing, event
                )
                self.logger.debug(f"Submitted mention {ts} for async processing")

                # Add callback for logging completion/errors
                def log_completion(f):
                    try:
                        f.result()
                    except Exception as e:
                        self.logger.error(f"Task failed: {e}", exc_info=True)

                future.add_done_callback(log_completion)
            else:
                self.logger.debug(f"Ignoring event type: {event_type}")

    def run(self) -> None:
        """
        Start the Socket Mode listener.

        """
        self.logger.info("🚀 Starting Slack Socket Mode Listener")
        self.logger.info(
            f"Async processing enabled with {self.executor._max_workers} worker threads"
        )

        # Register the event handler
        self.socket_client.socket_mode_request_listeners.append(
            self._process_socket_request
        )

        try:
            # Establish WebSocket connection and keep it alive
            self.socket_client.connect()
            self.logger.info("✅ WebSocket connection established")

            # Keep the process running
            Event().wait()

        except KeyboardInterrupt:
            self.logger.info("🛑 Received keyboard interrupt")
        except Exception as e:
            self.logger.error(f"❌ Socket Mode error: {e}", exc_info=True)
        finally:
            self.shutdown()

    def shutdown(self, *args) -> None:
        """
        Handle graceful shutdown.
        Waits for pending mention processing tasks to complete.

        :param args: Signal handler arguments (optional)
        """
        if not self.running:
            return

        self.logger.info("🛑 Shutting down Socket Mode listener...")
        self.running = False

        # Wait for pending mention processing tasks to complete
        self.logger.info("⏳ Waiting for pending mention processing tasks...")
        try:
            self.executor.shutdown(wait=True)
            self.logger.info("✅ All pending tasks completed")
        except Exception as e:
            self.logger.warning(f"Error waiting for tasks to complete: {e}")

        # Close WebSocket connection
        try:
            if hasattr(self, "socket_client"):
                self.socket_client.close()
                self.logger.info("✅ WebSocket connection closed")
        except Exception as e:
            self.logger.warning(f"Error closing socket connection: {e}")

        # Call parent class shutdown (will exit)
        super().shutdown(*args)
