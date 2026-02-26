"""
GitHub investigation tools for agentic PR analysis.

Provides LangChain tools that allow the LLM to autonomously investigate
PR code changes, descriptions, and file contents during performance analysis.
Supports both small PRs (full diff) and large downstream merges (funnel approach).
"""
import base64
import logging
import re
import threading

import requests
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from bugzooka.core.config import GITHUB_TOKEN
from bugzooka.core.constants import (
    MAX_PR_DIFF_SIZE,
    MAX_CHANGED_FILES_RESULTS,
    MAX_PR_COMMITS_RESULTS,
    MAX_PR_COMMENTS_SIZE,
)

logger = logging.getLogger(__name__)

MAX_FILE_CONTENT_SIZE = 15000
GITHUB_API_TIMEOUT = 30
MAX_PR_FILES_PAGES = 30

# Patterns identifying test/e2e files — shared by diff filter and changed-files filter
TEST_FILE_PATTERNS = (
    "/test/",
    "/tests/",
    "/testdata/",
    "/e2e/",
    "_test.go",
    "_test.py",
    "_test.js",
    "_test.ts",
)


def _is_test_file(filename: str) -> bool:
    """Check if a filename matches test/e2e file patterns."""
    return any(pattern in filename for pattern in TEST_FILE_PATTERNS)


# Module-level cache for PR files API responses (shared by changed_files and file_diff)
_pr_files_cache: dict = {}
_pr_files_cache_lock = threading.Lock()


def clear_pr_files_cache() -> None:
    """Clear the PR files cache to ensure fresh data on re-analysis."""
    with _pr_files_cache_lock:
        _pr_files_cache.clear()
    logger.info("Cleared PR files cache")


def _github_headers(accept: str = "application/vnd.github.v3+json") -> dict:
    """Build standard GitHub API headers with optional authentication."""
    headers = {
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"
    return headers


def _get_cached_pr_files(org: str, repo: str, pr_number: str) -> list:
    """
    Paginate and cache all PR file entries from GitHub API.

    Uses module-level cache to avoid redundant API calls when both
    get_pr_changed_files and get_file_diff are called in the same analysis.

    :param org: GitHub organization/owner
    :param repo: Repository name
    :param pr_number: Pull request number
    :return: List of file entry dicts from GitHub API
    """
    cache_key = f"{org}/{repo}/{pr_number}"
    with _pr_files_cache_lock:
        if cache_key in _pr_files_cache:
            logger.debug("Using cached PR files for %s", cache_key)
            return _pr_files_cache[cache_key]

    all_files = []
    page = 1
    url = f"https://api.github.com/repos/{org}/{repo}/pulls/{pr_number}/files"
    headers = _github_headers()

    while True:
        response = requests.get(
            url,
            headers=headers,
            params={"per_page": 100, "page": page},
            timeout=GITHUB_API_TIMEOUT,
        )
        response.raise_for_status()
        batch = response.json()
        all_files.extend(batch)
        if len(batch) < 100:
            break
        page += 1
        if page > MAX_PR_FILES_PAGES:
            logger.warning(
                "Pagination limit reached for %s/%s#%s: stopped at %d pages (%d files)",
                org,
                repo,
                pr_number,
                MAX_PR_FILES_PAGES,
                len(all_files),
            )
            break

    with _pr_files_cache_lock:
        _pr_files_cache[cache_key] = all_files
    logger.info(
        "Cached %d changed files for %s/%s#%s",
        len(all_files),
        org,
        repo,
        pr_number,
    )
    return all_files


# =============================================================================
# Private fetch functions
# =============================================================================


def _filter_test_files_from_diff(diff_text: str) -> str:
    """
    Filter out test file sections from a unified diff.

    Removes diff hunks for files matching common test patterns
    (test/, tests/, *_test.go, *_test.py, testdata/, e2e/) to focus
    the analysis on production code that affects runtime performance.

    :param diff_text: Raw unified diff text
    :return: Filtered diff with test file sections removed
    """
    sections = re.split(r"(?=^diff --git )", diff_text, flags=re.MULTILINE)
    filtered = []
    skipped = 0

    for section in sections:
        if not section.strip():
            continue
        header_line = section.split("\n", 1)[0]
        if any(pattern in header_line for pattern in TEST_FILE_PATTERNS):
            skipped += 1
            continue
        filtered.append(section)

    if skipped:
        logger.info("Filtered %d test file(s) from PR diff", skipped)

    return "".join(filtered)


def _fetch_pr_diff(org: str, repo: str, pr_number: str) -> str:
    """
    Fetch the unified diff for a GitHub pull request.

    :param org: GitHub organization/owner
    :param repo: Repository name
    :param pr_number: Pull request number
    :return: The PR diff text, possibly truncated, or error message on failure
    """
    url = f"https://api.github.com/repos/{org}/{repo}/pulls/{pr_number}"
    headers = _github_headers("application/vnd.github.v3.diff")

    try:
        response = requests.get(url, headers=headers, timeout=GITHUB_API_TIMEOUT)
        response.raise_for_status()

        diff_text = _filter_test_files_from_diff(response.text)

        if not diff_text.strip():
            logger.info(
                "PR diff for %s/%s#%s contains only test files",
                org,
                repo,
                pr_number,
            )
            return "No production code changes found (only test files were modified)."

        filtered_size = len(diff_text)
        if filtered_size > MAX_PR_DIFF_SIZE:
            logger.info(
                "PR diff for %s/%s#%s truncated from %d to %d characters",
                org,
                repo,
                pr_number,
                filtered_size,
                MAX_PR_DIFF_SIZE,
            )
            diff_text = (
                diff_text[:MAX_PR_DIFF_SIZE]
                + f"\n\n... [DIFF TRUNCATED - showing first {MAX_PR_DIFF_SIZE}"
                + f" characters of {filtered_size} total] ..."
            )

        logger.info(
            "Fetched PR diff for %s/%s#%s (%d characters, production code only)",
            org,
            repo,
            pr_number,
            len(diff_text),
        )
        return diff_text

    except requests.exceptions.RequestException as e:
        logger.warning(
            "Failed to fetch PR diff for %s/%s#%s: %s",
            org,
            repo,
            pr_number,
            e,
        )
        return f"Error fetching PR diff: {e}"


def _fetch_pr_description(org: str, repo: str, pr_number: str) -> str:
    """
    Fetch PR title, body, and labels via GitHub API.

    :param org: GitHub organization/owner
    :param repo: Repository name
    :param pr_number: Pull request number
    :return: Formatted string with PR title, description, and labels
    """
    url = f"https://api.github.com/repos/{org}/{repo}/pulls/{pr_number}"
    headers = _github_headers()

    try:
        response = requests.get(url, headers=headers, timeout=GITHUB_API_TIMEOUT)
        response.raise_for_status()

        pr_data = response.json()
        title = pr_data.get("title", "No title")
        body = pr_data.get("body", "No description provided.")
        labels = [label["name"] for label in pr_data.get("labels", [])]

        result = f"**Title:** {title}\n\n"
        result += f"**Description:**\n{body or 'No description provided.'}\n\n"
        if labels:
            result += f"**Labels:** {', '.join(labels)}\n"

        logger.info(
            "Fetched PR description for %s/%s#%s",
            org,
            repo,
            pr_number,
        )
        return result

    except requests.exceptions.RequestException as e:
        logger.warning(
            "Failed to fetch PR description for %s/%s#%s: %s",
            org,
            repo,
            pr_number,
            e,
        )
        return f"Error fetching PR description: {e}"


def _fetch_file_content(
    org: str,
    repo: str,
    file_path: str,
    ref: str = "main",
) -> str:
    """
    Fetch a specific file from the repo at a given ref/branch via GitHub API.

    :param org: GitHub organization/owner
    :param repo: Repository name
    :param file_path: Path to the file within the repository
    :param ref: Git ref (branch, tag, or commit SHA). Defaults to 'main'.
    :return: File content (decoded from base64), truncated if too large
    """
    url = f"https://api.github.com/repos/{org}/{repo}/contents/{file_path}"
    headers = _github_headers()
    params = {"ref": ref}

    try:
        response = requests.get(
            url, headers=headers, params=params, timeout=GITHUB_API_TIMEOUT
        )
        response.raise_for_status()

        data = response.json()
        content_b64 = data.get("content", "")
        try:
            content = base64.b64decode(content_b64).decode("utf-8")
        except (UnicodeDecodeError, ValueError) as e:
            logger.warning("File %s is not valid UTF-8 text: %s", file_path, e)
            return (
                f"Error: File '{file_path}' appears to be binary or non-UTF-8 encoded."
            )

        if len(content) > MAX_FILE_CONTENT_SIZE:
            original_size = len(content)
            logger.info(
                "File %s truncated from %d to %d characters",
                file_path,
                original_size,
                MAX_FILE_CONTENT_SIZE,
            )
            content = (
                content[:MAX_FILE_CONTENT_SIZE]
                + f"\n\n... [FILE TRUNCATED - showing first {MAX_FILE_CONTENT_SIZE}"
                + f" characters of {original_size} total] ..."
            )

        logger.info(
            "Fetched file %s from %s/%s@%s (%d characters)",
            file_path,
            org,
            repo,
            ref,
            len(content),
        )
        return content

    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 403:
            logger.warning(
                "File %s in %s/%s@%s exceeds GitHub API size limit (1MB)",
                file_path,
                org,
                repo,
                ref,
            )
            return (
                f"Error: File '{file_path}' exceeds GitHub's 1MB content API limit. "
                f"Use get_file_diff to view the PR changes for this file instead."
            )
        logger.warning(
            "Failed to fetch file %s from %s/%s@%s: %s",
            file_path,
            org,
            repo,
            ref,
            e,
        )
        return f"Error fetching file: {e}"
    except requests.exceptions.RequestException as e:
        logger.warning(
            "Failed to fetch file %s from %s/%s@%s: %s",
            file_path,
            org,
            repo,
            ref,
            e,
        )
        return f"Error fetching file: {e}"


def _fetch_pr_changed_files(
    org: str,
    repo: str,
    pr_number: str,
    path_prefix: str = "",
) -> str:
    """
    Fetch list of all changed files in a PR with change statistics.

    :param org: GitHub organization/owner
    :param repo: Repository name
    :param pr_number: Pull request number
    :param path_prefix: Optional prefix to filter files (e.g., 'go-controller/pkg/ovn/')
    :return: Formatted list of changed files sorted by change magnitude
    """
    try:
        all_files = _get_cached_pr_files(org, repo, pr_number)
    except requests.exceptions.RequestException as e:
        logger.warning(
            "Failed to fetch changed files for %s/%s#%s: %s",
            org,
            repo,
            pr_number,
            e,
        )
        return f"Error fetching changed files: {e}"

    total_count = len(all_files)

    if path_prefix:
        filtered = [f for f in all_files if f["filename"].startswith(path_prefix)]
    else:
        filtered = list(all_files)

    # Separate test files from production files
    test_files = [f for f in filtered if _is_test_file(f["filename"])]
    production_files = [f for f in filtered if not _is_test_file(f["filename"])]

    # Sort production files by change magnitude (additions + deletions) descending
    production_files.sort(
        key=lambda f: f.get("additions", 0) + f.get("deletions", 0),
        reverse=True,
    )

    # Truncate to limit
    shown = production_files[:MAX_CHANGED_FILES_RESULTS]

    lines = []
    if path_prefix:
        lines.append(
            f"Production files changed: {len(production_files)} "
            f"(of {total_count} total, matching prefix '{path_prefix}')"
        )
    else:
        lines.append(
            f"Production files changed: {len(production_files)} (of {total_count} total)"
        )

    if test_files:
        lines.append(
            f"({len(test_files)} test/e2e files excluded "
            f"— test files do not affect runtime performance)"
        )

    if not shown:
        lines.append("\nNo production files found matching the filter.")
        return "\n".join(lines)

    lines.append(f"\nTop {len(shown)} production files by change magnitude:")
    for f in shown:
        status = f.get("status", "modified")
        additions = f.get("additions", 0)
        deletions = f.get("deletions", 0)
        filename = f["filename"]
        lines.append(f"  {status:10s} +{additions:<5d} -{deletions:<5d}  {filename}")

    if len(production_files) > MAX_CHANGED_FILES_RESULTS:
        lines.append(
            f"\n  ... and {len(production_files) - MAX_CHANGED_FILES_RESULTS} more production files"
        )

    logger.info(
        "Listed %d/%d production files for %s/%s#%s (prefix: '%s', %d test files excluded)",
        len(shown),
        total_count,
        org,
        repo,
        pr_number,
        path_prefix,
        len(test_files),
    )
    return "\n".join(lines)


def _fetch_file_diff(
    org: str,
    repo: str,
    pr_number: str,
    file_path: str,
) -> str:
    """
    Fetch the diff (patch) for a specific file within a PR.

    :param org: GitHub organization/owner
    :param repo: Repository name
    :param pr_number: Pull request number
    :param file_path: Exact file path to get the diff for
    :return: The diff patch for the specified file, or error message
    """
    try:
        all_files = _get_cached_pr_files(org, repo, pr_number)
    except requests.exceptions.RequestException as e:
        logger.warning(
            "Failed to fetch file diff for %s in %s/%s#%s: %s",
            file_path,
            org,
            repo,
            pr_number,
            e,
        )
        return f"Error fetching file diff: {e}"

    # Find the file entry
    file_entry = next((f for f in all_files if f["filename"] == file_path), None)

    if not file_entry:
        return (
            f"File '{file_path}' not found in PR changes. "
            f"Use get_pr_changed_files to see available files."
        )

    patch = file_entry.get("patch")
    if not patch:
        additions = file_entry.get("additions", 0)
        deletions = file_entry.get("deletions", 0)
        return (
            f"Diff not available for '{file_path}' "
            f"(+{additions}/-{deletions} changes). "
            f"GitHub omits patches for very large or binary files. "
            f"Use get_file_content to inspect the full file instead."
        )

    if len(patch) > MAX_PR_DIFF_SIZE:
        original_size = len(patch)
        patch = (
            patch[:MAX_PR_DIFF_SIZE]
            + f"\n\n... [DIFF TRUNCATED - showing first {MAX_PR_DIFF_SIZE}"
            + f" characters of {original_size} total] ..."
        )

    # Prepend warning if this is a test/e2e file
    if _is_test_file(file_path):
        warning = (
            "NOTE: This is a test/e2e file. Changes to test files do NOT affect runtime\n"
            "performance of production components like OVN, OVS, or kubelet. Do not\n"
            "attribute performance regressions to test file changes.\n\n"
        )
        patch = warning + patch

    logger.info(
        "Fetched file diff for %s in %s/%s#%s (%d characters)",
        file_path,
        org,
        repo,
        pr_number,
        len(patch),
    )
    return patch


def _fetch_pr_commits(
    org: str,
    repo: str,
    pr_number: str,
    file_path: str = "",
) -> str:
    """
    Fetch commits for a PR, optionally filtered by file path.

    :param org: GitHub organization/owner
    :param repo: Repository name
    :param pr_number: Pull request number
    :param file_path: Optional file path to filter commits touching that file
    :return: Formatted list of commits
    """
    headers = _github_headers()

    try:
        if file_path:
            # First get PR head SHA
            pr_url = f"https://api.github.com/repos/{org}/{repo}/pulls/{pr_number}"
            pr_response = requests.get(
                pr_url, headers=headers, timeout=GITHUB_API_TIMEOUT
            )
            pr_response.raise_for_status()
            head_sha = pr_response.json()["head"]["sha"]

            # Get commits touching the specific file on this branch
            commits_url = f"https://api.github.com/repos/{org}/{repo}/commits"
            response = requests.get(
                commits_url,
                headers=headers,
                params={"sha": head_sha, "path": file_path, "per_page": 20},
                timeout=GITHUB_API_TIMEOUT,
            )
            response.raise_for_status()
            commits = response.json()

            lines = [
                f"Recent commits touching '{file_path}' ({len(commits)} found):",
                "Note: results may include commits from before this PR on the same branch. "
                "Cross-reference with the PR's commit list for accuracy.",
            ]
        else:
            # Get all PR commits (paginated, up to 250)
            all_commits = []
            page = 1
            while True:
                commits_url = (
                    f"https://api.github.com/repos/{org}/{repo}"
                    f"/pulls/{pr_number}/commits"
                )
                response = requests.get(
                    commits_url,
                    headers=headers,
                    params={"per_page": 100, "page": page},
                    timeout=GITHUB_API_TIMEOUT,
                )
                response.raise_for_status()
                batch = response.json()
                all_commits.extend(batch)
                if len(batch) < 100:
                    break
                page += 1

            commits = all_commits[:MAX_PR_COMMITS_RESULTS]
            total = len(all_commits)

            if total >= 250:
                lines = [
                    f"PR commits (showing {len(commits)} of 250+ — "
                    f"GitHub API limit. Use file_path filter for targeted results):"
                ]
            else:
                lines = [f"PR commits ({total} total, showing {len(commits)}):"]

        lines.append("")
        for c in commits:
            sha_short = c["sha"][:7]
            message = c["commit"]["message"].split("\n", 1)[0][:100]
            author = c["commit"]["author"]["name"]
            date = c["commit"]["author"]["date"][:10]
            lines.append(f"  {sha_short}  {date}  {author}  {message}")

        logger.info(
            "Fetched %d commits for %s/%s#%s (file_path: '%s')",
            len(commits),
            org,
            repo,
            pr_number,
            file_path,
        )
        return "\n".join(lines)

    except requests.exceptions.RequestException as e:
        logger.warning(
            "Failed to fetch commits for %s/%s#%s: %s",
            org,
            repo,
            pr_number,
            e,
        )
        return f"Error fetching commits: {e}"


def _fetch_pr_comments(org: str, repo: str, pr_number: str) -> str:
    """
    Fetch review comments and discussion comments for a PR.

    :param org: GitHub organization/owner
    :param repo: Repository name
    :param pr_number: Pull request number
    :return: Formatted comments sorted by date
    """
    headers = _github_headers()
    all_comments = []

    # Review comments (on specific lines of code)
    try:
        review_url = (
            f"https://api.github.com/repos/{org}/{repo}" f"/pulls/{pr_number}/comments"
        )
        response = requests.get(
            review_url,
            headers=headers,
            params={"per_page": 100},
            timeout=GITHUB_API_TIMEOUT,
        )
        response.raise_for_status()
        for c in response.json():
            all_comments.append(
                {
                    "type": "review",
                    "author": c["user"]["login"],
                    "path": c.get("path", ""),
                    "body": c["body"],
                    "date": c["created_at"][:10],
                }
            )
    except requests.exceptions.RequestException as e:
        logger.warning(
            "Failed to fetch review comments for %s/%s#%s: %s",
            org,
            repo,
            pr_number,
            e,
        )

    # Issue/discussion comments (general PR comments)
    try:
        issue_url = (
            f"https://api.github.com/repos/{org}/{repo}" f"/issues/{pr_number}/comments"
        )
        response = requests.get(
            issue_url,
            headers=headers,
            params={"per_page": 100},
            timeout=GITHUB_API_TIMEOUT,
        )
        response.raise_for_status()
        for c in response.json():
            all_comments.append(
                {
                    "type": "comment",
                    "author": c["user"]["login"],
                    "path": "",
                    "body": c["body"],
                    "date": c["created_at"][:10],
                }
            )
    except requests.exceptions.RequestException as e:
        logger.warning(
            "Failed to fetch issue comments for %s/%s#%s: %s",
            org,
            repo,
            pr_number,
            e,
        )

    if not all_comments:
        return "No comments found on this PR."

    # Sort by date descending
    all_comments.sort(key=lambda c: c["date"], reverse=True)

    lines = [f"PR comments ({len(all_comments)} total):", ""]
    total_chars = 0
    for c in all_comments:
        if c["type"] == "review" and c["path"]:
            header = f"--- Review by {c['author']} on {c['path']} ({c['date']}) ---"
        else:
            header = f"--- Comment by {c['author']} ({c['date']}) ---"
        entry = f"{header}\n{c['body']}\n"
        total_chars += len(entry)
        if total_chars > MAX_PR_COMMENTS_SIZE:
            lines.append(f"... [TRUNCATED — showing first {len(lines) - 2} comments]")
            break
        lines.append(entry)

    logger.info(
        "Fetched %d comments for %s/%s#%s",
        len(all_comments),
        org,
        repo,
        pr_number,
    )
    return "\n".join(lines)


def _fetch_repo_directory(
    org: str,
    repo: str,
    path: str,
    ref: str = "main",
) -> str:
    """
    List contents of a directory in the repository.

    :param org: GitHub organization/owner
    :param repo: Repository name
    :param path: Directory path within the repository
    :param ref: Git ref (branch, tag, or commit SHA). Defaults to 'main'.
    :return: Formatted directory listing
    """
    url = f"https://api.github.com/repos/{org}/{repo}/contents/{path}"
    headers = _github_headers()
    params = {"ref": ref}

    try:
        response = requests.get(
            url, headers=headers, params=params, timeout=GITHUB_API_TIMEOUT
        )
        response.raise_for_status()

        data = response.json()
        if not isinstance(data, list):
            return (
                f"'{path}' is a file, not a directory. Use get_file_content to read it."
            )

        # Sort: directories first, then files, each alphabetically
        dirs = sorted([e for e in data if e["type"] == "dir"], key=lambda e: e["name"])
        files = sorted([e for e in data if e["type"] != "dir"], key=lambda e: e["name"])

        lines = [f"Contents of {path} (ref: {ref}):", ""]
        for d in dirs:
            lines.append(f"  [dir]   {d['name']}/")
        for f in files:
            size = f.get("size", 0)
            lines.append(f"  [file]  {f['name']}  ({size} bytes)")

        logger.info(
            "Listed directory %s from %s/%s@%s (%d entries)",
            path,
            org,
            repo,
            ref,
            len(data),
        )
        return "\n".join(lines)

    except requests.exceptions.RequestException as e:
        logger.warning(
            "Failed to list directory %s from %s/%s@%s: %s",
            path,
            org,
            repo,
            ref,
            e,
        )
        return f"Error listing directory: {e}"


def _search_related_prs(org: str, repo: str, query: str) -> str:
    """
    Search for related PRs in the repository.

    :param org: GitHub organization/owner
    :param repo: Repository name
    :param query: Search query (e.g., 'OVN CPU regression')
    :return: Formatted list of matching PRs
    """
    url = "https://api.github.com/search/issues"
    headers = _github_headers()
    full_query = f"type:pr repo:{org}/{repo} {query}"

    try:
        response = requests.get(
            url,
            headers=headers,
            params={"q": full_query, "sort": "updated", "per_page": "5"},
            timeout=GITHUB_API_TIMEOUT,
        )
        response.raise_for_status()

        data = response.json()
        items = data.get("items", [])

        if not items:
            return f"No related PRs found for query: '{query}'"

        lines = [
            f"Related PRs for '{query}' ({data.get('total_count', 0)} total, showing top 5):",
            "",
        ]
        for item in items:
            pr_num = item["number"]
            title = item["title"][:80]
            state = item["state"]
            date = item["created_at"][:10]
            lines.append(f"  #{pr_num}  [{state}]  {date}  {title}")

        logger.info(
            "Found %d related PRs for query '%s' in %s/%s",
            len(items),
            query,
            org,
            repo,
        )
        return "\n".join(lines)

    except requests.exceptions.RequestException as e:
        logger.warning(
            "Failed to search related PRs in %s/%s: %s",
            org,
            repo,
            e,
        )
        return f"Error searching PRs: {e}"


# =============================================================================
# Pydantic schemas for tool inputs
# =============================================================================


class FileContentInput(BaseModel):
    """Input schema for the get_file_content tool."""

    file_path: str = Field(
        description="Path to the file within the repository (e.g., 'go-controller/pkg/ovn/base_network_controller.go')"
    )
    ref: str = Field(
        default="main",
        description="Git ref (branch, tag, or commit SHA). Defaults to 'main'.",
    )


class ChangedFilesInput(BaseModel):
    """Input schema for the get_pr_changed_files tool."""

    path_prefix: str = Field(
        default="",
        description=(
            "Optional path prefix to filter files. "
            "Example: 'go-controller/pkg/ovn/' to see only OVN-related changes. "
            "Leave empty to see all changed files."
        ),
    )


class FileDiffInput(BaseModel):
    """Input schema for the get_file_diff tool."""

    file_path: str = Field(
        description="Exact file path as shown by get_pr_changed_files (e.g., 'go-controller/pkg/ovn/base_network_controller.go')"
    )


class PRCommitsInput(BaseModel):
    """Input schema for the get_pr_commits tool."""

    file_path: str = Field(
        default="",
        description=(
            "Optional file path to filter commits. "
            "When provided, returns only commits that touched this specific file. "
            "Leave empty to get all PR commits."
        ),
    )


class ListDirectoryInput(BaseModel):
    """Input schema for the list_repo_directory tool."""

    path: str = Field(
        description="Directory path within the repository (e.g., 'go-controller/pkg/ovn/')"
    )
    ref: str = Field(
        default="main",
        description="Git ref (branch, tag, or commit SHA). Defaults to 'main'.",
    )


class SearchPRsInput(BaseModel):
    """Input schema for the search_related_prs tool."""

    query: str = Field(
        description="Search query for finding related PRs (e.g., 'OVN CPU regression' or 'IPAM performance')"
    )


# =============================================================================
# Tool factory
# =============================================================================


def create_pr_tools(org: str, repo: str, pr_number: str) -> list:
    """
    Create LangChain tools for PR investigation with pre-bound parameters.

    Returns tools where org, repo, and pr_number are already bound,
    so the LLM doesn't need to pass them as arguments.

    :param org: GitHub organization/owner
    :param repo: Repository name
    :param pr_number: Pull request number
    :return: List of LangChain StructuredTool instances
    """

    # --- Existing tool closures ---

    def get_pr_diff() -> str:
        """Fetch the full production code diff for the pull request. Test files are excluded."""
        return _fetch_pr_diff(org, repo, pr_number)

    def get_pr_description() -> str:
        """Fetch the PR title, description, and labels to understand the intent of the changes."""
        return _fetch_pr_description(org, repo, pr_number)

    def get_file_content(file_path: str, ref: str = "main") -> str:
        """Fetch the full content of a specific file from the repository."""
        return _fetch_file_content(org, repo, file_path, ref)

    # --- New tool closures ---

    def get_pr_changed_files(path_prefix: str = "") -> str:
        """Get the list of all files changed in this PR with change statistics."""
        return _fetch_pr_changed_files(org, repo, pr_number, path_prefix)

    def get_file_diff(file_path: str) -> str:
        """Fetch the diff for a specific file in the PR."""
        return _fetch_file_diff(org, repo, pr_number, file_path)

    def get_pr_commits(file_path: str = "") -> str:
        """List commits in the PR, optionally filtered to those touching a specific file."""
        return _fetch_pr_commits(org, repo, pr_number, file_path)

    def get_pr_comments() -> str:
        """Fetch review comments and discussion from the PR."""
        return _fetch_pr_comments(org, repo, pr_number)

    def list_repo_directory(path: str, ref: str = "main") -> str:
        """List contents of a directory in the repository."""
        return _fetch_repo_directory(org, repo, path, ref)

    def search_related_prs(query: str) -> str:
        """Search for related PRs in this repository."""
        return _search_related_prs(org, repo, query)

    # --- Tool definitions ---

    diff_tool = StructuredTool.from_function(
        func=get_pr_diff,
        name="get_pr_diff",
        description=(
            "Fetch the full production code diff for the pull request. "
            "Returns the unified diff with test files excluded. "
            "Best for small PRs (< 30 files). For large PRs, use "
            "get_pr_changed_files + get_file_diff instead."
        ),
    )

    description_tool = StructuredTool.from_function(
        func=get_pr_description,
        name="get_pr_description",
        description=(
            "Fetch the PR title, description, and labels. "
            "Use this to understand the intent and purpose of the changes, "
            "which helps produce better recommendations that preserve the PR's goals."
        ),
    )

    file_content_tool = StructuredTool.from_function(
        func=get_file_content,
        name="get_file_content",
        description=(
            "Fetch the full content of a specific file from the repository. "
            "Use this to inspect complete function implementations referenced in the diff, "
            "when the diff alone doesn't show enough context to understand behavior changes."
        ),
        args_schema=FileContentInput,
    )

    changed_files_tool = StructuredTool.from_function(
        func=get_pr_changed_files,
        name="get_pr_changed_files",
        description=(
            "Get the list of production files changed in the PR with addition/deletion counts. "
            "Test/e2e files are automatically excluded (they don't affect runtime performance). "
            "Call this FIRST to understand the scope of changes and determine PR size. "
            "Use path_prefix to filter by subsystem (e.g., 'go-controller/pkg/ovn/'). "
            "Results are sorted by change magnitude (most-changed files first)."
        ),
        args_schema=ChangedFilesInput,
    )

    file_diff_tool = StructuredTool.from_function(
        func=get_file_diff,
        name="get_file_diff",
        description=(
            "Fetch the diff (patch) for a specific file in the PR. "
            "Use after get_pr_changed_files identifies suspect files. "
            "Much more targeted than get_pr_diff for large PRs with many files."
        ),
        args_schema=FileDiffInput,
    )

    commits_tool = StructuredTool.from_function(
        func=get_pr_commits,
        name="get_pr_commits",
        description=(
            "List commits in the PR. Use file_path to filter to commits "
            "touching a specific file — essential for tracing regressions "
            "to specific commits in large merge PRs."
        ),
        args_schema=PRCommitsInput,
    )

    comments_tool = StructuredTool.from_function(
        func=get_pr_comments,
        name="get_pr_comments",
        description=(
            "Fetch review comments and discussion from the PR. "
            "Use this to check if reviewers flagged performance concerns "
            "or discussed tradeoffs relevant to the regression."
        ),
    )

    directory_tool = StructuredTool.from_function(
        func=list_repo_directory,
        name="list_repo_directory",
        description=(
            "List contents of a directory in the repository. "
            "Use this to navigate the repo structure when you need to find "
            "related files but don't know exact paths."
        ),
        args_schema=ListDirectoryInput,
    )

    related_prs_tool = StructuredTool.from_function(
        func=search_related_prs,
        name="search_related_prs",
        description=(
            "Search for related PRs in this repository by keyword. "
            "Use this to find historical regressions or related changes "
            "in the same area (e.g., 'OVN CPU regression')."
        ),
        args_schema=SearchPRsInput,
    )

    return [
        changed_files_tool,
        diff_tool,
        file_diff_tool,
        description_tool,
        file_content_tool,
        commits_tool,
        comments_tool,
        directory_tool,
        related_prs_tool,
    ]
