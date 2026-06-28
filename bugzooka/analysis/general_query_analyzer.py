"""General-purpose agentic query handler for free-form natural language questions.

Uses the same agentic LLM loop as pr_analyzer.py, but with a flexible prompt
that lets the LLM autonomously select which MCP tools to call based on the
user's natural language query. Supports multi-turn conversation via message history.
"""

import logging

from bugzooka.integrations.mcp_client import initialize_global_resources_async
from bugzooka.core.utils import make_response
from bugzooka.integrations.inference_client import analyze_with_agentic
from bugzooka.analysis.prompts import GENERAL_QUERY_PROMPT
import bugzooka.integrations.mcp_client as mcp_module

logger = logging.getLogger(__name__)


async def analyze_general_query(
    text: str,
    conversation_messages: list[dict],
    channel_id: str | None = None,
) -> dict:
    """
    Handle a free-form natural language query using the agentic LLM loop.

    The LLM receives the full conversation history and all available MCP tools,
    then autonomously decides which tools to call to answer the user's question.

    :param text: The current user message (already included in conversation_messages)
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
        result = await analyze_with_agentic(
            messages=messages,
            tools=mcp_module.mcp_tools,
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
