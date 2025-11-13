"""
Tests for SlackSocketListener - Socket Mode integration.

These tests verify the Socket Mode event handling and app_mention processing.
"""

import logging
import time
from unittest.mock import MagicMock, patch

import pytest

from bugzooka.integrations.slack_socket_listener import SlackSocketListener
from tests.helpers import CHANNEL_ID


@pytest.fixture
def mock_socket_mode_client():
    """Mock SocketModeClient for testing."""
    with patch(
        "bugzooka.integrations.slack_socket_listener.SocketModeClient"
    ) as mock_client:
        mock_instance = MagicMock()
        mock_instance.socket_mode_request_listeners = []
        mock_client.return_value = mock_instance
        yield mock_client


@pytest.fixture
def mock_web_client():
    """Mock WebClient for testing."""
    with patch("bugzooka.integrations.slack_client_base.WebClient") as mock_client:
        mock_instance = MagicMock()
        mock_client.return_value = mock_instance
        yield mock_client


def create_app_mention_event(text, user="U12345", ts="1234567890.123456"):
    """
    Create a mock app_mention event.

    Args:
        text: Message text
        user: User ID who mentioned the bot
        ts: Message timestamp

    Returns:
        dict: Mock event payload
    """
    return {
        "type": "app_mention",
        "user": user,
        "text": text,
        "ts": ts,
        "channel": CHANNEL_ID,
    }


def create_socket_mode_request(event):
    """
    Create a mock SocketModeRequest with an event.

    Args:
        event: Event payload

    Returns:
        MagicMock: Mock SocketModeRequest
    """
    mock_request = MagicMock()
    mock_request.type = "events_api"
    mock_request.envelope_id = "test-envelope-id"
    mock_request.payload = {"event": event}
    return mock_request


class TestSlackSocketListener:
    def test_initialization(self, mock_socket_mode_client, mock_web_client):
        """Test that SlackSocketListener initializes correctly."""
        logger = logging.getLogger("test")

        with patch("bugzooka.core.config.SLACK_BOT_TOKEN", "xoxb-test-token"):
            with patch("bugzooka.core.config.SLACK_APP_TOKEN", "xapp-test-token"):
                listener = SlackSocketListener(channel_id=CHANNEL_ID, logger=logger)

                assert listener.channel_id == CHANNEL_ID
                assert listener.logger == logger
                assert listener.running is True
                mock_socket_mode_client.assert_called_once()
                mock_web_client.assert_called_once()

    def test_should_process_app_mention(self, mock_socket_mode_client, mock_web_client):
        """Test that app_mention events in the correct channel are processed."""
        logger = logging.getLogger("test")

        with patch("bugzooka.core.config.SLACK_BOT_TOKEN", "xoxb-test-token"):
            with patch("bugzooka.core.config.SLACK_APP_TOKEN", "xapp-test-token"):
                listener = SlackSocketListener(channel_id=CHANNEL_ID, logger=logger)

                event = create_app_mention_event(
                    text="<@UBOTID> analyze this failure",
                    user="U12345",
                )

                assert listener._should_process_message(event) is True

    def test_should_not_process_wrong_channel(
        self, mock_socket_mode_client, mock_web_client
    ):
        """Test that mentions in other channels are ignored."""
        logger = logging.getLogger("test")

        with patch("bugzooka.core.config.SLACK_BOT_TOKEN", "xoxb-test-token"):
            with patch("bugzooka.core.config.SLACK_APP_TOKEN", "xapp-test-token"):
                listener = SlackSocketListener(channel_id=CHANNEL_ID, logger=logger)

                event = create_app_mention_event(
                    text="<@UBOTID> analyze this failure",
                    user="U12345",
                )
                event["channel"] = "C_DIFFERENT_CHANNEL"

                assert listener._should_process_message(event) is False

    def test_should_not_process_bot_self_mention(
        self, mock_socket_mode_client, mock_web_client
    ):
        """Test that bot's own messages are ignored."""
        logger = logging.getLogger("test")

        with patch("bugzooka.core.config.SLACK_BOT_TOKEN", "xoxb-test-token"):
            with patch("bugzooka.core.config.SLACK_APP_TOKEN", "xapp-test-token"):
                with patch("bugzooka.integrations.slack_socket_listener.JEDI_BOT_SLACK_USER_ID", "UBOTID"):
                    listener = SlackSocketListener(channel_id=CHANNEL_ID, logger=logger)

                    event = create_app_mention_event(
                        text="<@UBOTID> analyze this failure",
                        user="UBOTID",  # Bot mentioning itself
                    )

                    assert listener._should_process_message(event) is False

    def test_process_mention_sends_greeting(
        self, mock_socket_mode_client, mock_web_client
    ):
        """Test processing app_mention sends greeting message."""
        logger = logging.getLogger("test")

        with patch("bugzooka.core.config.SLACK_BOT_TOKEN", "xoxb-test-token"):
            with patch("bugzooka.core.config.SLACK_APP_TOKEN", "xapp-test-token"):
                listener = SlackSocketListener(channel_id=CHANNEL_ID, logger=logger)

                event = create_app_mention_event(
                    text="<@UBOTID> hello",
                    user="U12345",
                    ts="1234567890.123456",
                )

                # Call the core processing logic directly
                listener._process_mention(event)

                # Verify greeting message was sent (reaction happens earlier in the flow)
                mock_web_client.return_value.chat_postMessage.assert_called_once_with(
                    channel=CHANNEL_ID,
                    text="May the force be with you! :performance_jedi:\n\n💡 *Tip:* Try mentioning me with `analyze pr: <GitHub PR URL>, compare with <OpenShift Version>` to get performance analysis!",
                    thread_ts="1234567890.123456",
                )

    def test_submit_mention_for_processing(
        self, mock_socket_mode_client, mock_web_client
    ):
        """Test submitting mention for async processing in background thread."""
        logger = logging.getLogger("test")

        with patch("bugzooka.core.config.SLACK_BOT_TOKEN", "xoxb-test-token"):
            with patch("bugzooka.core.config.SLACK_APP_TOKEN", "xapp-test-token"):
                listener = SlackSocketListener(channel_id=CHANNEL_ID, logger=logger, max_workers=2)

                event = create_app_mention_event(
                    text="<@UBOTID> async test",
                    user="U12345",
                    ts="1234567890.123456",
                )

                # Call submission wrapper
                listener._submit_mention_for_processing(event)

                # Wait for thread to complete
                listener.executor.shutdown(wait=True)

                # Verify it was processed
                mock_web_client.return_value.chat_postMessage.assert_called_once()

    def test_submit_mention_prevents_duplicates(
        self, mock_socket_mode_client, mock_web_client
    ):
        """Test submission wrapper prevents duplicate processing."""
        logger = logging.getLogger("test")

        with patch("bugzooka.core.config.SLACK_BOT_TOKEN", "xoxb-test-token"):
            with patch("bugzooka.core.config.SLACK_APP_TOKEN", "xapp-test-token"):
                listener = SlackSocketListener(channel_id=CHANNEL_ID, logger=logger, max_workers=2)

                event = create_app_mention_event(
                    text="<@UBOTID> duplicate test",
                    user="U12345",
                    ts="1234567890.999999",
                )

                # Manually add to processing set
                listener.processing_messages.add("1234567890.999999")

                # Try to process - should be skipped
                listener._submit_mention_for_processing(event)

                # Should not have called anything since it was already processing
                mock_web_client.return_value.chat_postMessage.assert_not_called()

    def test_process_socket_request_acknowledgement(
        self, mock_socket_mode_client, mock_web_client
    ):
        """Test that Socket Mode requests are properly acknowledged."""
        logger = logging.getLogger("test")

        with patch("bugzooka.core.config.SLACK_BOT_TOKEN", "xoxb-test-token"):
            with patch("bugzooka.core.config.SLACK_APP_TOKEN", "xapp-test-token"):
                listener = SlackSocketListener(channel_id=CHANNEL_ID, logger=logger)

                event = create_app_mention_event(text="<@UBOTID> test", ts="1234567890.123456")
                socket_request = create_socket_mode_request(event)

                mock_client = MagicMock()

                # Mock to prevent actual processing
                with patch.object(listener, "_submit_mention_for_processing"):
                    listener._process_socket_request(mock_client, socket_request)

                    # Verify acknowledgement was sent
                    mock_client.send_socket_mode_response.assert_called_once()
                    response = mock_client.send_socket_mode_response.call_args[0][0]
                    assert response.envelope_id == "test-envelope-id"

    def test_process_socket_request_adds_reaction_immediately(
        self, mock_socket_mode_client, mock_web_client
    ):
        """Test that eyes emoji is added immediately before async processing."""
        logger = logging.getLogger("test")

        with patch("bugzooka.core.config.SLACK_BOT_TOKEN", "xoxb-test-token"):
            with patch("bugzooka.core.config.SLACK_APP_TOKEN", "xapp-test-token"):
                listener = SlackSocketListener(channel_id=CHANNEL_ID, logger=logger)

                event = create_app_mention_event(
                    text="<@UBOTID> test",
                    ts="1234567890.123456",
                )
                socket_request = create_socket_mode_request(event)

                mock_client = MagicMock()

                # Mock to prevent actual processing
                with patch.object(listener, "_submit_mention_for_processing"):
                    listener._process_socket_request(mock_client, socket_request)

                    # Verify eyes reaction was added immediately
                    mock_web_client.return_value.reactions_add.assert_called_once_with(
                        name="eyes",
                        channel=CHANNEL_ID,
                        timestamp="1234567890.123456",
                    )

    def test_process_socket_request_non_app_mention(
        self, mock_socket_mode_client, mock_web_client
    ):
        """Test that non-app_mention events are acknowledged but not processed."""
        logger = logging.getLogger("test")

        with patch("bugzooka.core.config.SLACK_BOT_TOKEN", "xoxb-test-token"):
            with patch("bugzooka.core.config.SLACK_APP_TOKEN", "xapp-test-token"):
                listener = SlackSocketListener(channel_id=CHANNEL_ID, logger=logger)

                # Create a different event type
                event = {
                    "type": "message",
                    "text": "Regular message",
                    "channel": CHANNEL_ID,
                }
                socket_request = create_socket_mode_request(event)

                mock_client = MagicMock()

                with patch.object(listener, "_submit_mention_for_processing") as mock_handle:
                    listener._process_socket_request(mock_client, socket_request)

                    # Should acknowledge but not call handler
                    mock_client.send_socket_mode_response.assert_called_once()
                    mock_handle.assert_not_called()

    def test_process_mention_error_handling(
        self, mock_socket_mode_client, mock_web_client
    ):
        """Test that errors during mention processing are properly logged."""
        logger = logging.getLogger("test")

        with patch("bugzooka.core.config.SLACK_BOT_TOKEN", "xoxb-test-token"):
            with patch("bugzooka.core.config.SLACK_APP_TOKEN", "xapp-test-token"):
                listener = SlackSocketListener(channel_id=CHANNEL_ID, logger=logger)

                # Mock chat_postMessage to raise an exception
                mock_web_client.return_value.chat_postMessage.side_effect = Exception(
                    "Test error"
                )

                event = create_app_mention_event(
                    text="<@UBOTID> test", ts="1234567890.123456"
                )

                # Should not raise exception
                listener._process_mention(event)

                # Verify chat_postMessage was attempted
                mock_web_client.return_value.chat_postMessage.assert_called_once()

