"""
End-to-end functional tests for SlackMessageFetcher.

These tests verify the complete application flow, focusing on message processing,
error detection, and analysis without actually connecting to external services.
"""

import logging
from unittest.mock import MagicMock, patch

from tests.helpers import CHANNEL_ID, create_test_messages, verify_slack_messages
from bugzooka.integrations.slack_fetcher import SlackMessageFetcher
from bugzooka.core.config import get_product_config
from bugzooka.integrations.inference import InferenceAPIUnavailableError


def run_slack_fetcher_test(
    test_messages,
    enable_inference,
    mock_slack_post_message,
    mock_slack_file_upload,
    mock_slack_conversations_history,
    inference_unavailable=False,
):
    """
    Run SlackMessageFetcher with complete test setup and mocking.

    Args:
        test_messages (list): Test messages to process
        enable_inference (bool): Whether to enable inference mode
        mock_slack_post_message: Mock function for Slack message posting
        mock_slack_file_upload: Mock function for Slack file uploads
        mock_slack_conversations_history: Mock function for Slack message history
        inference_unavailable (bool): If True, mock inference functions to raise InferenceAPIUnavailableError

    Returns:
        list: Messages that would be posted to Slack
    """
    posted_messages = []

    mock_post_message = mock_slack_post_message(posted_messages)
    mock_file_upload = mock_slack_file_upload(posted_messages)
    mock_conversations_history = mock_slack_conversations_history(test_messages)

    with patch("bugzooka.integrations.slack_client_base.WebClient") as mock_web_client:
        mock_client_instance = MagicMock()
        mock_client_instance.conversations_history = mock_conversations_history
        mock_client_instance.chat_postMessage = mock_post_message
        mock_client_instance.files_upload_v2 = mock_file_upload
        # Mock conversations_replies to indicate bot hasn't replied yet
        mock_client_instance.conversations_replies = MagicMock(
            return_value={"ok": True, "messages": [{"user": "U12345"}]}
        )
        # Mock chat_getPermalink for job history links
        mock_client_instance.chat_getPermalink = MagicMock(
            return_value={"ok": True, "permalink": "https://example.slack.com/archives/C123/p1234567890"}
        )
        mock_web_client.return_value = mock_client_instance

        if inference_unavailable:
            # Mock inference functions to raise InferenceAPIUnavailableError to simulate API unavailability
            filter_mock = patch(
                "bugzooka.integrations.slack_fetcher.filter_errors_with_llm",
                side_effect=InferenceAPIUnavailableError(
                    "Connection error to inference API"
                ),
            )
            analysis_mock = patch(
                "bugzooka.integrations.slack_fetcher.run_agent_analysis",
                side_effect=InferenceAPIUnavailableError(
                    "Connection error to inference API"
                ),
            )
        else:
            # Mock inference functions to return successful responses
            filter_mock = patch(
                "bugzooka.integrations.slack_fetcher.filter_errors_with_llm",
                return_value="Some filtered error summary from logs",
            )
            analysis_mock = patch(
                "bugzooka.integrations.slack_fetcher.run_agent_analysis",
                return_value="Some recommended actions.",
            )

        with filter_mock:
            with analysis_mock:
                # Create fetcher and run one fetch cycle
                logger = logging.getLogger("test")
                fetcher = SlackMessageFetcher(channel_id=CHANNEL_ID, logger=logger)

                product_config = get_product_config("OPENSHIFT")
                fetcher.fetch_messages(
                    product="OPENSHIFT",
                    ci="PROW",
                    product_config=product_config,
                    enable_inference=enable_inference,
                )

    return posted_messages


class TestSlackFetcher:
    def test_error_processing_inference_enabled(
        self, mock_slack_post_message, mock_slack_file_upload, mock_slack_conversations_history
    ):
        """Test processing error messages with inference enabled."""
        test_messages = create_test_messages(include_error=True, include_success=False)

        posted_messages = run_slack_fetcher_test(
            test_messages=test_messages,
            enable_inference=True,
            mock_slack_post_message=mock_slack_post_message,
            mock_slack_file_upload=mock_slack_file_upload,
            mock_slack_conversations_history=mock_slack_conversations_history,
        )

        verify_slack_messages(posted_messages)

    def test_error_processing_inference_disabled(
        self, mock_slack_post_message, mock_slack_file_upload, mock_slack_conversations_history
    ):
        """Test processing error messages with inference disabled still provides rule based analysis."""
        test_messages = create_test_messages(include_error=True, include_success=False)

        posted_messages = run_slack_fetcher_test(
            test_messages=test_messages,
            enable_inference=False,
            mock_slack_post_message=mock_slack_post_message,
            mock_slack_file_upload=mock_slack_file_upload,
            mock_slack_conversations_history=mock_slack_conversations_history,
        )

        verify_slack_messages(posted_messages, inference_enabled=False)

    def test_error_processing_inference_enabled_but_unavailable(
        self, mock_slack_post_message, mock_slack_file_upload, mock_slack_conversations_history
    ):
        """Test processing error messages with inference enabled but inference API unavailable."""
        test_messages = create_test_messages(include_error=True, include_success=False)

        posted_messages = run_slack_fetcher_test(
            test_messages=test_messages,
            enable_inference=True,
            mock_slack_post_message=mock_slack_post_message,
            mock_slack_file_upload=mock_slack_file_upload,
            mock_slack_conversations_history=mock_slack_conversations_history,
            inference_unavailable=True,
        )

        verify_slack_messages(posted_messages, inference_available=False)

    def test_success_processing(
        self, mock_slack_post_message, mock_slack_file_upload, mock_slack_conversations_history
    ):
        """Test processing success messages, should not post anything to Slack."""
        test_messages = create_test_messages(include_error=False, include_success=True)

        posted_messages = run_slack_fetcher_test(
            test_messages=test_messages,
            enable_inference=False,
            mock_slack_post_message=mock_slack_post_message,
            mock_slack_file_upload=mock_slack_file_upload,
            mock_slack_conversations_history=mock_slack_conversations_history,
        )

        assert len(posted_messages) == 0, (
            f"Expected no posted messages, got {len(posted_messages)}"
        )
