"""
Inference Client for OpenAI-compatible endpoints.

This module provides a single client that works with any OpenAI-compatible
inference endpoint including Gemini, Llama, DeepSeek, etc.
"""

import json
import logging
import ssl
from typing import Optional

import httpx
from openai import OpenAI
from langchain_core.utils.function_calling import convert_to_openai_tool

from bugzooka.core.constants import (
    INFERENCE_MAX_TOKENS,
    INFERENCE_TEMPERATURE,
    INFERENCE_API_TIMEOUT_SECONDS,
    INFERENCE_MAX_TOOL_ITERATIONS,
)

logger = logging.getLogger(__name__)


class InferenceAPIUnavailableError(Exception):
    """Raised when the inference API is unavailable."""


class AgentAnalysisLimitExceededError(Exception):
    """Raised when the agent analysis exceeds iteration or time limits."""


# Global inference client instance (initialized lazily)
_inference_client: Optional["InferenceClient"] = None


def get_inference_client() -> "InferenceClient":
    """
    Get the global inference client instance.

    Initializes the client on first call using INFERENCE_* environment variables
    via get_inference_config(). Subsequent calls return the same instance.

    :return: Global InferenceClient instance
    :raises ValueError: If required environment variables are not set
    """
    global _inference_client

    if _inference_client is not None:
        return _inference_client

    from bugzooka.core.config import get_inference_config

    config = get_inference_config()

    logger.info(
        "Initializing global inference client: url=%s, model=%s",
        config["url"],
        config["model"],
    )

    _inference_client = InferenceClient(
        base_url=config["url"],
        api_key=config["token"],
        model=config["model"],
        retry_config=config["retry"],
        verify_ssl=config["verify_ssl"],
        timeout=config["timeout"],
        top_p=config["top_p"],
        frequency_penalty=config["frequency_penalty"],
    )

    return _inference_client


class InferenceClient:
    """
    Client for any OpenAI-compatible inference endpoint.

    Works with: Gemini, Llama, DeepSeek, etc.

    Example usage:
        client = InferenceClient(
            base_url="https://api.example.com",
            api_key="your-api-key",
            model="llama-3-2-3b"
        )
        message = client.chat(messages=[{"role": "user", "content": "Hello"}])
        print(message.content)  # Access the response text
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        retry_config: dict,
        verify_ssl: bool = True,
        timeout: float = INFERENCE_API_TIMEOUT_SECONDS,
        top_p: Optional[float] = None,
        frequency_penalty: Optional[float] = None,
    ):
        """
        Initialize the inference client.

        :param base_url: Base URL for the inference API (without /v1 suffix)
        :param api_key: API authentication token
        :param model: Model name to use for inference
        :param verify_ssl: Whether to verify SSL certificates (default: True)
        :param timeout: Request timeout in seconds (default: 120)
        :param top_p: Nucleus sampling probability (optional, not all APIs support this)
        :param frequency_penalty: Penalty for frequent tokens (optional, not all APIs support this)
        :param retry_config: Retry configuration dict with max_attempts, delay, backoff, max_delay
        """
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.top_p = top_p
        self.frequency_penalty = frequency_penalty
        self.retry_config = retry_config

        # Telemetry accumulators (reset per agentic call)
        self.last_total_tokens = 0
        self.last_tool_calls_count = 0

        # Create custom HTTP client with SSL configuration
        if not verify_ssl:
            logger.warning("SSL certificate verification disabled for %s", base_url)
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            http_client = httpx.Client(verify=False, timeout=timeout)
        else:
            http_client = httpx.Client(timeout=timeout)

        # Ensure base_url doesn't have trailing slash for OpenAI SDK
        normalized_url = base_url.rstrip("/")

        self.client = OpenAI(
            api_key=api_key,
            base_url=normalized_url,
            http_client=http_client,
        )

        logger.debug(
            "Initialized InferenceClient: url=%s, model=%s",
            normalized_url,
            model,
        )

    def chat(
        self,
        messages: list,
        max_tokens: int = INFERENCE_MAX_TOKENS,
        temperature: float = INFERENCE_TEMPERATURE,
        tools: Optional[list] = None,
        **kwargs,
    ):
        """
        Chat completion.

        :param messages: List of message dictionaries with 'role' and 'content'
        :param max_tokens: Maximum tokens to generate
        :param temperature: Controls randomness (0.0 = deterministic)
        :param tools: Optional list of tools in OpenAI format
        :param kwargs: Additional parameters passed to the API
        :return: Message object with .content and .tool_calls attributes
        """
        try:
            api_kwargs = {
                "model": self.model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }

            # Only add these if set on client (not all APIs support them, e.g. Gemini)
            if self.top_p is not None:
                api_kwargs["top_p"] = self.top_p
            if self.frequency_penalty is not None:
                api_kwargs["frequency_penalty"] = self.frequency_penalty

            # Add tools only if provided
            if tools:
                api_kwargs["tools"] = tools

            # Add any extra kwargs
            api_kwargs.update(kwargs)

            logger.debug(
                "Calling inference API: %s, Model=%s", self.base_url, self.model
            )

            response = self.client.chat.completions.create(**api_kwargs)

            # Log token usage if available
            usage_data = None
            if hasattr(response, "usage") and response.usage:
                usage = response.usage
                logger.info(
                    "Token usage - Prompt: %d, Completion: %d, Total: %d",
                    usage.prompt_tokens,
                    usage.completion_tokens,
                    usage.total_tokens,
                )
                usage_data = {
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "total_tokens": usage.total_tokens,
                }

            message = response.choices[0].message
            try:
                message._token_usage = usage_data
            except (AttributeError, TypeError):
                object.__setattr__(message, "_token_usage", usage_data)
            return message

        except httpx.TimeoutException as e:
            logger.error("Request timed out after %s seconds: %s", self.timeout, e)
            raise InferenceAPIUnavailableError(
                f"Request timed out after {self.timeout} seconds"
            ) from e
        except httpx.ConnectError as e:
            logger.error("Connection error to inference API: %s", e)
            raise InferenceAPIUnavailableError(
                "Connection error to inference API"
            ) from e
        except Exception as e:
            error_type = type(e).__name__
            error_msg = str(e)
            logger.error(
                "Error calling inference API: %s - %s (url=%s, model=%s)",
                error_type,
                error_msg,
                self.base_url,
                self.model,
            )
            raise InferenceAPIUnavailableError(
                f"Inference API error ({error_type}): {error_msg}"
            ) from e

    async def chat_with_tools_async(
        self,
        messages: list,
        tools: list,
        execute_tool_func,
        max_iterations: int = INFERENCE_MAX_TOOL_ITERATIONS,
        max_tokens: int = INFERENCE_MAX_TOKENS,
        temperature: float = INFERENCE_TEMPERATURE,
    ) -> str:
        """
        Agentic loop with tool calling support.

        Iteratively calls the LLM, executes any requested tools, and continues
        until a final answer is produced or max_iterations is reached.

        :param messages: Initial list of message dictionaries
        :param tools: List of tools in OpenAI format
        :param execute_tool_func: Async function to execute tool calls.
                                  Signature: async (tool_name, tool_args) -> str
        :param max_iterations: Maximum number of tool-calling iterations
        :param max_tokens: Maximum tokens per response
        :param temperature: Controls randomness
        :return: Final response content as string
        """
        logger.debug("Starting agentic loop with %d messages", len(messages))

        total_tokens_accumulated = 0
        tool_calls_count = 0

        iteration = 0
        while iteration < max_iterations:
            iteration += 1
            logger.debug("Agentic iteration %d/%d", iteration, max_iterations)

            # Get response with potential tool calls
            message = self.chat(
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                tools=tools,
            )

            if hasattr(message, "_token_usage") and message._token_usage:
                total_tokens_accumulated += message._token_usage.get(
                    "total_tokens", 0
                )

            tool_calls = getattr(message, "tool_calls", None)

            if not tool_calls:
                # No tool calls - we have the final answer
                content = message.content
                if content:
                    logger.info("Analysis complete after %d iteration(s)", iteration)
                    logger.debug(
                        "Response: %s",
                        content[:200] + "..." if len(content) > 200 else content,
                    )
                else:
                    logger.warning("LLM returned None content, using empty string")
                    content = ""
                self.last_total_tokens = total_tokens_accumulated
                self.last_tool_calls_count = tool_calls_count
                return content

            # LLM wants to call tools - execute them
            tool_calls_count += len(tool_calls)
            tool_names_called = [tc.function.name for tc in tool_calls]
            logger.info(
                "Calling %d tool(s): %s", len(tool_calls), ", ".join(tool_names_called)
            )

            # Add the assistant's message with tool calls to conversation
            messages.append(
                {
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in tool_calls
                    ],
                }
            )

            # Execute each tool call and add results to messages
            for tool_call in tool_calls:
                function_name = tool_call.function.name

                try:
                    function_args = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError as e:
                    logger.error("Failed to parse tool arguments: %s", e)
                    function_result = f"Error: Invalid JSON arguments - {str(e)}"
                else:
                    # Execute the tool
                    function_result = await execute_tool_func(
                        function_name, function_args
                    )

                # Add tool result to messages
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": function_name,
                        "content": function_result,
                    }
                )

            # Continue loop to let LLM process tool results

        # If we hit max iterations without a final answer
        logger.warning(
            "Reached maximum iterations (%d) without final answer", max_iterations
        )
        self.last_total_tokens = total_tokens_accumulated
        self.last_tool_calls_count = tool_calls_count
        return "Analysis incomplete: Maximum tool calling iterations reached. Please try again with a simpler query."


# =============================================================================
# Tool/Agentic Functions
# =============================================================================


async def _execute_tool_call(tool_name, tool_args, available_tools):
    """
    Execute a tool call by finding and invoking the appropriate LangChain tool.
    Handles both sync and async tools.

    :param tool_name: Name of the tool to execute
    :param tool_args: Dictionary of arguments for the tool
    :param available_tools: List of available LangChain tools
    :return: Tool execution result as string
    """
    tool = next((t for t in available_tools if t.name == tool_name), None)

    if not tool:
        error_msg = f"Tool '{tool_name}' not found in available tools"
        logger.error("Tool not found: %s", error_msg)
        return f"Error: {error_msg}"

    try:
        logger.info("Executing tool: %s", tool_name)
        logger.debug("Tool arguments: %s", json.dumps(tool_args, indent=2))

        result = await tool.ainvoke(tool_args)

        result_str = str(result)
        result_length = len(result_str)

        if not result_str or result_str.strip() in ["", "null", "None", "{}", "[]"]:
            logger.warning("Tool %s returned empty or null result", tool_name)
        elif len(result_str.strip()) < 50:
            logger.warning(
                "Tool %s returned small result (%d chars): %s",
                tool_name,
                result_length,
                result_str,
            )
        else:
            logger.info("Tool %s completed (%d chars)", tool_name, result_length)

        logger.debug("Tool %s output: %s", tool_name, result_str)
        return result_str

    except Exception as e:
        error_msg = f"Error executing tool '{tool_name}': {str(e)}"
        logger.error("%s", error_msg, exc_info=True)
        logger.error("Tool arguments that caused the error:")
        logger.error("%s", json.dumps(tool_args, indent=2))
        return f"Error: {error_msg}"


async def analyze_with_agentic(
    messages: list,
    tools=None,
    max_iterations=None,
):
    """
    Agentic loop for LLM with tool calling support.

    This function implements the agentic pattern where the LLM can iteratively:
    1. Analyze the current context
    2. Decide to call tools if needed
    3. Process tool results
    4. Generate final answer

    Uses the global inference client (initialized from INFERENCE_* env vars).

    :param messages: List of message dictionaries (system, user, assistant prompts)
    :param tools: List of LangChain tools available for the LLM to call (optional)
    :param max_iterations: Maximum number of tool calling iterations
    :return: Final analysis result as string
    """
    if max_iterations is None:
        max_iterations = INFERENCE_MAX_TOOL_ITERATIONS

    try:
        client = get_inference_client()

        openai_tools = None
        if tools:
            openai_tools = [convert_to_openai_tool(tool) for tool in tools]
            tool_names = [t["function"]["name"] for t in openai_tools]
            logger.info(
                "Starting agentic analysis with %d tools: %s",
                len(openai_tools),
                ", ".join(tool_names),
            )

        if not openai_tools:
            logger.debug("No tools provided, doing simple chat completion")
            message = client.chat(messages=messages)
            return message.content or ""

        async def tool_executor(tool_name, tool_args):
            return await _execute_tool_call(tool_name, tool_args, tools)

        return await client.chat_with_tools_async(
            messages=messages,
            tools=openai_tools,
            execute_tool_func=tool_executor,
            max_iterations=max_iterations,
        )

    except InferenceAPIUnavailableError:
        raise
    except Exception as e:
        logger.error("Error in agentic loop: %s", str(e), exc_info=True)
        raise InferenceAPIUnavailableError(f"Error in agentic loop: {str(e)}") from e
