"""General-purpose agentic query handler for free-form natural language questions.

Unlike pr_analyzer.py which uses analyze_with_agentic from commons, this module
drives chat_with_tools_async directly so tool calls route through invoke_mcp_tool.
This lets the ImageCollector intercept image content blocks from MCP tools.
"""

import logging

from langchain_core.utils.function_calling import convert_to_openai_tool

from bugzooka.integrations.mcp_client import (
    initialize_global_resources_async,
    invoke_mcp_tool,
)
from bugzooka.core.utils import make_response
from bugzooka.integrations.inference_client import get_inference_client
from bugzooka.analysis.prompts import GENERAL_QUERY_PROMPT
import bugzooka.integrations.mcp_client as mcp_module

logger = logging.getLogger(__name__)


async def analyze_general_query(
    conversation_messages: list[dict],
    channel_id: str | None = None,
) -> dict:
    """
    Handle a free-form natural language query using the agentic LLM loop.

    :param conversation_messages: Full conversation history in OpenAI message format
    :param channel_id: Slack channel ID for ES_SERVER routing (optional)
    :return: Dictionary with 'success' (bool) and 'message' (str)
    """
    if channel_id:
        from bugzooka.integrations.mcp_interceptors import current_channel

        current_channel.set(channel_id)
        logger.debug("Set channel context for general query: %s", channel_id)

    await initialize_global_resources_async()

    if not mcp_module.mcp_tools:
        return make_response(
            success=False,
            message="No MCP tools available. Please check the MCP server connection.",
        )

    messages = [
        {"role": "system", "content": GENERAL_QUERY_PROMPT["system"]},
    ]
    messages.extend(conversation_messages)

    try:
        client = get_inference_client()
        tools_by_name = {tool.name: tool for tool in mcp_module.mcp_tools}
        openai_tools = [convert_to_openai_tool(t) for t in mcp_module.mcp_tools]

        async def execute_tool(tool_name, tool_args):
            tool = tools_by_name.get(tool_name)
            if not tool:
                return f"Error: Tool '{tool_name}' not found"
            return await invoke_mcp_tool(tool, tool_args)

        result = await client.chat_with_tools_async(
            messages=messages,
            tools=openai_tools,
            execute_tool_func=execute_tool,
        )

        if not result:
            logger.warning("LLM returned empty result for general query")
            return make_response(
                success=False,
                message="I couldn't generate a response. Please try rephrasing your question.",
            )

        return make_response(success=True, message=result)

    except Exception as e:
        logger.error("Error during general query analysis: %s", e, exc_info=True)
        return make_response(
            success=False,
            message=f"An error occurred while processing your query: {e}",
        )
