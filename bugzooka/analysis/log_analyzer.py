import logging
import asyncio
from functools import partial
from pydantic import BaseModel, Field

from langchain_core.tools import StructuredTool
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from bugzooka.core.constants import (
    MAX_CONTEXT_SIZE,
    MAX_AGENTIC_ITERATIONS,
)
from bugzooka.analysis.prompts import ERROR_FILTER_PROMPT
from bugzooka.analysis.log_summarizer import (
    download_prow_logs,
    generate_prompt,
)
from bugzooka.integrations.inference import (
    ask_inference_api,
    analyze_product_log,
    analyze_generic_log,
    AgentAnalysisLimitExceededError,
    InferenceAPIUnavailableError,
)
from bugzooka.integrations import mcp_client as mcp_module
from bugzooka.integrations.mcp_client import initialize_global_resources_async
from bugzooka.integrations.gemini_client import analyze_log_with_gemini
from bugzooka.analysis.prow_analyzer import analyze_prow_artifacts
from bugzooka.core.utils import extract_job_details

logger = logging.getLogger(__name__)


class SingleStringInput(BaseModel):
    """Schema for tools that accept a single string argument."""

    query: str = Field(description="The full error summary text to analyze.")


def product_log_wrapper(query: str, product: str, product_config: dict) -> str:
    """Wraps analyze_product_log to accept the 'query' keyword argument."""
    # The 'query' keyword argument from the agent is passed as the error_summary
    return analyze_product_log(product, product_config, query)


def generic_log_wrapper(query: str, product_config: dict) -> str:
    """Wraps analyze_generic_log to accept the 'query' keyword argument."""
    return analyze_generic_log(product_config, query)


def download_and_analyze_logs(text):
    """Extract job details, download and analyze Prow logs."""

    job_url, job_name = extract_job_details(text)
    if job_url is None or job_name is None:
        return None, None, None, None

    directory_path = download_prow_logs(job_url)
    return analyze_prow_artifacts(directory_path, job_name)


def filter_errors_with_llm(errors_list, requires_llm, product_config):
    """Filter errors using LLM."""
    retry_config = product_config.get("retry", {})

    def attempt():
        current_errors_list = errors_list

        if requires_llm:
            error_step = current_errors_list[0]
            error_prompt = ERROR_FILTER_PROMPT["user"].format(
                error_list="\n".join(current_errors_list or [])[:MAX_CONTEXT_SIZE]
            )
            response = ask_inference_api(
                messages=[
                    {"role": "system", "content": ERROR_FILTER_PROMPT["system"]},
                    {"role": "user", "content": error_prompt},
                    {"role": "assistant", "content": ERROR_FILTER_PROMPT["assistant"]},
                ],
                url=product_config["endpoint"]["GENERIC"],
                api_token=product_config["token"]["GENERIC"],
                model=product_config["model"]["GENERIC"],
            )

            # Convert JSON response to a Python list
            current_errors_list = [error_step + "\n"] + response.split("\n")

        error_prompt = generate_prompt(current_errors_list)
        error_summary = ask_inference_api(
            messages=error_prompt,
            url=product_config["endpoint"]["GENERIC"],
            api_token=product_config["token"]["GENERIC"],
            model=product_config["model"]["GENERIC"],
        )
        return error_summary

    return retry(
        stop=stop_after_attempt(retry_config["max_attempts"]),
        wait=wait_exponential(
            multiplier=retry_config["backoff"],
            min=retry_config["delay"],
            max=retry_config["max_delay"],
        ),
        retry=retry_if_exception_type(
            (InferenceAPIUnavailableError, AgentAnalysisLimitExceededError)
        ),
        reraise=True,
    )(attempt)()


async def _run_gemini_analysis(error_summary, product, product_config):
    """Run Gemini analysis with MCP tool support."""
    if mcp_module.mcp_client is None:
        await initialize_global_resources_async()

    product_tool = StructuredTool(
        name="analyze_product_log",
        func=partial(
            product_log_wrapper, product=product, product_config=product_config
        ),
        description=f"Analyze {product} logs from error summary. Input should be the error summary.",
        args_schema=SingleStringInput,
    )

    generic_tool = StructuredTool(
        name="analyze_generic_log",
        func=partial(generic_log_wrapper, product_config=product_config),
        description="Analyze general logs from error summary. Input should be the error summary.",
        args_schema=SingleStringInput,
    )

    tools = [product_tool, generic_tool] + mcp_module.mcp_tools

    if not mcp_module.mcp_tools:
        logger.warning(
            "No MCP tools available for Gemini. Continuing with basic analysis tools only."
        )

    logger.info(
        "Gemini Analysis: Using %d tools (%d MCP tools)",
        len(tools),
        len(mcp_module.mcp_tools),
    )

    return await analyze_log_with_gemini(
        product=product,
        product_config=product_config,
        error_summary=error_summary,
        tools=tools if tools else None,
    )


def run_agent_analysis(error_summary, product, product_config):
    """Run agentic analysis on the error summary."""
    retry_config = product_config.get("retry", {})

    def attempt():
        try:
            return asyncio.run(
                _run_gemini_analysis(error_summary, product, product_config)
            )
        except Exception as e:
            logger.error("Gemini analysis error: %s", str(e), exc_info=True)
            raise InferenceAPIUnavailableError(
                f"Gemini analysis failed: {type(e).__name__}: {str(e)}"
            ) from e

    return retry(
        stop=stop_after_attempt(retry_config["max_attempts"]),
        wait=wait_exponential(
            multiplier=retry_config["backoff"],
            min=retry_config["delay"],
            max=retry_config["max_delay"],
        ),
        retry=retry_if_exception_type(
            (InferenceAPIUnavailableError, AgentAnalysisLimitExceededError)
        ),
        reraise=True,
    )(attempt)()
