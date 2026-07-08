import json
import logging
from typing import Any, Optional

from langchain_mcp_adapters.client import MultiServerMCPClient

from bugzooka.core.utils import make_response

logger = logging.getLogger(__name__)

mcp_client = None
mcp_tools: list = []


async def initialize_global_resources_async(mcp_config_path: str = "mcp_config.json"):
    """
    Initialize the MCP client and load tools from ``mcp_config_path``.

    Handles a missing ``mcp_config.json`` without crashing. When
    ``get_es_channel_mappings()`` succeeds, registers a ``HeaderEncryptionInterceptor``
    so orion-mcp tool calls from a Slack context can carry an encrypted per-channel
    ES config in ``X-Encrypted-Context``. If mappings are unset, the client starts
    without that interceptor and orion-mcp relies on its default ``ES_SERVER``.
    """
    global mcp_client, mcp_tools
    # Initialize the MCP client from a config file.
    if mcp_client is not None:
        return

    try:
        with open(mcp_config_path, "r") as f:
            config = json.load(f)

        # Header encryption: per-Slack-channel ES config is sent to orion-mcp on
        # selected tools via X-Encrypted-Context (see HeaderEncryptionInterceptor).
        from bugzooka.integrations.mcp_interceptors import (
            create_header_encryption_interceptor,
        )
        from bugzooka.core.config import get_es_channel_mappings

        try:
            es_config_map = get_es_channel_mappings()
            header_encryption_interceptor = create_header_encryption_interceptor(
                es_config_map
            )
            logger.info(
                "Header encryption interceptor registered (%d channel ES configs)",
                len(es_config_map),
            )
        except ValueError as e:
            logger.warning(
                "ES channel mappings not configured: %s. "
                "Header encryption interceptor will not be registered. "
                "orion-mcp will use default ES_SERVER from environment.",
                str(e),
            )
            header_encryption_interceptor = None

        if header_encryption_interceptor:
            mcp_client = MultiServerMCPClient(
                config["mcp_servers"],
                tool_interceptors=[header_encryption_interceptor],
            )
            logger.info("MCP client initialized with header encryption interceptor")
        else:
            mcp_client = MultiServerMCPClient(config["mcp_servers"])
            logger.info("MCP client initialized without interceptors")

        mcp_tools = await mcp_client.get_tools()
        logger.info(f"MCP configuration loaded and {len(mcp_tools)} tools retrieved.")

    except FileNotFoundError:
        logger.warning(
            f"MCP configuration file not found at {mcp_config_path}. Running without external tools."
        )
        mcp_tools = []
        # Create a dummy client to avoid crashing, though it won't be used.
        mcp_client = MultiServerMCPClient({})
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in MCP configuration file: {e}")
        raise
    except Exception as e:
        logger.error(f"Error initializing MCP tools: {e}", exc_info=True)
        raise


def get_mcp_tool(tool_name: str) -> Optional[Any]:
    """
    Look up an MCP tool by name.

    :param tool_name: Name of the tool to find
    :return: The tool object if found, None otherwise
    """
    for tool in mcp_tools:
        if tool.name == tool_name:
            return tool
    return None


def get_available_tool_names() -> list[str]:
    """
    Get list of all available MCP tool names.

    :return: List of tool names
    """
    return [t.name for t in mcp_tools]


async def invoke_mcp_tool(tool: Any, args: dict) -> str:
    """
    Invoke an MCP tool with the given arguments.

    If the result contains image content blocks (from MCP ImageContent),
    they are collected via the ImageCollector context var for later upload
    to Slack. Only text content is returned to the LLM.

    :param tool: The MCP tool object
    :param args: Arguments to pass to the tool
    :return: Tool result as string
    """
    from bugzooka.integrations.image_collector import get_collector

    if hasattr(tool, "ainvoke"):
        result = await tool.ainvoke(args)
    else:
        result = tool.invoke(args)

    # Extract content from various result formats
    # langchain-core 1.3.0+ may return ToolMessage, list of dicts, or string
    if isinstance(result, str):
        return result
    elif isinstance(result, list) and len(result) > 0:
        if isinstance(result[0], dict):
            text_parts = []
            has_images = False
            collector = get_collector()
            tool_name = getattr(tool, "name", "unknown_tool")
            for block in result:
                if block.get("type") == "text":
                    text_parts.append(block["text"])
                elif block.get("type") == "image":
                    has_images = True
                    if collector:
                        b64 = block.get("base64") or block.get("data", "")
                        mime = block.get("mime_type") or block.get(
                            "mimeType", "image/png"
                        )
                        collector.add_image(b64, mime, tool_name)

            if text_parts:
                return "\n".join(text_parts)
            if has_images:
                return "[Chart image generated successfully]"
            return str(result)
        # List of ToolMessage objects
        elif hasattr(result[0], "content"):
            return result[0].content
    elif hasattr(result, "content"):
        # ToolMessage or similar message object
        return result.content

    # Fallback to string conversion
    return str(result)


def tool_not_found_error(tool_name: str) -> dict[str, Any]:
    """
    Create a standardized error response for when an MCP tool is not found.

    :param tool_name: Name of the tool that wasn't found
    :return: Error response dictionary
    """
    available = get_available_tool_names()
    error_msg = (
        f"MCP tool '{tool_name}' not found. Is the MCP server configured and running?"
    )
    logger.error(error_msg)
    logger.info("Available tools: %s", available)
    return make_response(success=False, message=error_msg)
