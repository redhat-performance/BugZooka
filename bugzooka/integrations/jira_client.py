"""Jira integration tools."""

import logging
import os
import json
from typing import List, Optional

from fastmcp import FastMCP
import requests

logger = logging.getLogger(__name__)

mcp = FastMCP("jira_tools")


class JiraClient:
    """Jira API client for searching issues."""

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def search_issues(
        self, project_key: str, search_text: str, max_results: int = 10
    ) -> List[dict]:
        """Search for issues in a project by title or description.
        Only returns open/in-progress issues, ordered by most recently updated."""
        jql = (
            f'project = "{project_key}" AND '
            f'(summary ~ "{search_text}" OR description ~ "{search_text}") AND '
            f'status NOT IN (Closed, Resolved, Done, "Won\'t Do", "Won\'t Fix", Obsolete) '
            "ORDER BY updated DESC"
        )

        payload = {
            "jql": jql,
            "maxResults": max_results,
            "fields": [
                "summary",
                "description",
                "status",
                "assignee",
                "reporter",
                "created",
                "updated",
                "key",
            ],
        }

        try:
            response = requests.post(
                f"{self.base_url}/rest/api/2/search",
                headers=self.headers,
                json=payload,
                timeout=30,
            )
            response.raise_for_status()

            # Debug: log the response content
            logger.debug(f"Jira API response status: {response.status_code}")
            logger.debug(f"Jira API response content: {response.text[:500]}...")

            try:
                data = response.json()
            except json.JSONDecodeError as json_error:
                logger.error(f"JSON decode error: {json_error}")
                logger.error(f"Response content: {response.text}")
                raise Exception(f"Invalid JSON response from Jira API: {json_error}")

            # Check if data is None or empty
            if not data:
                logger.error("Empty response from Jira API")
                return []

            issues = []

            # Safely get issues list
            issues_list = data.get("issues", [])
            if not isinstance(issues_list, list):
                logger.error(f"Expected list of issues, got: {type(issues_list)}")
                return []

            for issue in issues_list:
                if not isinstance(issue, dict):
                    logger.warning(f"Skipping non-dict issue: {type(issue)}")
                    continue

                fields = issue.get("fields", {})
                if not isinstance(fields, dict):
                    logger.warning(
                        f"Skipping issue with non-dict fields: {type(fields)}"
                    )
                    continue

                issues.append(
                    {
                        "key": issue.get("key"),
                        "summary": fields.get("summary", ""),
                        "description": fields.get("description", ""),
                        "status": (fields.get("status") or {}).get("name", ""),
                        "assignee": (fields.get("assignee") or {}).get(
                            "displayName", ""
                        ),
                        "reporter": (fields.get("reporter") or {}).get(
                            "displayName", ""
                        ),
                        "created": fields.get("created", ""),
                        "updated": fields.get("updated", ""),
                    }
                )

            return issues

        except requests.exceptions.RequestException as e:
            logger.error(f"Error searching Jira issues: {e}")
            if hasattr(e, "response") and e.response is not None:
                logger.error(f"Response status: {e.response.status_code}")
                logger.error(f"Response content: {e.response.text}")
            raise Exception(f"Failed to search Jira issues: {str(e)}")

    def get_issue(self, issue_key: str) -> Optional[dict]:
        """Get a specific issue by key."""
        try:
            response = requests.get(
                f"{self.base_url}/rest/api/2/issue/{issue_key}",
                headers=self.headers,
                timeout=30,
            )
            response.raise_for_status()

            data = response.json()
            fields = data.get("fields", {})

            return {
                "key": data.get("key"),
                "summary": fields.get("summary", ""),
                "description": fields.get("description", ""),
                "status": (fields.get("status") or {}).get("name", ""),
                "assignee": (fields.get("assignee") or {}).get("displayName", ""),
                "reporter": (fields.get("reporter") or {}).get("displayName", ""),
                "created": fields.get("created", ""),
                "updated": fields.get("updated", ""),
            }

        except requests.exceptions.RequestException as e:
            logger.error(f"Error getting Jira issue {issue_key}: {e}")
            return None


def get_allowed_projects() -> List[str]:
    """Get allowed Jira projects from environment variable.
    Returns:
        List of allowed project keys
    Raises:
        ValueError: If no valid projects are configured
    """
    projects_env = os.environ.get("JIRA_ALLOWED_PROJECTS")
    if not projects_env:
        raise ValueError("JIRA_ALLOWED_PROJECTS environment variable is not set")

    allowed = [p.strip() for p in projects_env.split(",") if p.strip()]

    logger.debug(f"Allowed Jira projects: {allowed}")

    if not allowed:
        raise ValueError("JIRA_ALLOWED_PROJECTS must contain at least one project")

    return allowed


def get_jira_client() -> JiraClient:
    """Get Jira client from environment variables."""
    base_url = os.environ.get("JIRA_BASE_URL")
    token = os.environ.get("JIRA_TOKEN")

    logger.debug(f"JIRA_BASE_URL: {base_url}")
    logger.debug(f"JIRA_TOKEN: {token[:10] if token else 'None'}...")

    if not base_url or not token:
        raise ValueError(
            "Missing Jira credentials: JIRA_BASE_URL and JIRA_TOKEN must be set in environment"
        )

    return JiraClient(base_url, token)


@mcp.tool()
def search_jira_issues(
    project_key: str, search_text: str, max_results: int = 10
) -> str:
    """Search for open Jira issues in a project by title or description.
    Use this tool to find related bugs or known issues that match the error patterns.
    Only returns open/in-progress issues (excludes Closed, Resolved, Done, Won't Do/Fix, Obsolete).
    Results are ordered by most recently updated first.
    Args:
        project_key: The Jira project key to search in
            (must be one of the configured allowed projects)
        search_text: Text to search for in issue summary or description
            (e.g., "etcd degraded", "CrashLoopBackOff", component names)
        max_results: Maximum number of results to return (default: 10)
    Returns:
        JSON string containing matching open issues with their details, ordered by recency
    """
    # Validate project key against allowed projects
    allowed_projects = get_allowed_projects()
    if project_key not in allowed_projects:
        return (
            f"Error: Project '{project_key}' is not in the allowed list. "
            f"Available projects: {', '.join(allowed_projects)}. "
            f"Please use one of these project keys."
        )

    try:
        logger.debug(
            f"search_jira_issues called with project_key={project_key}, search_text={search_text}, max_results={max_results}"
        )
        client = get_jira_client()
        logger.debug(f"Got Jira client: {client}")
        issues = client.search_issues(project_key, search_text, max_results)
        logger.debug(f"Got issues: {issues}")

        if not issues:
            return "No matching issues found."

        # Format the results as a readable list
        formatted_result = f"Found {len(issues)} matching issues:\n\n"

        for i, issue in enumerate(issues, 1):
            formatted_result += f"{i}. **{issue['key']}** - {issue['summary']}\n"
            formatted_result += f"   - Status: {issue['status']}\n"
            formatted_result += f"   - Assignee: {issue['assignee']}\n"
            formatted_result += f"   - Reporter: {issue['reporter']}\n"
            formatted_result += f"   - Created: {issue['created']}\n"
            formatted_result += f"   - Updated: {issue['updated']}\n"

            # Add description if available (truncated for readability)
            if issue["description"]:
                desc = (
                    issue["description"][:200] + "..."
                    if len(issue["description"]) > 500
                    else issue["description"]
                )
                formatted_result += f" {desc}\n"

            formatted_result += "\n"

        return formatted_result

    except Exception as e:
        logger.error(f"Error in search_jira_issues: {e}")
        return f"Error searching Jira issues: {str(e)}"


@mcp.tool()
def get_jira_issue(issue_key: str) -> str:
    """Get a specific Jira issue by its key.
    Args:
        issue_key: The Jira issue key (e.g., 'PROJ-123')
    Returns:
        JSON string containing the issue details
    """
    try:
        client = get_jira_client()
        issue = client.get_issue(issue_key)

        if not issue:
            return f"Issue {issue_key} not found or access denied."

        # Format the issue as a readable string
        formatted_result = f"**{issue['key']}** - {issue['summary']}\n\n"
        formatted_result += f"**Status:** {issue['status']}\n"
        formatted_result += f"**Assignee:** {issue['assignee']}\n"
        formatted_result += f"**Reporter:** {issue['reporter']}\n"
        formatted_result += f"**Created:** {issue['created']}\n"
        formatted_result += f"**Updated:** {issue['updated']}\n\n"

        if issue["description"]:
            formatted_result += f"**Description:**\n{issue['description']}\n"

        return formatted_result

    except Exception as e:
        logger.error(f"Error in get_jira_issue: {e}")
        return f"Error getting Jira issue: {str(e)}"


if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)
