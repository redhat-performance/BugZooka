"""
Slack Socket Mode integration for real-time event listening.
This integration uses WebSockets to listen for @ mentions of the bot in real-time.
Mentions are processed asynchronously using a thread pool for concurrent handling.
"""
import asyncio
import concurrent.futures
import logging
import sys
from threading import Lock, Event
from typing import Dict, Any, Set

from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

from bugzooka.core.config import (
    SLACK_APP_TOKEN,
    JEDI_BOT_SLACK_USER_ID,
)
from bugzooka.analysis.pr_analyzer import analyze_pr_with_gemini
from bugzooka.integrations.slack_client_base import SlackClientBase


class SlackSocketListener(SlackClientBase):
    """
    Real-time Slack listener using Socket Mode.
    Listens for @ mentions of the bot and processes messages asynchronously in real-time.
    """

    def __init__(
        self, channel_id: str, logger: logging.Logger, max_workers: int = 5
    ):
        """
        Initialize Socket Mode client and channel details.

        :param channel_id: Slack channel ID to monitor
        :param logger: Logger instance
        :param max_workers: Maximum number of concurrent mention handlers (default: 5)
        """
        # Initialize base class (handles WebClient, channel_id, logger, running flag, signal handler)
        super().__init__(channel_id, logger)

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

    def _should_process_message(self, event: Dict[str, Any]) -> bool:
        """
        Determine if a message should be processed.
        Only process messages that mention the bot and are in the configured channel.

        :param event: Slack event data
        :return: True if message should be processed
        """
        # Only process app_mention events
        if event.get("type") != "app_mention":
            return False

        # Check if it's in the right channel
        if event.get("channel") != self.channel_id:
            self.logger.debug(
                f"Ignoring mention in different channel: {event.get('channel')}"
            )
            return False

        # Don't process messages from the bot itself
        if event.get("user") == JEDI_BOT_SLACK_USER_ID:
            self.logger.debug("Ignoring message from bot itself")
            return False

        return True

    def _process_mention(self, event: Dict[str, Any]) -> None:
        """
        Process an @ mention of the bot (core processing logic).
        Checks for "analyze pr: {PR link}" pattern and calls Orion MCP if found.
        Otherwise sends greeting message.

        :param event: Slack event data
        """
        if not self._should_process_message(event):
            return

        user = event.get("user", "Unknown")
        ts = event.get("ts")
        channel = event.get("channel")
        text = event.get("text", "")

        self.logger.info(f"📩 Processing mention from {user} at ts {ts}")

        # Check if message contains "analyze pr"
        if "analyze pr" in text.lower():
            try:
                # Send initial acknowledgment
                self.client.chat_postMessage(
                    channel=channel,
                    text="🔍 Analyzing PR performance... This may take a few moments.",
                    thread_ts=ts,
                )
                
                # Analyze PR from text (need to run async function in sync context)
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    analysis_result = loop.run_until_complete(
                        analyze_pr_with_gemini(text)
                    )
                finally:
                    loop.close()
                
                # Split the result by "====" separator and send each part as a separate message
                message_content = analysis_result['message']
                separator = "=" * 80  # 80 equals signs
                
                # Check if separator exists in the message
                if separator in message_content:
                    # Split by separator
                    sections = message_content.split(separator)
                    
                    # Send first section with the header
                    if sections:
                        first_section = sections[0].strip()
                        self.client.chat_postMessage(
                            channel=channel,
                            text=f":robot_face: *PR Performance Analysis (AI generated)*\n\n{first_section}",
                            thread_ts=ts,
                        )
                    
                    # Send remaining sections (tables) as separate messages
                    for i, section in enumerate(sections[1:], start=1):
                        section = section.strip()
                        if section:  # Only send non-empty sections
                            self.client.chat_postMessage(
                                channel=channel,
                                text=section,
                                thread_ts=ts,
                            )
                            self.logger.debug(f"Sent section {i} of PR analysis")
                else:
                    # No separator found, send everything in one message
                    self.client.chat_postMessage(
                        channel=channel,
                        text=f":robot_face: *PR Performance Analysis (AI generated)*\n\n{message_content}",
                        thread_ts=ts,
                    )
                
                if analysis_result["success"]:
                    org, repo, pr_number, version = analysis_result["pr_info"]
                    self.logger.info(f"✅ Sent PR analysis for {org}/{repo}#{pr_number} (OpenShift {version}) to {user}")
                else:
                    self.logger.warning(f"⚠️ PR analysis failed: {analysis_result['message']}")
                    
            except Exception as e:
                self.logger.error(f"Error processing PR summarization: {e}", exc_info=True)
                self.client.chat_postMessage(
                    channel=channel,
                    text=f"❌ Unexpected error: {str(e)}",
                    thread_ts=ts,
                )
            return

        # Default: Send simple greeting message
        try:
            self.client.chat_postMessage(
                channel=channel,
                text="May the force be with you! :performance_jedi:\n\n💡 *Tip:* Try mentioning me with `analyze pr: <GitHub PR URL>, compare with <OpenShift Version>` to get performance analysis!",
                thread_ts=ts,
            )
            self.logger.info(f"✅ Sent greeting to {user}")
        except Exception as e:
            self.logger.error(f"Error sending message: {e}", exc_info=True)

    def _submit_mention_for_processing(self, event: Dict[str, Any]) -> None:
        """
        Submit mention to thread pool for async processing with duplicate detection.
        This wrapper ensures the same message isn't processed multiple times concurrently.

        :param event: Slack event data
        """
        ts = event.get("ts")

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

            if event_type == "app_mention":
                # Add eyes emoji reaction immediately for instant visual feedback
                ts = event.get("ts")
                channel = event.get("channel")
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
                future = self.executor.submit(self._submit_mention_for_processing, event)
                self.logger.debug(
                    f"Submitted mention {ts} for async processing"
                )

                # Add callback for logging completion/errors
                def log_completion(f):
                    try:
                        f.result()
                    except Exception as e:
                        self.logger.error(f"Task failed: {e}", exc_info=True)

                future.add_done_callback(log_completion)
            else:
                self.logger.debug(f"Ignoring event type: {event_type}")

    def run(self, **kwargs) -> None:
        """
        Start the Socket Mode listener.

        :param kwargs: Configuration arguments (not used, for compatibility)
        """
        self.logger.info(
            f"🚀 Starting Slack Socket Mode Listener for Channel: {self.channel_id}"
        )
        self.logger.info(
            "Bot will respond to @ mentions with a simple greeting message"
        )
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

