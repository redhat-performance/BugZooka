"""
PR Performance Analysis using Gemini with MCP tools.
This module provides functionality to analyze GitHub pull request performance
by orchestrating Gemini AI with MCP tools.
"""
import logging
import re
from typing import Optional, Tuple

from bugzooka.integrations.mcp_client import initialize_global_resources_async
import bugzooka.integrations.mcp_client as mcp_module
from bugzooka.integrations.gemini_client import analyze_with_gemini_agentic
from bugzooka.analysis.prompts import PR_PERFORMANCE_ANALYSIS_PROMPT


logger = logging.getLogger(__name__)


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
    
    This function handles:
    1. Parsing the GitHub PR link and version from text
    2. Validating input format and providing helpful hints
    3. Initializing MCP client and checking for MCP tools
    4. Orchestrating Gemini agentic analysis with tool calling
    5. Returning structured results
    
    :param text: User message text with PR URL and version (both required)
    :return: Dictionary with 'success' (bool), 'message' (str), and optional 'pr_info' (tuple)
    """
    # Parse PR request from text
    parsed = _parse_pr_request(text)
    
    if not parsed:
        # Provide helpful error message with examples
        return {
            "success": False,
            "message": (
                "❌ Invalid PR analysis request format.\n\n"
                "**Required format:**\n"
                "```\n"
                "analyze pr: https://github.com/org/repo/pull/123, compare with 4.19\n"
                "```\n\n"
                "⚠️ Both PR URL and OpenShift version are required!\n\n"
            )
        }
    
    org, repo, pr_number, version = parsed
    logger.info(f"🔍 PR analysis requested for {org}/{repo}/pull/{pr_number} (OpenShift {version})")
    
    if mcp_module.mcp_client is None:
        await initialize_global_resources_async()

    # Check if Orion MCP tools are available
    orion_tool = None
    for tool in mcp_module.mcp_tools:
        if tool.name == "openshift_report_on_pr":
            orion_tool = tool
            break

    if not orion_tool:
        error_msg = "Orion MCP tool 'openshift_report_on_pr' not found. Is the Orion MCP server configured and running?"
        logger.error(error_msg)
        logger.debug("Available tools: %s", [t.name for t in mcp_module.mcp_tools])
        return {
            "success": False,
            "message": f"❌ {error_msg}"
        }

    try:
        logger.info("Starting PR performance analysis: %s/%s#%s (OpenShift %s)", 
                   org, repo, pr_number, version)
        
        # Create prompt for PR analysis using centralized prompts
        pr_url = f"https://github.com/{org}/{repo}/pull/{pr_number}"
        
        system_prompt = PR_PERFORMANCE_ANALYSIS_PROMPT["system"]
        user_prompt = PR_PERFORMANCE_ANALYSIS_PROMPT["user"].format(
            org=org,
            repo=repo,
            pr_number=pr_number,
            pr_url=pr_url,
            version=version
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        # Use the generic agentic loop from gemini_client
        result = await analyze_with_gemini_agentic(
            messages=messages,
            tools=mcp_module.mcp_tools,
            model="gemini-2.5-pro",
        )
        
        # Handle empty results
        if not result:
            logger.warning("Gemini returned empty result for PR analysis")
            return {
                "success": False,
                "message": "⚠️ No analysis could be generated. Please try again later."
            }
        
        # Check if Orion MCP found no performance data
        # Look for various indicators that no data was found
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
                logger.info("No performance test data found for PR %s/%s#%s", org, repo, pr_number)
                return {
                    "success": True,
                    "message": f"📊 No performance test results found for PR #{pr_number}\n\n"
                              f"This could mean:\n"
                              f"• Performance tests haven't run yet for this PR\n"
                              f"• The PR doesn't trigger performance test jobs\n"
                              f"• Test results are not yet available in the Orion database\n\n"
                              f"💡 Check back later or verify that performance tests are configured for this repository.",
                    "pr_info": (org, repo, pr_number, version)
                }
        
        logger.info("PR analysis completed successfully (%d chars)", len(result))
        
        return {
            "success": True,
            "message": result,
            "pr_info": (org, repo, pr_number, version)
        }

    except Exception as e:
        error_msg = f"Error analyzing PR: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return {
            "success": False,
            "message": f"❌ {error_msg}"
        }

