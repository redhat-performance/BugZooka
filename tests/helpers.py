"""
Test helper functions for BugZooka end-to-end testing.
"""

# Test constants
CHANNEL_ID = "C1234567890"
THREAD_TS_1 = "1756201133.123"
THREAD_TS_2 = "1756201134.789"

# Expected block counts
EXPECTED_MESSAGE_BLOCKS_COUNT = 2

# Message content expectations
ERROR_LOGS_PREVIEW_HEADER = "Error Logs Preview"
IMPLICATIONS_HEADER = "Implications to understand (AI generated)"
ANALYSIS_UNAVAILABLE_HEADER = "Analysis Unavailable"
UNAVAILABLE_REASON_TEXT = "The inference API is currently unavailable."


def _validate_message_blocks(message, expected_count, message_type):
    """Helper function to validate message block structure."""
    blocks = message.get("blocks") or []
    assert (
        len(blocks) == expected_count
    ), f"{message_type} message should have {expected_count} blocks, got {len(blocks)}"
    return blocks


def _get_simple_block_text(block):
    """Helper function to extract text from simple text blocks."""
    return block.get("text", {}).get("text", "")


def _get_nested_block_text(block):
    """Helper function to extract text from nested element blocks."""
    return block.get("elements", [])[0].get("elements", [])[0].get("text", "")


def _validate_block_content(block_text, expected_content, block_description):
    """Helper function to validate block content."""
    assert (
        expected_content in block_text
    ), f"{block_description} should contain '{expected_content}', got: {block_text[:100]}..."


def verify_slack_messages(
    posted_messages, inference_enabled=True, inference_available=True
):
    """
    Verify that error messages were processed correctly with specific message patterns.

    Expected message patterns:
    - First message: Always "Error Logs Preview"
    - Second message: Always "Job History Link"
    - If inference disabled: Only these two messages should be present
    - If inference enabled and available: Third message blocks contain
      "Implications to understand (AI generated)"
    - If inference enabled but unavailable: Third message text is "Analysis Unavailable"

    Args:
        posted_messages (list): Messages posted to Slack
        inference_enabled (bool): Whether inference is enabled
        inference_available (bool): Whether inference is available (only relevant if enabled)

    Raises:
        AssertionError: If verification fails
    """
    expected_count = 3 if inference_enabled else 2  # +1 for job-history message

    assert (
        len(posted_messages) == expected_count
    ), f"Expected {expected_count} posted messages, got {len(posted_messages)}"

    # Verify all messages are posted to correct channel and threaded properly
    for msg in posted_messages:
        assert (
            msg["channel"] == CHANNEL_ID
        ), f"Message should be posted to channel {CHANNEL_ID}, got {msg['channel']}"
        assert (
            msg["thread_ts"] == THREAD_TS_1
        ), "Message should be threaded to the error message"

    # Validate first message - Error Logs Preview
    # Can be either a file upload (is_file=True) or a regular message with blocks
    first_message = posted_messages[0]
    if first_message.get("is_file"):
        # File upload - validate text field instead of blocks
        assert (
            ERROR_LOGS_PREVIEW_HEADER in first_message.get("text", "")
        ), "File upload should contain Error Logs Preview header"
    else:
        # Regular message - validate blocks
        blocks = _validate_message_blocks(
            first_message, EXPECTED_MESSAGE_BLOCKS_COUNT, "Error Logs Preview"
        )

        first_block_text = _get_simple_block_text(blocks[0])
        _validate_block_content(
            first_block_text, ERROR_LOGS_PREVIEW_HEADER, "Error Logs Preview Header"
        )

        second_block_text = _get_nested_block_text(blocks[1])
        assert len(second_block_text) > 0, "Error Logs Preview should have text"

    # Handle analysis message - it's at index 1 if inference disabled (no job-history), index 2 if inference enabled (with job-history)
    if not inference_enabled:
        return

    analysis_message_index = 2 if inference_enabled else 1
    analysis_message = posted_messages[analysis_message_index]
    blocks = _validate_message_blocks(
        analysis_message,
        EXPECTED_MESSAGE_BLOCKS_COUNT,
        "Implications summary" if inference_available else "Unavailable",
    )

    first_block_text = _get_simple_block_text(blocks[0])

    if inference_available:
        _validate_block_content(
            first_block_text, IMPLICATIONS_HEADER, "Implications Header"
        )

        second_block_text = blocks[1].get("text", "")
        assert len(second_block_text) > 0, "Implications summary should have text"
    else:
        _validate_block_content(
            first_block_text, ANALYSIS_UNAVAILABLE_HEADER, "Analysis Unavailable Header"
        )

        second_block_text = _get_nested_block_text(blocks[1])
        _validate_block_content(
            second_block_text, UNAVAILABLE_REASON_TEXT, "Unavailable Reason"
        )


def create_test_messages(include_error=True, include_success=False):
    """
    Create test message arrays for different scenarios.

    Args:
        include_error (bool): Whether to include error messages with real Prow URLs
        include_success (bool): Whether to include success messages

    Returns:
        list: Array of test messages in Slack format
    """
    messages = []

    if include_error:
        messages.append(
            {
                "user": "U12345",
                "text": "Job periodic-ci-openshift-eng-ocp-qe-perfscale-ci-main-aws-4.20-nightly-x86-udn-density-l3-24nodes ended with failure. View logs: https://prow.ci.openshift.org/view/gs/test-platform-results/logs/periodic-ci-openshift-eng-ocp-qe-perfscale-ci-main-aws-4.20-nightly-x86-udn-density-l3-24nodes/1960160453627744256",
                "ts": THREAD_TS_1,
            }
        )

    if include_success:
        messages.append(
            {
                "user": "U12347",
                "text": "Job *periodic-ci-openshift-eng-ocp-qe-perfscale-ci-main-aws-4.20-nightly-x86-control-plane-ipsec-120nodes* ended with success. View logs: https://prow.ci.openshift.org/view/gs/test-platform-results/logs/periodic-ci-openshift-eng-ocp-qe-perfscale-ci-main-aws-4.20-nightly-x86-control-plane-ipsec-120nodes/1959918836853510144",
                "ts": THREAD_TS_2,
            }
        )

    return messages
