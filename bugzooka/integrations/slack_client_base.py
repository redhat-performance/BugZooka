"""
Base class for Slack client implementations.
Contains shared functionality for Slack WebClient interactions,
message formatting, and common utilities.
"""
import logging
import sys
from typing import List

from slack_sdk.web import WebClient

from bugzooka.core.config import SLACK_BOT_TOKEN


class SlackClientBase:
    """
    Base class for Slack client implementations.
    Provides common initialization, message formatting, and utility methods.
    """

    def __init__(self, channel_id: str, logger: logging.Logger):
        """
        Initialize Slack client with common configuration.

        :param channel_id: Slack channel ID to monitor/post to
        :param logger: Logger instance
        """
        self.slack_bot_token = SLACK_BOT_TOKEN
        self.channel_id = channel_id
        self.logger = logger
        self.running = True

        if not self.slack_bot_token:
            self.logger.error("Missing SLACK_BOT_TOKEN environment variable.")
            sys.exit(1)

        # Initialize WebClient for API calls
        self.client = WebClient(token=self.slack_bot_token)

    def get_slack_message_blocks(
        self, markdown_header: str, content_text: str, use_markdown: bool = False
    ):
        """
        Prepares Slack message building blocks.

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

    def chunk_text(self, text: str, limit: int = 11900) -> List[str]:
        """
        Split text into chunks not exceeding `limit` characters, preferring newline or
        whitespace boundaries to avoid breaking mid-sentence.

        :param text: Text to chunk
        :param limit: Maximum characters per chunk
        :return: List of text chunks
        """
        if not text:
            return [""]

        chunks: List[str] = []
        start = 0
        text_len = len(text)
        while start < text_len:
            end = min(start + limit, text_len)
            if end == text_len:
                chunks.append(text[start:end])
                break

            # Try to break on the last newline within the window
            window = text[start:end]
            split_idx = window.rfind("\n")
            if split_idx == -1:
                # Fallback: try last whitespace
                split_idx = max(window.rfind(" "), window.rfind("\t"))
            if split_idx == -1:
                # As a last resort, hard cut at limit
                split_at = end
            else:
                split_at = start + split_idx + 1  # include the newline/space

            chunks.append(text[start:split_at].rstrip())
            start = split_at

        return chunks

    def post_message(
        self,
        text: str,
        thread_ts: str = None,
        blocks=None,
    ):
        """
        Post a message to the configured Slack channel.

        :param text: Message text
        :param thread_ts: Optional thread timestamp to reply in thread
        :param blocks: Optional blocks for rich formatting
        """
        try:
            self.client.chat_postMessage(
                channel=self.channel_id,
                text=text,
                thread_ts=thread_ts,
                blocks=blocks,
            )
        except Exception as e:
            self.logger.error(f"Error posting message to Slack: {e}", exc_info=True)
            raise

    def add_reaction(self, name: str, timestamp: str):
        """
        Add a reaction emoji to a message.

        :param name: Emoji name (without colons)
        :param timestamp: Message timestamp
        """
        try:
            self.client.reactions_add(
                name=name,
                channel=self.channel_id,
                timestamp=timestamp,
            )
        except Exception as e:
            self.logger.warning(f"Failed to add {name} reaction: {e}")

    def shutdown(self, *args):
        """
        Handle graceful shutdown.
        Subclasses should override to add specific cleanup logic.

        :param args: Signal handler arguments (optional)
        """
        if not self.running:
            return

        self.logger.info("🛑 Shutting down Slack client...")
        self.running = False
        sys.exit(0)

