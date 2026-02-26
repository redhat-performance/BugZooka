# PR Performance Analysis with Code Context

## Overview

BugZooka's PR analysis compares performance test results from a pull request against baseline metrics, classifies regressions and improvements, and — with the changes on this branch — autonomously investigates the PR's code changes to identify root causes.

**Before this branch:** The LLM receives performance metrics from Orion (via MCP tools) and can say "OVN CPU increased 15%" but has no visibility into the actual code changes, so it cannot explain *why* or recommend specific fixes.

**After this branch:** The LLM gets 9 GitHub investigation tools that let it explore diffs, commits, file contents, and PR metadata. It uses a funnel approach to narrow down from hundreds of changed files to the specific code change that caused a regression.

---

## How PR Analysis Works Today (on `main`)

### Trigger

A user mentions the bot in Slack:

```
@jedi analyze pr: https://github.com/openshift/ovn-kubernetes/pull/4567, compare with 4.19
```

### Pipeline

```
Slack mention
  |
  v
SlackSocketListener._process_mention()        # slack_socket_listener.py
  |  detects "analyze pr" in message text
  v
analyze_pr_with_gemini(text)                   # pr_analyzer.py
  |  1. _parse_pr_request(text) -> org, repo, pr_number, version
  |  2. Initialize MCP client (Orion tools for performance data)
  |  3. Build system/user/assistant prompts
  |  4. Call analyze_with_agentic(messages, tools=mcp_tools)
  v
Agentic loop (inference_client.py)
  |  LLM calls MCP tools to fetch performance metrics
  |  Classifies each metric as regression/improvement/neutral
  |  Up to 5 iterations (INFERENCE_MAX_TOOL_ITERATIONS)
  v
Result posted to Slack thread
  |  Split by "=" separator into multiple messages
  v
Done
```

### Output Format

The LLM produces three sections:

1. **Performance Impact Assessment** — Overall impact (Regression/Improvement/Neutral), significant and moderate changes listed with emojis
2. **Regression Analysis** (only if significant regressions found) — Root cause, impact, recommendations
3. **Most Impacted Metrics** — Tables per config, top 10 metrics sorted by absolute % change

### The Gap

The Regression Analysis section on `main` is sort of okay — the LLM has metrics but no code context, so its root cause analysis is generic ("the changes in this PR may have caused...") rather than specific ("the new `syncFlows()` call in `base_network_controller.go:412` adds a full OVS flow sync on every pod event").

---

## What This Branch Adds

### Problem

Without code context, root cause analysis is guesswork. The LLM cannot:
- Identify which files changed in the PR
- Read the actual diffs to find behavioral changes
- Trace regressions to specific commits in large merge PRs
- Understand the PR's intent to suggest fixes that preserve it

### Solution: 9 GitHub Investigation Tools

The new `pr_tools.py` module provides 9 tools that the LLM can call during analysis. These are combined with the existing MCP tools:

```python
all_tools = mcp_module.mcp_tools + pr_tools  # pr_analyzer.py
```

| Tool | Purpose | When to Use |
|------|---------|-------------|
| `get_pr_changed_files` | List production files changed with +/- counts | **Always first** — determines PR size and scope |
| `get_pr_diff` | Full unified diff (test files excluded) | Small PRs (< 30 files) only |
| `get_file_diff` | Diff for a single file | Large PRs — surgical investigation of suspects |
| `get_pr_description` | PR title, body, labels | Understand intent; avoid naive "revert" recommendations |
| `get_file_content` | Full file from repo at any ref | When diff alone lacks context |
| `get_pr_commits` | Commits in PR, optionally filtered by file | Trace regressions to specific commits |
| `get_pr_comments` | Review comments and discussion | Check if reviewers flagged performance concerns |
| `list_repo_directory` | Directory listing | Navigate repo when paths are unknown |
| `search_related_prs` | Find related PRs by keyword | Historical context for recurring regressions |

### Funnel Investigation Approach

The LLM follows a three-step funnel defined in the system prompt (`prompts.py`):

**Step A — Scope Assessment**

Always call `get_pr_changed_files()` first (no filter) to understand PR size. Test/e2e files are automatically excluded — focus on production files only.

**Step B — Choose Strategy Based on PR Size**

- **Small PR (< 30 files):** Call `get_pr_diff` for a full overview, then `get_pr_description`.
- **Large PR (30+ files):** Do NOT call `get_pr_diff` (it will be truncated). Instead:
  1. Map regressing metrics to code areas using this table:

     | Regressing Metrics | Suspect Paths |
     |---|---|
     | ovnCPU, ovsCPU, ovn-northd, ovn-controller, nbdb, sbdb | `ovn`, `ovs`, `go-controller/pkg/ovn/` |
     | cniLatency, cniPlugin | `cni`, `cmd/cni`, `pkg/cni` |
     | podReadyLatency, podSchedulingLatency | `pod`, `kubelet`, `scheduler` |
     | apiserver, etcd, api-call | `apiserver`, `etcd`, `kube-apiserver` |
     | networkLatency, networkProgramming | `network`, `proxy`, `iptables`, `nft` |

  2. Call `get_pr_changed_files(path_prefix=...)` to narrow to suspect files
  3. Call `get_file_diff` on the top 3-5 suspects
  4. Call `get_pr_commits(file_path=...)` to identify which commits introduced changes
  5. Call `get_pr_description` to understand intent

**Step C — Analysis Rules**

- Focus on **behavioral/semantic changes** (new code paths, altered control flow) not trivial overhead
- Assess whether **regression magnitude is plausible** given scope of changes
- Verify the **regressing subsystem matches** the changed code — if not, explain the causal chain or note weak correlation
- **CRITICAL:** Never attribute regressions to test/e2e file changes — they have zero runtime impact
- **Validate causal mechanisms** — if no plausible chain from code to regression exists, state root cause is unclear rather than forcing an incorrect attribution

### Test File Filtering

Early testing showed the LLM investigating test framework changes (e.g., e2e PSA namespace labels) and incorrectly attributing OVN CPU regressions to them. Defense in depth:

1. **Tool-level filtering** (primary): `get_pr_changed_files` excludes test files from results entirely. `get_pr_diff` also filters them. If the LLM somehow requests a test file diff via `get_file_diff`, a warning is prepended.
2. **Prompt-level guidance** (secondary): The system prompt explicitly instructs the LLM to ignore test files.

Test file patterns (shared constant `TEST_FILE_PATTERNS`):
```
/test/  /tests/  /testdata/  /e2e/
_test.go  _test.py  _test.js  _test.ts
```

### Severity Threshold Change

| Level | Before (main) | After (this branch) |
|---|---|---|
| Significant | >= 10% | >= 8% |
| Moderate | 5% - 10% | 5% - 8% |

Lowered to catch more actionable regressions earlier.

### Increased Tool Iterations

PR analysis gets 15 iterations (`PR_ANALYSIS_MAX_TOOL_ITERATIONS`) vs 5 for generic inference. The funnel approach requires more tool calls: fetch files, map to subsystem, get diffs, trace commits, read description.

---

## Architecture

```
Slack mention ("diff analyze: <PR URL>, compare with <version>")
  |
  v
SlackSocketListener._process_mention()
  |
  v
analyze_pr_with_gemini(text)                          # pr_analyzer.py
  |
  +-- _parse_pr_request(text)
  |     -> org, repo, pr_number, version, pr_url
  |
  +-- clear_pr_files_cache()                          # pr_tools.py
  |     (ensures fresh data for this analysis)
  |
  +-- create_pr_tools(org, repo, pr_number)           # pr_tools.py
  |     -> 9 StructuredTool instances with pre-bound org/repo/pr
  |
  +-- all_tools = mcp_tools + pr_tools
  |
  +-- analyze_with_agentic(messages, all_tools, max_iterations=15)
  |     |                                             # inference_client.py
  |     |   LLM iteration loop:
  |     |     1. LLM sees metrics + tools available
  |     |     2. LLM calls tool (e.g., get_pr_changed_files)
  |     |     3. Tool result returned to LLM
  |     |     4. LLM calls next tool or produces final answer
  |     |     ... up to 15 iterations
  |     |
  |     v
  |   Final response text
  |
  v
Post to Slack thread (split by "=" separator into multiple messages)
```

### Caching

`pr_tools.py` maintains a module-level cache (`_pr_files_cache`) for the GitHub PR files API response. When both `get_pr_changed_files` and `get_file_diff` are called during the same analysis (which is common in the funnel approach), the cache prevents redundant API calls. The cache is cleared via `clear_pr_files_cache()` before each new analysis.

---

## Key Files

| File | Role |
|------|------|
| `bugzooka/analysis/pr_tools.py` | **New.** 9 GitHub investigation tools, PR files cache, test file filtering |
| `bugzooka/analysis/pr_analyzer.py` | Orchestrator: parses Slack request, creates tools, runs agentic analysis |
| `bugzooka/analysis/prompts.py` | System/user/assistant prompts with funnel strategy and classification rules |
| `bugzooka/core/constants.py` | Tuning constants: `PR_ANALYSIS_MAX_TOOL_ITERATIONS`, `MAX_PR_DIFF_SIZE`, `MAX_CHANGED_FILES_RESULTS`, etc. |
| `bugzooka/core/config.py` | `GITHUB_TOKEN` env var for authenticated API access |
| `bugzooka/integrations/slack_socket_listener.py` | Slack trigger detection and response posting |
| `bugzooka/integrations/inference_client.py` | `analyze_with_agentic()` — the LLM tool-calling loop |

---

## Configuration

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GITHUB_TOKEN` | Recommended | GitHub personal access token. Without it, API requests are unauthenticated (60 requests/hour limit vs 5,000 authenticated). PR analysis makes multiple API calls per run, so the unauthenticated limit will be hit quickly. |

### Tuning Constants (`constants.py`)

| Constant | Value | Purpose |
|----------|-------|---------|
| `PR_ANALYSIS_MAX_TOOL_ITERATIONS` | 15 | Max LLM tool-calling rounds for PR analysis |
| `MAX_PR_DIFF_SIZE` | 20000 | Truncate PR diffs at 20K chars |
| `MAX_CHANGED_FILES_RESULTS` | 50 | Show top 50 files by change magnitude |
| `MAX_PR_COMMITS_RESULTS` | 50 | Limit commit listings to 50 |
| `MAX_PR_COMMENTS_SIZE` | 10000 | Truncate PR comments at 10K chars |

### Constants in `pr_tools.py` (could be moved to `constants.py`)

| Constant | Value | Purpose |
|----------|-------|---------|
| `MAX_FILE_CONTENT_SIZE` | 15000 | Truncate file content at 15K chars |
| `GITHUB_API_TIMEOUT` | 30 | HTTP timeout for GitHub API calls (seconds) |
| `MAX_PR_FILES_PAGES` | 30 | Max pagination pages for PR files endpoint |

---

## Remaining Work / Known Issues

- **Trigger phrase mismatch:** The code checks for `"diff analyze"` in the message but the help text tells users to type `"analyze pr:"`. These need to be reconciled. This change was done for testing
- **Unbounded pagination:** `_fetch_pr_commits` has no page limit safety guard, unlike other pagination loops in `pr_tools.py`.
- **Single-page comment fetch:** `_fetch_pr_comments` only fetches the first 100 comments (no pagination). Acceptable given the 10K truncation but inconsistent with other endpoints.
- **Constants location:** Three constants in `pr_tools.py` could be centralized in `constants.py` for consistency.
