"""
PR Performance Analysis using Gemini with MCP tools.
This module provides functionality to analyze GitHub pull request performance
by orchestrating Gemini AI with MCP tools.
"""
import logging
import re
from typing import Optional, Tuple

from bugzooka.integrations.mcp_client import (
    get_mcp_tool,
    initialize_global_resources_async,
    tool_not_found_error,
)
from bugzooka.core.utils import make_response
from bugzooka.core.constants import PR_ANALYSIS_MAX_TOOL_ITERATIONS
from bugzooka.integrations.inference_client import analyze_with_agentic
from bugzooka.analysis.prompts import PR_PERFORMANCE_ANALYSIS_PROMPT
from bugzooka.analysis.pr_tools import clear_pr_files_cache, create_pr_tools
import bugzooka.integrations.mcp_client as mcp_module


logger = logging.getLogger(__name__)

TOOL_NAME = "openshift_report_on_pr"


def _sanitize_gemini_output(result: str) -> str:
    """
    Sanitize Gemini output by removing any thinking process that precedes
    the "*Performance Impact Assessment*" marker.

    :param result: Raw Gemini output
    :return: Sanitized output starting from the performance assessment marker
    """
    marker = "*Performance Impact Assessment*"

    # Find the marker in the result
    marker_index = result.find(marker)

    if marker_index == -1:
        # Marker not found, return original result
        logger.debug(
            "Performance Impact Assessment marker not found in output, returning as-is"
        )
        return result

    # Return everything from the marker onwards
    sanitized = result[marker_index:]
    logger.info("Sanitized output: removed %d characters before marker", marker_index)
    return sanitized


def _parse_pr_request(text: str) -> Optional[Tuple[str, str, str, str]]:
    """
    Parse PR analysis request from text.

    Expected format: "analyze pr: {github_url}, compare with {version}"

    Both PR URL and version are required.

    :param text: Message text to parse
    :return: Tuple of (organization, repository, pr_number, version) or None if invalid
    """
    # Match GitHub PR URLs like https://github.com/org/repo/pull/123
    pr_pattern = r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)"
    pr_match = re.search(pr_pattern, text)

    if not pr_match:
        return None

    org, repo, pr_number = pr_match.groups()

    # Extract version from "compare with X.XX" pattern - REQUIRED
    version_pattern = r"compare\s+with\s+(\d+\.\d+)"
    version_match = re.search(version_pattern, text, re.IGNORECASE)

    if not version_match:
        # Version is required - return None to trigger validation error
        return None

    version = version_match.group(1)

    return org, repo, pr_number, version


async def analyze_pr_with_gemini(text: str) -> dict:
    """
    Parse PR analysis request and analyze PR performance using Gemini with MCP.

    Expected input format:
    - "analyze pr: https://github.com/org/repo/pull/123, compare with 4.19"

    Both PR URL and OpenShift version are REQUIRED.

    :param text: User message text with PR URL and version (both required)
    :return: Dictionary with 'success' (bool), 'message' (str), and optional 'pr_info' (tuple)
    """
    # Parse PR request from text
    parsed = _parse_pr_request(text)

    if not parsed:
        return make_response(
            success=False,
            message=(
                "Invalid PR analysis request format.\n\n"
                "**Required format:**\n"
                "```\n"
                "analyze pr: https://github.com/org/repo/pull/123, compare with 4.19\n"
                "```\n\n"
                "Both PR URL and OpenShift version are required!\n"
            ),
        )

    org, repo, pr_number, version = parsed
    logger.info(
        f"🔍 PR analysis requested for {org}/{repo}/pull/{pr_number} (OpenShift {version})"
    )

    # Ensure MCP client is initialized
    await initialize_global_resources_async()

    # Check if Orion MCP tool is available
    orion_tool = get_mcp_tool(TOOL_NAME)
    if not orion_tool:
        return tool_not_found_error(TOOL_NAME)

    try:
        logger.info(
            "Starting PR performance analysis: %s/%s#%s (OpenShift %s)",
            org,
            repo,
            pr_number,
            version,
        )

        # Create prompt for PR analysis using centralized prompts
        pr_url = f"https://github.com/{org}/{repo}/pull/{pr_number}"

        system_prompt = PR_PERFORMANCE_ANALYSIS_PROMPT["system"]
        user_prompt = PR_PERFORMANCE_ANALYSIS_PROMPT["user"].format(
            org=org,
            repo=repo,
            pr_number=pr_number,
            pr_url=pr_url,
            version=version,
        )
        assistant_prompt = PR_PERFORMANCE_ANALYSIS_PROMPT["assistant"]

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": assistant_prompt},
        ]

        # Clear stale cache before creating new tools for this analysis
        clear_pr_files_cache()

        # Create PR investigation tools and combine with MCP tools
        pr_tools = create_pr_tools(org, repo, pr_number)
        all_tools = mcp_module.mcp_tools + pr_tools

        # Use the agentic loop with tool calling
        result = await analyze_with_agentic(
            messages=messages,
            tools=all_tools,
            max_iterations=PR_ANALYSIS_MAX_TOOL_ITERATIONS,
        )

        # Handle empty results
        if not result:
            logger.warning("Gemini returned empty result for PR analysis")
            return make_response(
                success=False,
                message="No analysis could be generated. Please try again later.",
            )

        # Check if Orion MCP found no performance data
        no_data_indicators = [
            "NO_PERFORMANCE_DATA_FOUND",
            "no data found",
            "no performance data",
            "no test results",
            "no results found",
            "not found",
            "no matching",
        ]

        result_lower = result.lower()
        if any(indicator.lower() in result_lower for indicator in no_data_indicators):
            # Also check if the result is very short (likely just a "no data" message)
            if len(result.strip()) < 200:
                logger.info(
                    "No performance test data found for PR %s/%s#%s",
                    org,
                    repo,
                    pr_number,
                )
                return make_response(
                    success=True,
                    message=(
                        f"No performance test results found for PR #{pr_number}\n\n"
                        f"This could mean:\n"
                        f"- Performance tests haven't run yet for this PR\n"
                        f"- The PR doesn't trigger performance test jobs\n"
                        f"- Test results are not yet available in the Orion database\n\n"
                        f"Check back later or verify that performance tests are configured for this repository."
                    ),
                    pr_info=(org, repo, pr_number, version),
                )

        logger.info("PR analysis completed successfully (%d chars)", len(result))

        # Sanitize output to remove any thinking process before the performance assessment marker
        sanitized_result = _sanitize_gemini_output(result)

        return make_response(
            success=True,
            message=sanitized_result,
            pr_info=(org, repo, pr_number, version),
        )

    except Exception as e:
        error_msg = f"Error analyzing PR: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return make_response(success=False, message=error_msg)
