import logging
import os
import ssl
import httpx
import json

from openai import OpenAI
from langchain_core.utils.function_calling import convert_to_openai_tool
from bugzooka.core.constants import (
    INFERENCE_MAX_TOKENS,
    INFERENCE_TEMPERATURE,
    INFERENCE_MAX_TOOL_ITERATIONS,
)
from bugzooka.integrations.inference import InferenceAPIUnavailableError


logger = logging.getLogger(__name__)


class GeminiClient:
    """
    Gemini API client that implements OpenAI-compatible interface.
    """

    def __init__(self, api_key=None, base_url=None, verify_ssl=None, timeout=None):
        """
        Initialize Gemini client.

        :param api_key: Gemini API key (defaults to GEMINI_API_KEY env var)
        :param base_url: Gemini API base URL (defaults to GEMINI_API_URL env var)
        :param verify_ssl: Whether to verify SSL certificates (defaults to GEMINI_VERIFY_SSL env var, then True)
        :param timeout: Request timeout in seconds (defaults to GEMINI_TIMEOUT env var, then 60.0)
        """
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY environment variable is required")

        self.base_url = base_url or os.getenv("GEMINI_API_URL")
        if not self.base_url:
            raise ValueError(
                "GEMINI_API_URL environment variable or base_url parameter is required"
            )

        # Timeout configuration
        if timeout is None:
            timeout = float(os.getenv("GEMINI_TIMEOUT", "60.0"))
        
        logger.debug("Gemini client timeout set to %.1f seconds", timeout)

        # SSL verification configuration
        if verify_ssl is None:
            verify_ssl_env = os.getenv("GEMINI_VERIFY_SSL").lower()
            verify_ssl = verify_ssl_env == "true"

        # Create custom HTTP client with SSL configuration
        if not verify_ssl:
            logger.warning("SSL certificate verification disabled for Gemini API")
            # Create SSL context that doesn't verify certificates
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

            http_client = httpx.Client(verify=False, timeout=timeout)
        else:
            http_client = httpx.Client(timeout=timeout)

        self.client = OpenAI(
            api_key=self.api_key, base_url=self.base_url, http_client=http_client
        )

    def chat_completions_create(self, messages, model="gemini-2.0-flash", **kwargs):
        """
        Create chat completion using Gemini API.

        :param messages: List of message dictionaries
        :param model: Gemini model name (default: gemini-2.0-flash)
        :param kwargs: Additional parameters
        :return: Chat completion response
        """
        try:
            logger.debug("Calling Gemini API: %s, Model=%s", self.base_url, model)
            
            response = self.client.chat.completions.create(
                model=model, messages=messages, **kwargs
            )
            
            # Log token usage information
            if hasattr(response, 'usage') and response.usage:
                usage = response.usage
                logger.info("📊 Token usage - Prompt: %d, Completion: %d, Total: %d",
                           usage.prompt_tokens, usage.completion_tokens, usage.total_tokens)
            else:
                logger.debug("No usage information available in response")
            
            logger.debug("Gemini API call successful")
            return response
        except Exception as e:
            # Enhanced error logging with more details
            error_type = type(e).__name__
            error_msg = str(e)

            logger.error("❌ Error calling Gemini API:")
            logger.error("  - Error Type: %s", error_type)
            logger.error("  - Error Message: %s", error_msg)
            logger.error("  - Base URL: %s", self.base_url)
            logger.error("  - Model: %s", model)

            raise InferenceAPIUnavailableError(
                f"Gemini API error ({error_type}): {error_msg}"
            ) from e


def convert_langchain_tools_to_openai_format(langchain_tools):
    """
    Convert LangChain tools to OpenAI function calling format using LangChain's built-in converter.

    :param langchain_tools: List of LangChain StructuredTool objects
    :return: List of tool definitions in OpenAI format
    """
    openai_tools = [convert_to_openai_tool(tool) for tool in langchain_tools]
    return openai_tools


async def execute_tool_call(tool_name, tool_args, available_tools):
    """
    Execute a tool call by finding and invoking the appropriate LangChain tool.
    Handles both sync and async tools.

    :param tool_name: Name of the tool to execute
    :param tool_args: Dictionary of arguments for the tool
    :param available_tools: List of available LangChain tools
    :return: Tool execution result as string
    """
    # Find the tool by name
    tool = next((t for t in available_tools if t.name == tool_name), None)

    if not tool:
        error_msg = f"Tool '{tool_name}' not found in available tools"
        logger.error("❌ %s", error_msg)
        return f"Error: {error_msg}"

    try:
        # Log tool execution
        logger.info("🔧 Executing tool: %s", tool_name)
        logger.debug("Tool arguments: %s", json.dumps(tool_args, indent=2))

        # Check if the tool is async (has coroutine attribute or ainvoke method)
        if hasattr(tool, 'coroutine') and tool.coroutine:
            # MCP tools have a coroutine attribute
            result = await tool.ainvoke(tool_args)
        elif hasattr(tool, 'ainvoke'):
            # Some tools have ainvoke method
            result = await tool.ainvoke(tool_args)
        else:
            # Synchronous tool
            result = tool.invoke(tool_args)

        # Log result
        result_str = str(result)
        result_length = len(result_str)
        
        # Check for empty or minimal results
        if not result_str or result_str.strip() in ["", "null", "None", "{}", "[]"]:
            logger.warning("⚠️ Tool %s returned empty or null result", tool_name)
        elif len(result_str.strip()) < 50:
            logger.warning("⚠️ Tool %s returned small result (%d chars): %s", 
                          tool_name, result_length, result_str)
        else:
            logger.info("✅ Tool %s completed (%d chars)", tool_name, result_length)
            
        # Log full output at DEBUG level
        logger.debug("Tool %s output: %s", tool_name, result_str)
        
        return result_str
    except Exception as e:
        error_msg = f"Error executing tool '{tool_name}': {str(e)}"
        logger.error("❌ %s", error_msg, exc_info=True)
        logger.error("💡 Tool arguments that caused the error:")
        logger.error("%s", json.dumps(tool_args, indent=2))
        return f"Error: {error_msg}"


async def analyze_with_gemini_agentic(
    messages: list,
    tools=None,
    model="gemini-2.0-flash",
    max_iterations=None
):
    """
    Generic agentic loop for Gemini with tool calling support.
    
    This function implements the agentic pattern where Gemini can iteratively:
    1. Analyze the current context
    2. Decide to call tools if needed
    3. Process tool results
    4. Generate final answer
    
    :param messages: List of message dictionaries (system, user, assistant prompts)
    :param tools: List of LangChain tools available for Gemini to call (optional)
    :param model: Gemini model to use (default: gemini-2.0-flash)
    :param max_iterations: Maximum number of tool calling iterations (default: INFERENCE_MAX_TOOL_ITERATIONS)
    :return: Final analysis result from Gemini as string
    """
    if max_iterations is None:
        max_iterations = INFERENCE_MAX_TOOL_ITERATIONS
    
    try:
        gemini_client = GeminiClient()
        
        # Convert LangChain tools to OpenAI format if provided
        openai_tools = None
        if tools:
            openai_tools = convert_langchain_tools_to_openai_format(tools)
            tool_names = [t["function"]["name"] for t in openai_tools]
            logger.info("Starting Gemini analysis with %d tools: %s",
                       len(openai_tools), ", ".join(tool_names))
        
        logger.debug("Starting agentic loop with %d messages", len(messages))

        # Tool calling loop - iterate until we get a final answer or hit max iterations
        iteration = 0
        while iteration < max_iterations:
            iteration += 1
            logger.debug("Agentic iteration %d/%d", iteration, max_iterations)

            # Call Gemini API
            api_kwargs = {
                "messages": messages,
                "model": model,
                "max_tokens": INFERENCE_MAX_TOKENS,
                "temperature": INFERENCE_TEMPERATURE,
            }

            # Only add tools if they exist
            if openai_tools:
                api_kwargs["tools"] = openai_tools

            response = gemini_client.chat_completions_create(**api_kwargs)

            response_message = response.choices[0].message

            # Check if Gemini wants to call tools
            tool_calls = getattr(response_message, 'tool_calls', None)

            if not tool_calls:
                # No tool calls - we have the final answer
                content = response_message.content
                if content:
                    logger.info("Analysis complete after %d iteration(s)", iteration)
                    logger.debug("Response: %s", content[:200] + "..." if len(content) > 200 else content)
                else:
                    logger.warning("Gemini returned None content, using empty string")
                    content = ""
                return content

            # Gemini wants to call tools - execute them
            tool_names_called = [tc.function.name for tc in tool_calls]
            logger.info("Calling %d tool(s): %s", len(tool_calls), ", ".join(tool_names_called))

            # Add the assistant's message with tool calls to conversation
            messages.append({
                "role": "assistant",
                "content": response_message.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments
                        }
                    }
                    for tc in tool_calls
                ]
            })

            # Execute each tool call and add results to messages
            for tool_call in tool_calls:
                function_name = tool_call.function.name
                
                try:
                    function_args = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError as e:
                    logger.error("Failed to parse tool arguments: %s", e)
                    function_result = f"Error: Invalid JSON arguments - {str(e)}"
                else:
                    # Execute the tool (await since it's now async)
                    function_result = await execute_tool_call(
                        function_name,
                        function_args,
                        tools
                    )

                # Add tool result to messages
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": function_name,
                    "content": function_result
                })

            # Continue loop to let Gemini process tool results

        # If we hit max iterations without a final answer
        logger.warning("Reached maximum iterations (%d) without final answer", max_iterations)
        return "Analysis incomplete: Maximum tool calling iterations reached. Please try again with a simpler query."
        
    except Exception as e:
        logger.error("Error in Gemini agentic loop: %s", str(e), exc_info=True)
        raise InferenceAPIUnavailableError(
            f"Error in Gemini agentic loop: {str(e)}"
        ) from e


async def analyze_log_with_gemini(
    product: str,
    product_config: dict,
    error_summary: str,
    model="gemini-2.0-flash",
    tools=None,
    max_iterations=None
):
    """
    Analyzes log summaries using Gemini API with product-specific prompts and optional tool calling.

    :param product: Product name (e.g., "OPENSHIFT", "ANSIBLE", or "GENERIC")
    :param product_config: Config dict with prompt, endpoint, token, and model info
    :param error_summary: Error summary text to analyze
    :param model: Gemini model to use (default: gemini-2.0-flash)
    :param tools: List of LangChain tools available for Gemini to call (optional)
    :param max_iterations: Maximum number of tool calling iterations (default: INFERENCE_MAX_TOOL_ITERATIONS)
    :return: Analysis result from Gemini
    """
    try:
        logger.info("Starting log analysis for product: %s", product)
        
        prompt_config = product_config["prompt"][product]
        try:
            formatted_content = prompt_config["user"].format(
                error_summary=error_summary
            )
        except KeyError:
            formatted_content = prompt_config["user"].format(summary=error_summary)

        logger.debug("Error summary: %s", error_summary[:150] + "..." if len(error_summary) > 150 else error_summary)

        messages = [
            {"role": "system", "content": prompt_config["system"]},
            {"role": "user", "content": formatted_content},
            {"role": "assistant", "content": prompt_config["assistant"]},
        ]

        # Use the generic agentic loop
        return await analyze_with_gemini_agentic(
            messages=messages,
            tools=tools,
            model=model,
            max_iterations=max_iterations
        )

    except Exception as e:
        logger.error("Error analyzing %s log: %s", product, str(e), exc_info=True)
        raise InferenceAPIUnavailableError(
            f"Error analyzing {product} log with Gemini: {str(e)}"
        ) from e
