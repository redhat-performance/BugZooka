import logging
import re
from functools import partial

from langchain.agents import AgentType, Tool, initialize_agent
from langchain_community.chat_models.openai import ChatOpenAI
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from src.constants import (
    MAX_CONTEXT_SIZE,
    MAX_AGENTIC_ITERATIONS,
)
from src.prompts import ERROR_FILTER_PROMPT
from src.log_summarizer import (
    download_prow_logs,
    search_errors_in_file,
    generate_prompt,
    download_url_to_log,
)
from src.inference import (
    ask_inference_api,
    analyze_product_log,
    analyze_generic_log,
    AgentAnalysisLimitExceededError,
    InferenceAPIUnavailableError,
)
from src.gemini_client import analyze_log_with_gemini
from src.config import ANALYSIS_MODE
from src.prow_analyzer import analyze_prow_artifacts
from src.utils import extract_job_details

logger = logging.getLogger(__name__)


def download_and_analyze_logs(text, ci_system):
    """Extract job details, download and analyze logs based on CI system."""
    if ci_system == "PROW":
        job_url, job_name = extract_job_details(text)
        if job_url is None or job_name is None:
            return None, None, None
        directory_path = download_prow_logs(job_url)
        errors_list, categorization_message, requires_llm, is_install_issue = analyze_prow_artifacts(directory_path, job_name)
    else:
        # Pre-assumes the other ci system is ansible
        url_pattern = r"<([^>]+)>"
        match = re.search(url_pattern, text)
        if not match:
            return None, None, None
        url = match.group(1)
        logger.info("Ansible job url: %s", url)
        directory_path = download_url_to_log(url, "/build-log.txt")
        errors_list = search_errors_in_file(directory_path + "/build-log.txt")
        categorization_message = ""
        requires_llm = True  # Assuming you want LLM for ansible too?
        is_install_issue = False

    return errors_list, categorization_message, requires_llm, is_install_issue


def filter_errors_with_llm(errors_list, requires_llm, product_config):
    """Filter errors using LLM."""
    retry_config = product_config.get("retry", {})

    @retry(
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
    )
    def _filter_errors():
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

    return _filter_errors()


def run_agent_analysis(error_summary, product, product_config):
    """Run agent analysis on the error summary."""
    retry_config = product_config.get("retry", {})

    @retry(
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
    )
    def _run_agent_analysis():
        if ANALYSIS_MODE == "gemini":
            logger.info("Using Gemini analysis mode")
            response = analyze_log_with_gemini(product, product_config, error_summary)
            return response
        else:
            logger.info("Using agent-based analysis mode")
            return _run_fallback_agent_analysis()
    
    def _run_fallback_agent_analysis():
        """Fallback to agent-based analysis if direct Gemini call fails."""
        llm = ChatOpenAI(
            model=product_config["model"]["GENERIC"],
            api_key=product_config["token"]["GENERIC"],
            base_url=product_config["endpoint"]["GENERIC"] + "/v1",
        )

        product_tool = Tool(
            name="analyze_product_log",
            func=partial(analyze_product_log, product, product_config),
            description=f"Analyze {product} logs from error summary. Input should be the error summary.",
        )

        generic_tool = Tool(
            name="analyze_generic_log",
            func=partial(analyze_generic_log, product_config),
            description="Analyze general logs from error summary. Input should be the error summary.",
        )

        TOOLS = [product_tool, generic_tool]
        agent = initialize_agent(
            tools=TOOLS,
            llm=llm,
            agent=AgentType.ZERO_SHOT_REACT_DESCRIPTION,
            verbose=True,
            handle_parsing_errors=True,
            max_iterations=MAX_AGENTIC_ITERATIONS,
        )

        query = (
            f"Please analyze this {product} specific error"
            f" summary: {error_summary} using the most appropriate"
            f" tool (product-specific or generic)"
            f" and provide me potential next steps to debug"
            f" this issue as a final answer"
        )

        response = agent.run(query)

        if "Agent stopped due to iteration limit or time limit" in response:
            raise AgentAnalysisLimitExceededError(
                "Agent analysis exceeded iteration or time limits"
            )

        return response

    return _run_agent_analysis()
