import json
import logging
from urllib.parse import urlparse

import httpx

from langchain_mcp_adapters.client import MultiServerMCPClient

logger = logging.getLogger(__name__)

mcp_client = None
mcp_tools = []


async def _check_server_connectivity(server_name: str, server_config: dict, timeout: float = 5.0) -> tuple[bool, str]:
    """
    Check if an MCP server is reachable.
    
    :param server_name: Name of the server (for logging)
    :param server_config: Server configuration dict with 'url' key
    :param timeout: Connection timeout in seconds
    :return: Tuple of (is_reachable, error_message)
    """
    url = server_config.get("url", "")
    if not url:
        return False, f"No URL configured for server '{server_name}'"
    
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            # Try a HEAD request to the base URL to check connectivity
            # We expect it might return 404 or other codes, but connection should work
            await client.head(url)
            return True, ""
    except httpx.ConnectError as e:
        return False, f"Connection refused - server may not be running"
    except httpx.TimeoutException:
        return False, f"Connection timed out after {timeout}s - server not responding"
    except Exception as e:
        return False, f"Unexpected error connecting to server: {type(e).__name__}: {e}"


async def _validate_mcp_servers(servers_config: dict) -> list[str]:
    """
    Validate connectivity to all configured MCP servers.
    
    :param servers_config: Dictionary of server configurations
    :return: List of error messages for unreachable servers
    """
    errors = []
    
    for server_name, server_config in servers_config.items():
        url = server_config.get("url", "unknown")
        logger.info(f"Checking connectivity to MCP server '{server_name}' at {url}...")
        
        is_reachable, error_msg = await _check_server_connectivity(server_name, server_config)
        
        if is_reachable:
            logger.info(f"✓ MCP server '{server_name}' is reachable")
        else:
            error = f"✗ MCP server '{server_name}': {error_msg}"
            logger.error(error)
            errors.append(error)
    
    return errors


async def initialize_global_resources_async(mcp_config_path: str = "mcp_config.json"):
    """
    Initializes the MCP client and retrieves tools.
    
    This version includes:
    - Graceful handling for a missing mcp_config.json file
    - Pre-validation of server connectivity with clear error messages
    
    :param mcp_config_path: Path to the MCP configuration JSON file
    :raises MCPConnectionError: If any configured MCP server is unreachable
    """
    global mcp_client, mcp_tools
    
    # Skip if already initialized
    if mcp_client is not None:
        return

    try:
        with open(mcp_config_path, 'r') as f:
            config = json.load(f)

        servers_config = config.get('mcp_servers', {})
        
        if not servers_config:
            logger.warning("No MCP servers configured. Running without external tools.")
            mcp_tools = []
            mcp_client = MultiServerMCPClient({})
            return
        
        # Validate connectivity to all servers first
        logger.info(f"Validating {len(servers_config)} MCP server(s)...")
        errors = await _validate_mcp_servers(servers_config)
        
        if errors:
            error_summary = "\n".join(errors)
            raise MCPConnectionError(
                f"Failed to connect to {len(errors)} MCP server(s):\n{error_summary}\n\n"
                f"Please ensure all configured MCP servers are running, or remove "
                f"unavailable servers from configuration file"
            )
        
        # Initialize client with all configured servers
        logger.info(f"Initializing MCP client with {len(servers_config)} server(s): "
                   f"{list(servers_config.keys())}")
        mcp_client = MultiServerMCPClient(servers_config)
        mcp_tools = await mcp_client.get_tools()
        logger.info(f"MCP configuration loaded and {len(mcp_tools)} tools retrieved.")

    except FileNotFoundError:
        logger.warning(
            f"MCP configuration file not found. Running without external tools."
        )
        mcp_tools = []
        mcp_client = MultiServerMCPClient({})
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in MCP configuration file: {e}")
        raise
    except MCPConnectionError:
        # Re-raise our custom error with the detailed message
        raise
    except Exception as e:
        logger.error(f"Error initializing MCP tools: {e}", exc_info=True)
        raise


class MCPConnectionError(Exception):
    """Raised when MCP server connection fails with details about which server(s) are unavailable."""
    pass