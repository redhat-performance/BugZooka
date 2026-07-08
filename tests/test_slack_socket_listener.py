"""
Tests for SlackSocketListener - Socket Mode integration.

These tests verify the Socket Mode event handling and app_mention processing.
"""

import logging
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
                listener = SlackSocketListener(logger=logger)

                assert listener.logger == logger
                assert listener.running is True
                mock_socket_mode_client.assert_called_once()
                mock_web_client.assert_called_once()

    def test_should_process_app_mention(self, mock_socket_mode_client, mock_web_client):
        """Test that app_mention events are processed."""
        logger = logging.getLogger("test")

        with patch("bugzooka.core.config.SLACK_BOT_TOKEN", "xoxb-test-token"):
            with patch("bugzooka.core.config.SLACK_APP_TOKEN", "xapp-test-token"):
                listener = SlackSocketListener(logger=logger)

                event = create_app_mention_event(
                    text="<@UBOTID> analyze this failure",
                    user="U12345",
                )

                assert listener._should_process_message(event) is True

    def test_should_process_any_channel(self, mock_socket_mode_client, mock_web_client):
        """Test that mentions in any channel are processed."""
        logger = logging.getLogger("test")

        with patch("bugzooka.core.config.SLACK_BOT_TOKEN", "xoxb-test-token"):
            with patch("bugzooka.core.config.SLACK_APP_TOKEN", "xapp-test-token"):
                listener = SlackSocketListener(logger=logger)

                event = create_app_mention_event(
                    text="<@UBOTID> analyze this failure",
                    user="U12345",
                )
                event["channel"] = "C_DIFFERENT_CHANNEL"

                assert listener._should_process_message(event) is True

    def test_should_not_process_bot_self_mention(
        self, mock_socket_mode_client, mock_web_client
    ):
        """Test that bot's own messages are ignored."""
        logger = logging.getLogger("test")

        with patch("bugzooka.core.config.SLACK_BOT_TOKEN", "xoxb-test-token"):
            with patch("bugzooka.core.config.SLACK_APP_TOKEN", "xapp-test-token"):
                with patch(
                    "bugzooka.integrations.slack_socket_listener.JEDI_BOT_SLACK_USER_ID",
                    "UBOTID",
                ):
                    listener = SlackSocketListener(logger=logger)

                    event = create_app_mention_event(
                        text="<@UBOTID> analyze this failure",
                        user="UBOTID",  # Bot mentioning itself
                    )

                    assert listener._should_process_message(event) is False

    def test_process_mention_general_query(
        self, mock_socket_mode_client, mock_web_client
    ):
        """Test processing a free-form question routes to general query handler."""
        logger = logging.getLogger("test")

        with patch("bugzooka.core.config.SLACK_BOT_TOKEN", "xoxb-test-token"):
            with patch("bugzooka.core.config.SLACK_APP_TOKEN", "xapp-test-token"):
                with patch(
                    "bugzooka.integrations.slack_socket_listener.analyze_general_query"
                ) as mock_analyze:
                    mock_analyze.return_value = {
                        "success": True,
                        "message": "Performance looks stable.",
                    }

                    listener = SlackSocketListener(logger=logger)

                    event = create_app_mention_event(
                        text="<@UBOTID> how is cudn doing on 5.0?",
                        user="U12345",
                        ts="1234567890.123456",
                    )

                    listener._process_mention(event)

                    mock_analyze.assert_called_once()
                    assert mock_web_client.return_value.chat_postMessage.call_count >= 2

    def test_submit_mention_for_processing(
        self, mock_socket_mode_client, mock_web_client
    ):
        """Test submitting mention for async processing in background thread."""
        logger = logging.getLogger("test")

        with patch("bugzooka.core.config.SLACK_BOT_TOKEN", "xoxb-test-token"):
            with patch("bugzooka.core.config.SLACK_APP_TOKEN", "xapp-test-token"):
                with patch(
                    "bugzooka.integrations.slack_socket_listener.analyze_general_query"
                ) as mock_analyze:
                    mock_analyze.return_value = {
                        "success": True,
                        "message": "Done",
                    }

                    listener = SlackSocketListener(logger=logger, max_workers=2)

                    event = create_app_mention_event(
                        text="<@UBOTID> async test",
                        user="U12345",
                        ts="1234567890.123456",
                    )

                    listener._submit_mention_for_processing(event)
                    listener.executor.shutdown(wait=True)

                    assert mock_web_client.return_value.chat_postMessage.call_count >= 1

    def test_submit_mention_prevents_duplicates(
        self, mock_socket_mode_client, mock_web_client
    ):
        """Test submission wrapper prevents duplicate processing."""
        logger = logging.getLogger("test")

        with patch("bugzooka.core.config.SLACK_BOT_TOKEN", "xoxb-test-token"):
            with patch("bugzooka.core.config.SLACK_APP_TOKEN", "xapp-test-token"):
                listener = SlackSocketListener(logger=logger, max_workers=2)

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
                listener = SlackSocketListener(logger=logger)

                event = create_app_mention_event(
                    text="<@UBOTID> test", ts="1234567890.123456"
                )
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
                listener = SlackSocketListener(logger=logger)

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
                listener = SlackSocketListener(logger=logger)

                # Create a different event type
                event = {
                    "type": "message",
                    "text": "Regular message",
                    "channel": CHANNEL_ID,
                }
                socket_request = create_socket_mode_request(event)

                mock_client = MagicMock()

                with patch.object(
                    listener, "_submit_mention_for_processing"
                ) as mock_handle:
                    listener._process_socket_request(mock_client, socket_request)

                    # Should acknowledge but not call handler
                    mock_client.send_socket_mode_response.assert_called_once()
                    mock_handle.assert_not_called()

    def test_process_mention_error_handling(
        self, mock_socket_mode_client, mock_web_client
    ):
        """Test that errors during mention processing are handled gracefully."""
        logger = logging.getLogger("test")

        with patch("bugzooka.core.config.SLACK_BOT_TOKEN", "xoxb-test-token"):
            with patch("bugzooka.core.config.SLACK_APP_TOKEN", "xapp-test-token"):
                with patch(
                    "bugzooka.integrations.slack_socket_listener.analyze_general_query"
                ) as mock_analyze:
                    mock_analyze.side_effect = RuntimeError("Analysis failed")

                    listener = SlackSocketListener(logger=logger)

                    event = create_app_mention_event(
                        text="<@UBOTID> test", ts="1234567890.123456"
                    )

                    # Should not raise exception
                    listener._process_mention(event)

                    # Verify error message was posted
                    calls = mock_web_client.return_value.chat_postMessage.call_args_list
                    error_texts = [c.kwargs.get("text", "") for c in calls]
                    assert any("error" in t.lower() for t in error_texts)

    def test_process_mention_triggers_nightly_analysis(
        self, mock_socket_mode_client, mock_web_client
    ):
        """Test processing inspect command triggers nightly analysis."""
        logger = logging.getLogger("test")

        with patch("bugzooka.core.config.SLACK_BOT_TOKEN", "xoxb-test-token"):
            with patch("bugzooka.core.config.SLACK_APP_TOKEN", "xapp-test-token"):
                with patch(
                    "bugzooka.integrations.slack_socket_listener.analyze_nightly_regression"
                ) as mock_analyze:
                    # Create a mock NamedTuple-like object
                    from bugzooka.analysis.nightly_regression_analyzer import (
                        NightlyInspectRequest,
                    )

                    mock_nightly_info = NightlyInspectRequest(
                        nightly_version="4.22.0-0.nightly-2026-01-05-203335",
                        previous_nightly=None,
                        config=None,
                        lookback_days="15",
                    )
                    mock_analyze.return_value = {
                        "success": True,
                        "message": "No regressions detected",
                        "nightly_info": mock_nightly_info,
                    }

                    listener = SlackSocketListener(logger=logger)

                    event = create_app_mention_event(
                        text="<@UBOTID> inspect 4.22.0-0.nightly-2026-01-05-203335",
                        user="U12345",
                        ts="1234567890.123456",
                    )

                    listener._process_mention(event)

                    # Verify acknowledgment and result messages were sent
                    assert mock_web_client.return_value.chat_postMessage.call_count >= 2
                    mock_analyze.assert_called_once()

    def test_process_mention_nightly_analysis_with_options(
        self, mock_socket_mode_client, mock_web_client
    ):
        """Test processing inspect command with vs/config/days options."""
        logger = logging.getLogger("test")

        with patch("bugzooka.core.config.SLACK_BOT_TOKEN", "xoxb-test-token"):
            with patch("bugzooka.core.config.SLACK_APP_TOKEN", "xapp-test-token"):
                with patch(
                    "bugzooka.integrations.slack_socket_listener.analyze_nightly_regression"
                ) as mock_analyze:
                    from bugzooka.analysis.nightly_regression_analyzer import (
                        NightlyInspectRequest,
                    )

                    mock_nightly_info = NightlyInspectRequest(
                        nightly_version="4.22.0-0.nightly-2026-01-05-203335",
                        previous_nightly="4.22.0-0.nightly-2026-01-01-123456",
                        config="trt-external-payload-node-density.yaml",
                        lookback_days="30",
                    )
                    mock_analyze.return_value = {
                        "success": True,
                        "message": "Regression detected in metric X",
                        "nightly_info": mock_nightly_info,
                    }

                    listener = SlackSocketListener(logger=logger)

                    event = create_app_mention_event(
                        text="<@UBOTID> inspect 4.22.0-0.nightly-2026-01-05-203335 vs 4.22.0-0.nightly-2026-01-01-123456 for config trt-external-payload-node-density.yaml for 30 days",
                        user="U12345",
                        ts="1234567890.123456",
                    )

                    listener._process_mention(event)

                    # Verify the analyzer was called with the full text
                    mock_analyze.assert_called_once()
                    call_args = mock_analyze.call_args[0][0]
                    assert "4.22.0-0.nightly-2026-01-05-203335" in call_args
                    assert "4.22.0-0.nightly-2026-01-01-123456" in call_args

    def test_process_mention_nightly_analysis_failure(
        self, mock_socket_mode_client, mock_web_client
    ):
        """Test processing inspect command when analysis fails."""
        logger = logging.getLogger("test")

        with patch("bugzooka.core.config.SLACK_BOT_TOKEN", "xoxb-test-token"):
            with patch("bugzooka.core.config.SLACK_APP_TOKEN", "xapp-test-token"):
                with patch(
                    "bugzooka.integrations.slack_socket_listener.analyze_nightly_regression"
                ) as mock_analyze:
                    mock_analyze.return_value = {
                        "success": False,
                        "message": "Invalid nightly version format",
                    }

                    listener = SlackSocketListener(logger=logger)

                    event = create_app_mention_event(
                        text="<@UBOTID> inspect invalid-version",
                        user="U12345",
                        ts="1234567890.123456",
                    )

                    listener._process_mention(event)

                    # Verify messages were sent (acknowledgment + error result)
                    assert mock_web_client.return_value.chat_postMessage.call_count >= 2

    def test_process_mention_nightly_analysis_exception(
        self, mock_socket_mode_client, mock_web_client
    ):
        """Test processing inspect command when analyzer raises exception."""
        logger = logging.getLogger("test")

        with patch("bugzooka.core.config.SLACK_BOT_TOKEN", "xoxb-test-token"):
            with patch("bugzooka.core.config.SLACK_APP_TOKEN", "xapp-test-token"):
                with patch(
                    "bugzooka.integrations.slack_socket_listener.analyze_nightly_regression"
                ) as mock_analyze:
                    mock_analyze.side_effect = Exception("MCP connection failed")

                    listener = SlackSocketListener(logger=logger)

                    event = create_app_mention_event(
                        text="<@UBOTID> inspect 4.22.0-0.nightly-2026-01-05-203335",
                        user="U12345",
                        ts="1234567890.123456",
                    )

                    listener._process_mention(event)

                    calls = mock_web_client.return_value.chat_postMessage.call_args_list
                    error_texts = [c.kwargs.get("text", "") for c in calls]
                    assert any("error" in t.lower() for t in error_texts)

    def test_process_mention_triggers_pr_analysis(
        self, mock_socket_mode_client, mock_web_client
    ):
        """Test analyze pr command triggers PR analysis handler."""
        logger = logging.getLogger("test")

        with patch("bugzooka.core.config.SLACK_BOT_TOKEN", "xoxb-test-token"):
            with patch("bugzooka.core.config.SLACK_APP_TOKEN", "xapp-test-token"):
                with patch(
                    "bugzooka.integrations.slack_socket_listener.analyze_pr_with_gemini"
                ) as mock_analyze:
                    mock_analyze.return_value = {
                        "success": True,
                        "message": "Performance looks good",
                        "pr_info": ("openshift", "ovn-kubernetes", ["123"], "4.19"),
                    }

                    listener = SlackSocketListener(logger=logger)

                    event = create_app_mention_event(
                        text="<@UBOTID> analyze pr: https://github.com/openshift/ovn-kubernetes/pull/123, compare with 4.19",
                        user="U12345",
                        ts="1234567890.123456",
                    )

                    listener._process_mention(event)

                    mock_analyze.assert_called_once()
                    assert mock_web_client.return_value.chat_postMessage.call_count >= 2

    def test_process_mention_pr_analysis_with_separator(
        self, mock_socket_mode_client, mock_web_client
    ):
        """Test PR analysis splits response by ==== separator into multiple messages."""
        logger = logging.getLogger("test")

        with patch("bugzooka.core.config.SLACK_BOT_TOKEN", "xoxb-test-token"):
            with patch("bugzooka.core.config.SLACK_APP_TOKEN", "xapp-test-token"):
                with patch(
                    "bugzooka.integrations.slack_socket_listener.analyze_pr_with_gemini"
                ) as mock_analyze:
                    separator = "=" * 80
                    mock_analyze.return_value = {
                        "success": True,
                        "message": f"Impact Assessment{separator}Table 1{separator}Table 2",
                        "pr_info": ("openshift", "ovn-kubernetes", ["123"], "4.19"),
                    }

                    listener = SlackSocketListener(logger=logger)

                    event = create_app_mention_event(
                        text="<@UBOTID> analyze pr: https://github.com/openshift/ovn-kubernetes/pull/123, compare with 4.19",
                        user="U12345",
                        ts="1234567890.123456",
                    )

                    listener._process_mention(event)

                    # ack + 3 sections (header + 2 tables)
                    assert mock_web_client.return_value.chat_postMessage.call_count == 4

    def test_process_mention_triggers_perf_summary(
        self, mock_socket_mode_client, mock_web_client
    ):
        """Test performance summary command triggers perf summary handler."""
        logger = logging.getLogger("test")

        with patch("bugzooka.core.config.SLACK_BOT_TOKEN", "xoxb-test-token"):
            with patch("bugzooka.core.config.SLACK_APP_TOKEN", "xapp-test-token"):
                with patch(
                    "bugzooka.integrations.slack_socket_listener.analyze_performance"
                ) as mock_analyze, patch(
                    "bugzooka.integrations.slack_socket_listener.parse_perf_summary_args"
                ) as mock_parse:
                    mock_parse.return_value = (
                        ["config1.yaml"],
                        ["4.19"],
                        7,
                        False,
                    )
                    mock_analyze.return_value = {
                        "success": True,
                        "messages": ["Summary table 1", "Summary table 2"],
                    }

                    listener = SlackSocketListener(logger=logger)

                    event = create_app_mention_event(
                        text="<@UBOTID> performance summary 7d",
                        user="U12345",
                        ts="1234567890.123456",
                    )

                    listener._process_mention(event)

                    mock_parse.assert_called_once()
                    mock_analyze.assert_called_once()
                    # ack + 2 summary messages
                    assert mock_web_client.return_value.chat_postMessage.call_count == 3
