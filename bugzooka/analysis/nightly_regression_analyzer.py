"""
Nightly Regression Analysis using MCP tools.
This module provides functionality to analyze nightly OpenShift builds for
performance regressions by directly calling the has_nightly_regressed MCP tool.
"""
import logging
import re
from typing import Optional, NamedTuple

from bugzooka.integrations.mcp_client import (
    get_mcp_tool,
    initialize_global_resources_async,
    invoke_mcp_tool,
    tool_not_found_error,
)
from bugzooka.core.utils import make_response


logger = logging.getLogger(__name__)

# Default lookback in days
DEFAULT_LOOKBACK_DAYS = "15"
TOOL_NAME = "has_nightly_regressed"

# Nightly version regex pattern
NIGHTLY_VERSION_PATTERN = r"\d+\.\d+\.\d+-0\.nightly-\d{4}-\d{2}-\d{2}-\d+"


class NightlyInspectRequest(NamedTuple):
    """Parsed nightly inspection request."""

    nightly_version: str
    previous_nightly: Optional[str]
    config: Optional[str]
    lookback_days: str


def _parse_nightly_inspect_request(text: str) -> Optional[NightlyInspectRequest]:
    """
    Parse nightly inspection request from text.

    Expected formats:
    - "inspect <nightly_version>"
    - "inspect <nightly_version> vs <previous_nightly>"
    - "inspect <nightly_version> for config <config>"
    - "inspect <nightly_version> for <N> days"
    - "inspect <nightly_version> vs <previous_nightly> for config <config>"

    Examples:
    - "inspect 4.22.0-0.nightly-2026-01-05-203335"
    - "inspect 4.22.0-0.nightly-2026-01-05-203335 vs 4.22.0-0.nightly-2026-01-01-123456"
    - "inspect 4.22.0-0.nightly-2026-01-05-203335 for config trt-external-payload-node-density.yaml"
    - "inspect 4.22.0-0.nightly-2026-01-05-203335 for config trt-external-payload-node-density.yaml for 30 days"
    - "inspect 4.22.0-0.nightly-2026-01-05-203335 for 30 days"
    - "inspect 4.22.0-0.nightly-2026-01-05-203335 vs 4.22.0-0.nightly-2026-01-01-123456 for config trt-external-payload-node-density.yaml"

    :param text: Message text to parse
    :return: NightlyInspectRequest or None if invalid
    """
    # Match primary nightly version after "inspect"
    nightly_pattern = rf"inspect\s+({NIGHTLY_VERSION_PATTERN})"
    nightly_match = re.search(nightly_pattern, text, re.IGNORECASE)

    if not nightly_match:
        return None

    nightly_version = nightly_match.group(1)

    # Extract optional previous nightly from "vs <previous_nightly>" or "against <previous_nightly>" pattern
    previous_pattern = rf"(?:vs|against|compare\s+with)\s+({NIGHTLY_VERSION_PATTERN})"
    previous_match = re.search(previous_pattern, text, re.IGNORECASE)
    previous_nightly = previous_match.group(1) if previous_match else None

    # Extract optional config from "for config <config_name>" pattern
    config_pattern = r"for\s+config\s+([\w\-\.]+(?:\.yaml)?)"
    config_match = re.search(config_pattern, text, re.IGNORECASE)
    config = config_match.group(1) if config_match else None

    # Extract optional lookback days from "for <N> days" pattern
    days_pattern = r"for\s+(\d+)\s+days?"
    days_match = re.search(days_pattern, text, re.IGNORECASE)
    lookback_days = days_match.group(1) if days_match else DEFAULT_LOOKBACK_DAYS

    return NightlyInspectRequest(
        nightly_version=nightly_version,
        previous_nightly=previous_nightly,
        config=config,
        lookback_days=lookback_days,
    )


async def analyze_nightly_regression(text: str, channel_id: str = None) -> dict:
    """
    Parse nightly inspection request and call has_nightly_regressed MCP tool directly.

    Expected input formats:
    - "inspect 4.22.0-0.nightly-2026-01-05-203335"
    - "inspect 4.22.0-0.nightly-2026-01-05-203335 vs 4.22.0-0.nightly-2026-01-01-123456"
    - "inspect 4.22.0-0.nightly-2026-01-05-203335 for config trt-external-payload-node-density.yaml"
    - "inspect 4.22.0-0.nightly-2026-01-05-203335 for config trt-external-payload-node-density.yaml for 30 days"
    - "inspect 4.22.0-0.nightly-2026-01-05-203335 for 30 days"
    - "inspect 4.22.0-0.nightly-2026-01-05-203335 vs 4.22.0-0.nightly-2026-01-01-123456 for config trt-external-payload-node-density.yaml"

    :param text: User message text with nightly version (required) and optional previous_nightly/config/days
    :param channel_id: Slack channel ID for ES_SERVER routing (optional)
    :return: Dictionary with 'success' (bool), 'message' (str), and optional 'nightly_info'
    """
    # Set channel context for ES encryption interceptor
    if channel_id:
        from bugzooka.integrations.mcp_interceptors import current_channel
        current_channel.set(channel_id)
        logger.debug("Set channel context for nightly regression analysis: %s", channel_id)
    # Parse nightly inspect request from text
    parsed = _parse_nightly_inspect_request(text)

    if not parsed:
        return make_response(
            success=False,
            message=(
                "Invalid nightly inspection request format.\n\n"
                "*Required format:*\n"
                "```\n"
                "inspect <nightly_version> [vs <previous_nightly>] [for config <config_name>] [for <N> days]\n"
                "```\n\n"
                "*Examples:*\n"
                "- `inspect 4.22.0-0.nightly-2026-01-05-203335`\n"
                "- `inspect 4.22.0-0.nightly-2026-01-05-203335 vs 4.22.0-0.nightly-2026-01-01-123456`\n"
                "- `inspect 4.22.0-0.nightly-2026-01-05-203335 for config trt-external-payload-node-density.yaml`\n"
                "- `inspect 4.22.0-0.nightly-2026-01-05-203335 for 30 days`\n"
                "- `inspect 4.22.0-0.nightly-2026-01-05-203335 vs 4.22.0-0.nightly-2026-01-01-123456 for config trt-external-payload-node-density.yaml`\n\n"
                f"Default lookback: {DEFAULT_LOOKBACK_DAYS} days\n"
            ),
        )

    config_display = parsed.config if parsed.config else "all TRT configs"
    comparison_display = (
        f" vs `{parsed.previous_nightly}`" if parsed.previous_nightly else ""
    )

    logger.info(
        "Nightly inspection requested for %s%s (config: %s, lookback: %s days)",
        parsed.nightly_version,
        f" vs {parsed.previous_nightly}" if parsed.previous_nightly else "",
        config_display,
        parsed.lookback_days,
    )

    # Ensure MCP client is initialized
    await initialize_global_resources_async()

    # Get the MCP tool
    orion_tool = get_mcp_tool(TOOL_NAME)
    if not orion_tool:
        return tool_not_found_error(TOOL_NAME)

    try:
        logger.info(
            "Calling %s tool: %s (previous: %s, config: %s, lookback: %s days)",
            TOOL_NAME,
            parsed.nightly_version,
            parsed.previous_nightly or "N/A",
            config_display,
            parsed.lookback_days,
        )

        # Build tool arguments
        tool_args = {
            "nightly_version": parsed.nightly_version,
            "lookback": parsed.lookback_days,
        }
        if parsed.previous_nightly:
            tool_args["previous_nightly"] = parsed.previous_nightly
        if parsed.config:
            tool_args["configs"] = parsed.config

        # Call the MCP tool
        result = await invoke_mcp_tool(orion_tool, tool_args)

        logger.info("%s returned (%d chars)", TOOL_NAME, len(result))

        # Format the response for Slack
        header = (
            f"*Nightly:* `{parsed.nightly_version}`{comparison_display}\n"
            f"*Config:* {config_display}\n"
            f"*Lookback:* {parsed.lookback_days} days\n\n"
        )

        return make_response(
            success=True,
            message=header + result,
            nightly_info=(
                parsed.nightly_version,
                parsed.previous_nightly,
                parsed.config,
                parsed.lookback_days,
            ),
        )

    except Exception as e:
        error_msg = f"Error calling {TOOL_NAME}: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return make_response(success=False, message=error_msg)
