# Contributing to BugZooka

Thanks for your interest in contributing! This guide will walk you through setting up a local development environment, testing safely in your own Slack channel, and making sure your PR is solid before submitting it.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Fork and Clone](#fork-and-clone)
- [Setting Up Your Development Environment](#setting-up-your-development-environment)
- [Creating Your Own .env File](#creating-your-own-env-file)
- [Setting Up a Test Slack Channel](#setting-up-a-test-slack-channel)
- [Running BugZooka Locally](#running-bugzooka-locally)
- [Running Tests and Checks](#running-tests-and-checks)
- [PR Checklist](#pr-checklist)
- [Code Style](#code-style)
- [Common Pitfalls](#common-pitfalls)

---

## Prerequisites

- Python 3.11 or higher
- pip
- A Slack workspace where you can create apps and channels (a personal or test workspace works great)
- Git

## Fork and Clone

1. Fork the repository on GitHub.
2. Clone your fork locally:
   ```bash
   git clone https://github.com/<your-username>/BugZooka.git
   cd BugZooka
   ```
3. Add the upstream remote so you can keep your fork in sync:
   ```bash
   git remote add upstream https://github.com/<org>/BugZooka.git
   ```

## Setting Up Your Development Environment

```bash
# Create and activate a virtual environment
python -m venv venv
source venv/bin/activate

# Install all dependencies including dev tools (pytest, black, ruff, mypy, pre-commit)
make dev-install

# Set up pre-commit hooks so formatting/linting runs automatically on each commit
pre-commit install
```

## Creating Your Own .env File

BugZooka loads configuration from a `.env` file in the project root. This file is gitignored, so you won't accidentally commit secrets.

Create your `.env` by copying the template below and filling in your values:

```bash
# ---- .env ----

### Mandatory: Slack connection
SLACK_BOT_TOKEN="xoxb-your-bot-token"
SLACK_CHANNEL_ID="C0123456789"
JEDI_BOT_SLACK_USER_ID="U0123456789"

### Optional: Socket Mode (for @ mention support)
SLACK_APP_TOKEN="xapp-your-app-level-token"
ENABLE_SOCKET_MODE="true"

### Inference API (required for LLM-powered analysis)
INFERENCE_URL="https://your-openai-compatible-endpoint"
INFERENCE_TOKEN="your-api-key"
INFERENCE_MODEL="gemini-2.5-pro"

### Optional inference tuning
INFERENCE_VERIFY_SSL="true"
INFERENCE_API_TIMEOUT_SECONDS="120"

### Logging
LOG_LEVEL="DEBUG"
```

**Where do these values come from?** See the next section.

> **Never commit your `.env` file.** It contains secrets. The `.gitignore` already excludes it, but double-check with `git status` before committing.

## Setting Up a Test Slack Channel

Testing against a production channel is risky -- you don't want your development bot spamming real users. Instead, set up your own isolated test environment or feel free to reach out to OCP PerfScale Team to use any of their existing test instances.

### Step 1: Create a Slack App

If you don't already have a test Slack app:

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and click **Create New App**.
2. Choose **From scratch**, give it a name (e.g., "BugZooka Dev - yourname"), and select your test workspace.
3. Under **OAuth & Permissions**, add these Bot Token Scopes:
   - `channels:history` -- read channel messages
   - `channels:read` -- list channels
   - `chat:write` -- post messages
   - `reactions:write` -- add emoji reactions
   - `app_mentions:read` -- respond to @ mentions (needed for Socket Mode)
4. Install the app to your workspace. Copy the **Bot User OAuth Token** (`xoxb-...`) -- this is your `SLACK_BOT_TOKEN`.
5. Find the bot's user ID (visible in the app's profile or via the Slack API) -- this is your `JEDI_BOT_SLACK_USER_ID`.

### Step 2: Enable Socket Mode (optional but recommended)

1. In your Slack app settings, go to **Socket Mode** and enable it.
2. Generate an **App-Level Token** with the `connections:write` scope. Copy it (`xapp-...`) -- this is your `SLACK_APP_TOKEN`.
3. Under **Event Subscriptions**, enable events and subscribe to `app_mention`.

### Step 3: Create a Test Channel

1. In your Slack workspace, create a private or public channel (e.g., `#bugzooka-dev-yourname`).
2. Invite your bot to the channel: `/invite @BugZooka Dev - yourname`.
3. Copy the channel ID (right-click the channel name > "View channel details" > the ID at the bottom). This is your `SLACK_CHANNEL_ID`.

### Step 4: Set Up an Inference Endpoint

BugZooka works with any OpenAI-compatible API. Some free/cheap options for testing:

- **Google Gemini**: Use the Gemini API with an API key from [Google AI Studio](https://aistudio.google.com/)
- **Local models**: Run Ollama or vLLM locally and point `INFERENCE_URL` at `http://localhost:11434/v1`
- **Any OpenAI-compatible provider**: DeepSeek, Together AI, etc.

Set `INFERENCE_URL`, `INFERENCE_TOKEN`, and `INFERENCE_MODEL` accordingly.

> **Tip:** If you're working on a feature that doesn't involve LLM analysis (e.g., Slack message parsing, summarization logic), you can skip the inference config entirely and just omit `--enable-inference` when running.

## Running BugZooka Locally

```bash
# Polling mode only (watches your test channel for messages)
make run

# With LLM analysis enabled
make run ARGS="--enable-inference"

# With Socket Mode (@ mentions) + LLM analysis
make run ARGS="--enable-inference --enable-socket-mode"

# With verbose logging for debugging
make run ARGS="--log-level DEBUG --enable-inference"
```

To test it works:
1. Post a message in your test Slack channel that looks like a CI failure notification.
2. Watch the terminal logs for BugZooka picking it up and processing it.
3. Check the channel for the bot's threaded reply.

For Socket Mode, try `@YourBotName analyze pr: <some-pr-url>, compare with 4.19` in your test channel.

## Running Tests and Checks

Before opening a PR, make sure everything passes locally:

```bash
# Run the full test suite
make test

# Run linting (ruff + mypy)
make lint

# Auto-format your code (black + ruff)
make format

# Run everything at once (lint + test)
make check
```

If you set up pre-commit hooks (`pre-commit install`), formatting and linting will also run automatically when you `git commit`. If a hook fails, it means something needs fixing -- check the output, fix the issue, re-stage, and commit again.

### What CI Checks

When you open a PR, GitHub Actions will run:
- **pylint** with a minimum score threshold of 8/10
- **pytest** to verify tests pass
- **build** to confirm the package installs cleanly

All of these must pass before your PR can be merged.

## PR Checklist

Before opening your pull request, run through this checklist:

- [ ] **Branch is up to date** with `main` (`git pull upstream main && git rebase main`)
- [ ] **`make check` passes** (both linting and tests)
- [ ] **New code has tests** if it adds or changes behavior
- [ ] **Tested in your own Slack channel** for any Slack-facing changes
- [ ] **No secrets in the diff** (`git diff` should not contain tokens, keys, or `.env` content)
- [ ] **PR description explains what and why** -- what problem does this solve? How did you test it?
- [ ] **Commits are clean** -- squash fixup commits, write clear commit messages

## Code Style

This project uses:

| Tool | Purpose | Command |
|------|---------|---------|
| [Black](https://github.com/psf/black) | Code formatting | `make format` |
| [Ruff](https://github.com/astral-sh/ruff) | Linting (replaces flake8, isort) | `make lint` |
| [MyPy](https://mypy-lang.org/) | Static type checking | `make lint` |
| [Pytest](https://docs.pytest.org/) | Testing | `make test` |

General guidelines:
- Follow existing patterns in the codebase. If you're unsure how something should be structured, look at similar code nearby.
- Add type hints to function signatures.
- Keep functions focused -- if a function is doing too many things, split it up.
- Write tests for new functionality. Look at `tests/` for examples of how existing tests are structured.

## Common Pitfalls

**"My bot isn't picking up messages"**
- Make sure the bot is invited to your test channel.
- Verify `SLACK_CHANNEL_ID` matches the channel you're posting in.
- Check that `SLACK_BOT_TOKEN` has the right scopes.
- Look at the terminal logs for errors (run with `--log-level DEBUG`).

**"Inference isn't working"**
- Confirm `INFERENCE_URL` is reachable from your machine.
- If using a self-signed cert, set `INFERENCE_VERIFY_SSL="false"`.
- Check that `INFERENCE_MODEL` is a valid model name for your endpoint.

**"Pre-commit hooks are failing"**
- Run `make format` to auto-fix formatting issues, then re-stage and commit.
- For mypy errors, add type hints or fix type mismatches.

**"Tests pass locally but fail in CI"**
- CI runs pylint with `--fail-under=8`. Run `pylint bugzooka/` locally to check your score.
- Make sure you haven't introduced dependencies that aren't in `requirements.txt`.

---

Questions? Open an issue or reach out to the maintainers listed in the [OWNERS](OWNERS) file.
