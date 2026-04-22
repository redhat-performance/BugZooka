"""
MCP Tool Interceptors for BugZooka.

Implements request/response interception for MCP tool calls using the
langchain-mcp-adapters ToolCallInterceptor protocol.

Key components:
- current_channel: ContextVar for tracking Slack channel across async calls
- ESEncryptionInterceptor: Injects encrypted ES_SERVER config into request headers
"""
import logging
from contextvars import ContextVar
from typing import Callable, Awaitable

from langchain_mcp_adapters.interceptors import (
    ToolCallInterceptor,
    MCPToolCallRequest,
    MCPToolCallResult,
)

from bugzooka.core.es_encryption import encrypt_es_config

logger = logging.getLogger(__name__)


# Context variable to track current Slack channel
# This is set by analyzers before calling MCP tools and read by interceptor
# Automatically isolated per thread/async task (no race conditions)
current_channel: ContextVar[str] = ContextVar('current_channel', default=None)


class ESEncryptionInterceptor:
    """
    Interceptor that adds encrypted ES_SERVER config to MCP request headers.

    For each orion-mcp tool call:
    1. Gets current Slack channel from context variable
    2. Looks up ES_SERVER for that channel from mappings
    3. Encrypts ES_SERVER config using AES-256-GCM
    4. Adds "X-Encrypted-ES-Context" header to request
    5. Passes modified request to next handler

    Thread-safe and async-safe using context variables for channel tracking.
    """

    def __init__(self, es_channel_mappings: dict):
        """
        Initialize ES encryption interceptor.

        :param es_channel_mappings: Dict mapping channel_id -> es_config dict
                                   es_config dict contains: es_server, es_metadata_index, es_benchmark_index
                                   Example: {"C12345": {"es_server": "https://es-prod.com:9200",
                                             "es_metadata_index": "perf_scale_ci*",
                                             "es_benchmark_index": "ripsaw-kube-burner-*"}}
        """
        self.es_channel_mappings = es_channel_mappings
        logger.info(
            "ESEncryptionInterceptor initialized with %d channel mappings",
            len(es_channel_mappings)
        )
        logger.debug("Channels configured: %s", list(es_channel_mappings.keys()))

    async def __call__(
        self,
        request: MCPToolCallRequest,
        handler: Callable[[MCPToolCallRequest], Awaitable[MCPToolCallResult]],
    ) -> MCPToolCallResult:
        """
        Intercept MCP tool call and add encrypted ES config header if needed.

        Implements the ToolCallInterceptor protocol from langchain-mcp-adapters.

        :param request: Original tool call request with name, args, headers, etc.
        :param handler: Next handler in the interceptor chain
        :return: Tool call result from downstream handlers
        """
        # Get current Slack channel from context variable
        # This was set by the analyzer (e.g., analyze_pr_with_gemini)
        channel_id = current_channel.get()

        # Only add encryption header for orion-mcp tools when we have a channel
        if channel_id and self._is_orion_tool(request.name):
            logger.debug(
                "Intercepting orion-mcp tool call: %s (channel: %s)",
                request.name,
                channel_id
            )

            try:
                # Encrypt ES config for this channel
                encrypted_blob = encrypt_es_config(channel_id, self.es_channel_mappings)

                # Add encrypted config to request headers
                # Preserve any existing headers that might be present
                new_headers = {
                    **(request.headers or {}),  # Preserve existing headers
                    "X-Encrypted-ES-Context": encrypted_blob,
                }

                logger.debug(
                    "Added X-Encrypted-ES-Context header for channel %s (%d bytes)",
                    channel_id,
                    len(encrypted_blob)
                )

                # Create modified request with new headers
                # request.override() creates a new immutable request instance
                modified_request = request.override(headers=new_headers)

                # Call next handler in chain with modified request
                return await handler(modified_request)

            except Exception as e:
                logger.error(
                    "Error encrypting ES config for channel %s: %s",
                    channel_id,
                    str(e),
                    exc_info=True
                )
                # On error, pass through unmodified request
                # This allows fallback to orion-mcp's default ES_SERVER
                logger.warning(
                    "Falling back to unmodified request (orion-mcp will use default ES_SERVER)"
                )

        # For non-orion tools or when no channel is set, pass through unchanged
        return await handler(request)

    def _is_orion_tool(self, tool_name: str) -> bool:
        """
        Check if tool is from orion-mcp server.

        All orion-mcp tools follow the naming convention: orion_*

        :param tool_name: Name of the MCP tool
        :return: True if tool is from orion-mcp, False otherwise
        """
        return tool_name.startswith("orion_")


def create_es_interceptor(es_channel_mappings: dict) -> ESEncryptionInterceptor:
    """
    Factory function to create ES encryption interceptor.

    :param es_channel_mappings: Dict mapping channel_id -> es_config dict
                                es_config dict contains: es_server, es_metadata_index, es_benchmark_index
    :return: Configured ESEncryptionInterceptor instance

    Example:
        >>> mappings = {
        ...     "C12345": {
        ...         "es_server": "https://es-prod.com:9200",
        ...         "es_metadata_index": "perf_scale_ci*",
        ...         "es_benchmark_index": "ripsaw-kube-burner-*"
        ...     }
        ... }
        >>> interceptor = create_es_interceptor(mappings)
        >>> # Pass to MultiServerMCPClient:
        >>> client = MultiServerMCPClient(servers, tool_interceptors=[interceptor])
    """
    return ESEncryptionInterceptor(es_channel_mappings)
