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
    Initializes the MCP client and retrieves tools.
    This version includes graceful handling for a missing mcp_config.json file.
    Registers ES encryption interceptor for channel-based routing.
    """
    global mcp_client, mcp_tools
    # Initialize the MCP client from a config file.
    if mcp_client is not None:
        return

    try:
        with open(mcp_config_path, "r") as f:
            config = json.load(f)

        # Create ES encryption interceptor for channel-based routing
        from bugzooka.integrations.mcp_interceptors import create_es_interceptor
        from bugzooka.core.config import get_es_channel_mappings

        # Load channel to ES server mappings
        try:
            es_channel_mappings = get_es_channel_mappings()
            es_interceptor = create_es_interceptor(es_channel_mappings)
            logger.info(
                "ES encryption interceptor created with %d channel mappings",
                len(es_channel_mappings)
            )
        except ValueError as e:
            logger.warning(
                "ES channel mappings not configured: %s. "
                "ES encryption interceptor will not be registered. "
                "orion-mcp will use default ES_SERVER from environment.",
                str(e)
            )
            es_interceptor = None

        # Initialize MCP client with interceptor (if configured)
        if es_interceptor:
            mcp_client = MultiServerMCPClient(
                config["mcp_servers"],
                tool_interceptors=[es_interceptor]
            )
            logger.info("MCP client initialized with ES encryption interceptor")
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

    :param tool: The MCP tool object
    :param args: Arguments to pass to the tool
    :return: Tool result as string
    """
    if hasattr(tool, "ainvoke"):
        result = await tool.ainvoke(args)
    else:
        result = tool.invoke(args)

    # Extract content from various result formats
    # langchain-core 1.3.0+ may return ToolMessage, list of dicts, or string
    if isinstance(result, str):
        return result
    elif isinstance(result, list) and len(result) > 0:
        # List of message dicts: [{'type': 'text', 'text': '...', 'id': '...'}]
        if isinstance(result[0], dict) and 'text' in result[0]:
            return result[0]['text']
        # List of ToolMessage objects
        elif hasattr(result[0], 'content'):
            return result[0].content
    elif hasattr(result, 'content'):
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
