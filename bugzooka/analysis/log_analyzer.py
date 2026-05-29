import logging
import asyncio
import os
import tempfile
from pydantic import BaseModel, Field

from langchain_core.tools import StructuredTool
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from bugzooka.core.constants import MAX_CONTEXT_SIZE
from bugzooka.analysis.prompts import ERROR_FILTER_PROMPT, JIRA_TOOL_PROMPT
from bugzooka.analysis.log_summarizer import (
    download_prow_logs,
    generate_prompt,
    find_pod_latency_file,
    download_pod_latency_file,
    parse_slowest_pod,
    download_node_journal,
)
from bugzooka.analysis.node_log_analyzer import analyze_node_journal, format_result_markdown
from bugzooka.integrations.inference_client import (
    get_inference_client,
    analyze_with_agentic,
    AgentAnalysisLimitExceededError,
    InferenceAPIUnavailableError,
)
from bugzooka.integrations import mcp_client as mcp_module
from bugzooka.integrations.mcp_client import initialize_global_resources_async
from bugzooka.core.config import get_prompt_config
from bugzooka.analysis.prow_analyzer import analyze_prow_artifacts, ProwAnalysisResult
from bugzooka.core.utils import extract_job_details, extract_gcs_path
from bugzooka.analysis.log_summarizer import get_prow_inner_artifact_files

logger = logging.getLogger(__name__)


def _with_retry(func):
    """Decorator that adds retry logic using the inference client's retry config."""
    config = get_inference_client().retry_config
    return retry(
        stop=stop_after_attempt(config["max_attempts"]),
        wait=wait_exponential(
            multiplier=config["backoff"],
            min=config["delay"],
            max=config["max_delay"],
        ),
        retry=retry_if_exception_type(
            (InferenceAPIUnavailableError, AgentAnalysisLimitExceededError)
        ),
        reraise=True,
    )(func)


class SingleStringInput(BaseModel):
    """Schema for tools that accept a single string argument."""

    query: str = Field(description="The full error summary text to analyze.")


async def analyze_log_with_tools(prompt_config: dict, error_summary: str, tools=None):
    """
    Analyzes log summaries using an LLM with prompts and optional tool calling.

    :param prompt_config: Prompt dict with system, user, assistant keys
    :param error_summary: Error summary text to analyze
    :param tools: List of LangChain tools available for the LLM to call (optional)
    :return: Analysis result
    """
    try:
        logger.info("Starting log analysis with tools")

        try:
            formatted_content = prompt_config["user"].format(
                error_summary=error_summary
            )
        except KeyError:
            formatted_content = prompt_config["user"].format(summary=error_summary)

        logger.debug(
            "Error summary: %s",
            error_summary[:150] + "..." if len(error_summary) > 150 else error_summary,
        )

        system_prompt = prompt_config["system"]
        if tools and any(getattr(t, "name", "") == "search_jira_issues" for t in tools):
            logger.info("Jira MCP tools detected - injecting Jira prompt")
            system_prompt += JIRA_TOOL_PROMPT["system"]

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": formatted_content},
            {"role": "assistant", "content": prompt_config["assistant"]},
        ]

        return await analyze_with_agentic(messages=messages, tools=tools)

    except InferenceAPIUnavailableError:
        raise
    except Exception as e:
        logger.error("Error analyzing log: %s", str(e), exc_info=True)
        raise InferenceAPIUnavailableError(f"Error analyzing log: {str(e)}") from e


def analyze_log_tool(query: str) -> str:
    """Tool function for LLM to analyze logs. Accepts 'query' to match SingleStringInput schema."""
    try:
        prompt_config = get_prompt_config()

        try:
            formatted_content = prompt_config["user"].format(error_summary=query)
        except KeyError:
            formatted_content = prompt_config["user"].format(summary=query)

        messages = [
            {"role": "system", "content": prompt_config["system"]},
            {"role": "user", "content": formatted_content},
            {"role": "assistant", "content": prompt_config["assistant"]},
        ]

        client = get_inference_client()
        message = client.chat(messages=messages)
        return message.content or ""

    except InferenceAPIUnavailableError:
        raise
    except Exception as e:
        logger.error("Error analyzing log: %s", e)
        raise InferenceAPIUnavailableError(f"Error analyzing log: {e}") from e


def download_and_analyze_logs(text):
    """Extract job details, download and analyze logs."""
    job_url, job_name = extract_job_details(text)
    if job_url is None or job_name is None:
        return ProwAnalysisResult(
            errors=None,
            categorization_message=None,
            requires_llm=None,
            is_install_issue=None,
            step_name=None,
            full_errors_for_file=None,
        )
    directory_path = download_prow_logs(job_url)
    return analyze_prow_artifacts(directory_path, job_name)


def filter_errors_with_llm(errors_list, requires_llm):
    """Filter errors using LLM.

    :return: Tuple of (result_text, retry_count)
    """
    client = get_inference_client()

    @_with_retry
    def _filter():
        current_errors_list = errors_list

        if requires_llm:
            error_step = current_errors_list[0]
            error_prompt = ERROR_FILTER_PROMPT["user"].format(
                error_list="\n".join(current_errors_list or [])[:MAX_CONTEXT_SIZE]
            )
            message = client.chat(
                messages=[
                    {"role": "system", "content": ERROR_FILTER_PROMPT["system"]},
                    {"role": "user", "content": error_prompt},
                    {"role": "assistant", "content": ERROR_FILTER_PROMPT["assistant"]},
                ],
            )

            # Convert JSON response to a Python list
            content = message.content or ""
            current_errors_list = [error_step + "\n"] + content.split("\n")

        error_prompt = generate_prompt(current_errors_list)
        message = client.chat(messages=error_prompt)
        return message.content or ""

    result = _filter()
    retry_count = _filter.statistics.get("attempt_number", 1) - 1
    return result, retry_count


def run_agent_analysis(error_summary):
    """Run agent analysis on the error summary with retry logic.

    :return: Tuple of (result_text, retry_count)
    """

    async def _run_async():
        if mcp_module.mcp_client is None:
            await initialize_global_resources_async()

        log_tool = StructuredTool(
            name="analyze_log",
            func=analyze_log_tool,
            description="Analyze logs from error summary. Input should be the error summary.",
            args_schema=SingleStringInput,
        )

        tools = [log_tool] + mcp_module.mcp_tools

        if not mcp_module.mcp_tools:
            logger.warning(
                "No MCP tools available. Continuing with basic analysis tools only."
            )

        logger.info(
            "Using %d tools (%d MCP tools)", len(tools), len(mcp_module.mcp_tools)
        )

        prompt_config = get_prompt_config()
        return await analyze_log_with_tools(
            prompt_config=prompt_config,
            error_summary=error_summary,
            tools=tools,
        )

    @_with_retry
    def _run():
        try:
            return asyncio.run(_run_async())
        except (InferenceAPIUnavailableError, AgentAnalysisLimitExceededError):
            raise
        except Exception as e:
            logger.error("Unexpected error during analysis: %s", str(e), exc_info=True)
            raise InferenceAPIUnavailableError(
                f"Unhandled error during analysis: {type(e).__name__}: {str(e)}"
            ) from e

    result = _run()
    retry_count = _run.statistics.get("attempt_number", 1) - 1
    return result, retry_count


def run_node_rca_analysis(job_url: str) -> str:
    """
    Full pipeline: prow URL → slowest pod → node journal → RCA markdown.

    Steps:
      1. Extract GCS path from prow view URL
      2. Discover log_folder via get_prow_inner_artifact_files
      3. Find podLatencyMeasurement JSON in openshift-qe-* step dir
      4. Download + parse JSON to find slowest pod and its node
      5. Download + decompress node journal (gzipped, no .gz suffix)
      6. Run deterministic parser, return formatted markdown

    :param job_url: prow view URL (https://prow.ci.../view/gs/...)
    :return: markdown RCA report string, or an error message string
    """
    try:
        gcs_path = extract_gcs_path(job_url)
    except Exception as exc:
        return f"node-rca: could not extract GCS path from URL: {exc}"

    log_folder_path, _ = get_prow_inner_artifact_files(gcs_path)
    if not log_folder_path:
        return "node-rca: could not find inner artifact folder (is this a valid prow URL?)."

    # log_folder_path looks like "gs://bucket/.../artifacts/<log_folder>/"
    # extract just the log_folder name
    log_folder = log_folder_path.rstrip("/").split("/artifacts/", 1)[-1].rstrip("/")

    tmp_dir = tempfile.mkdtemp(prefix="bugzooka-node-rca-")

    metrics_url = find_pod_latency_file(gcs_path, log_folder)
    if not metrics_url:
        return (
            "node-rca: podLatencyMeasurement JSON not found under "
            f"artifacts/{log_folder}/openshift-qe-*. "
            "Is this a node-density or similar perf job?"
        )

    json_path = download_pod_latency_file(metrics_url, tmp_dir)
    if not json_path:
        return f"node-rca: failed to download {metrics_url}."

    pod_name, node_hostname, latency_ms = parse_slowest_pod(json_path)
    if not pod_name or not node_hostname:
        return "node-rca: could not determine slowest pod from podLatencyMeasurement JSON."

    logger.info(
        "node-rca: slowest pod=%s node=%s latency=%dms",
        pod_name, node_hostname, latency_ms,
    )

    journal_path = download_node_journal(gcs_path, log_folder, node_hostname, tmp_dir)
    if not journal_path:
        return (
            f"node-rca: could not download journal for node {node_hostname}. "
            "Check that gather-extra artifacts are present for this run."
        )

    rca_result = analyze_node_journal(journal_path, pod_name=pod_name)
    header = (
        f"**Node RCA for `{pod_name}`** "
        f"(slowest pod, {latency_ms / 1000:.1f}s podReadyLatency)\n\n"
    )
    return header + format_result_markdown(rca_result)
