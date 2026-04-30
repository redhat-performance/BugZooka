"""
MCP Tool Interceptors for BugZooka.

Implements request/response interception for MCP tool calls using the
langchain-mcp-adapters ToolCallInterceptor protocol.

Key components:
- current_channel: ContextVar for tracking Slack channel across async calls
- HeaderEncryptionInterceptor: MCP hook for adding encrypted headers; today it
  attaches per-channel ES config (es_server, indices) for orion-mcp.
"""
import logging
from contextvars import ContextVar
from typing import Callable, Awaitable

from langchain_mcp_adapters.interceptors import (
    ToolCallInterceptor,
    MCPToolCallRequest,
    MCPToolCallResult,
)

from bugzooka.core.header_encryption import encrypt_es_config

logger = logging.getLogger(__name__)


# Context variable to track current Slack channel
# This is set by analyzers before calling MCP tools and read by interceptor
current_channel: ContextVar[str] = ContextVar('current_channel', default=None)


class HeaderEncryptionInterceptor:
    """
    Generic MCP interceptor for injecting encrypted HTTP headers on tool calls.

    Current use case (orion-mcp): for tools that query Elasticsearch, attach an
    ``X-Encrypted-Context`` header whose value is AES-256-GCM ciphertext over the
    channel's ES config JSON (``es_server`` and optional index fields from
    ``es_config_map``). orion-mcp decrypts with ``HEADER_SYMMETRIC_KEY`` and uses
    the config for queries. The same class pattern can later cover other encrypted
    headers without renaming the interceptor.

    For each matching tool call:
    1. Read Slack channel from ``current_channel`` (set by analyzers before MCP calls).
    2. Resolve ES config for that channel from ``es_config_map``.
    3. Encrypt JSON via ``encrypt_es_config`` (uses ``HEADER_SYMMETRIC_KEY``).
    4. Merge ``X-Encrypted-Context`` into request headers (preserving existing headers).
    5. Forward the modified request to the next handler.

    Thread-safe and async-safe: channel is tracked with a ``ContextVar``.
    """

    def __init__(self, es_config_map: dict):
        """
        Initialize the header-encryption interceptor.

        :param es_config_map: Slack channel_id -> ES config dict. Each dict must
            include ``es_server``; optional keys include ``es_metadata_index`` and
            ``es_benchmark_index`` (same shape as ``ES_CHANNEL_MAPPINGS`` from config).

        Example map entry::

            {
                "C12345": {
                    "es_server": "https://es-prod.example.com:9200",
                    "es_metadata_index": "perf_scale_ci*",
                    "es_benchmark_index": "ripsaw-kube-burner-*",
                }
            }
        """
        self.es_config_map = es_config_map
        logger.info(
            "HeaderEncryptionInterceptor initialized with %d channel ES configs",
            len(es_config_map),
        )
        logger.debug("Channels configured: %s", list(es_config_map.keys()))

    async def __call__(
        self,
        request: MCPToolCallRequest,
        handler: Callable[[MCPToolCallRequest], Awaitable[MCPToolCallResult]],
    ) -> MCPToolCallResult:
        """
        Intercept an MCP tool call and add the encrypted ES config header when applicable.

        Implements the ``ToolCallInterceptor`` protocol from langchain-mcp-adapters.

        :param request: Original tool call (name, args, headers, etc.).
        :param handler: Next handler in the interceptor chain.
        :return: Result from downstream handlers.
        """
        # Set by analyzers (e.g. analyze_pr_with_gemini) before invoking MCP tools.
        channel_id = current_channel.get()

        # Only enrich orion-mcp tools that hit Elasticsearch; other tools pass through.
        if channel_id and self._is_orion_tool(request.name):
            logger.debug(
                "Intercepting orion-mcp tool call: %s (channel: %s)",
                request.name,
                channel_id,
            )

            try:
                encrypted_blob = encrypt_es_config(channel_id, self.es_config_map)
                # Preserve any existing headers (e.g. Authorization).
                new_headers = {
                    **(request.headers or {}),
                    "X-Encrypted-Context": encrypted_blob,
                }

                logger.debug(
                    "Added X-Encrypted-Context (encrypted ES config) for channel %s (%d bytes)",
                    channel_id,
                    len(encrypted_blob),
                )

                # Immutable request: override() returns a new instance with merged headers.
                modified_request = request.override(headers=new_headers)
                return await handler(modified_request)

            except Exception as e:
                logger.error(
                    "Error encrypting ES config header for channel %s: %s",
                    channel_id,
                    str(e),
                    exc_info=True,
                )
                # Do not fail the tool call; orion-mcp can fall back to default ES_SERVER.
                logger.warning(
                    "Falling back to unmodified request (orion-mcp may use default ES_SERVER)"
                )

        # Non-orion tools, missing channel, or encryption error path: unchanged request.
        return await handler(request)

    def _is_orion_tool(self, tool_name: str) -> bool:
        """
        Return True if this orion-mcp tool should receive the encrypted ES config header.

        Only tools that query Elasticsearch need the header. Tools such as
        ``get_release_date`` or ``get_orion_configs`` that do not use ES are excluded
        (not listed in ``es_tools``).
        """
        es_tools = {
            "get_orion_metrics",
            "get_orion_metrics_with_meta",
            "openshift_report_on",
            "get_orion_performance_data",
            "openshift_report_on_pr",
            "has_openshift_regressed",
            "has_networking_regressed",
            "metrics_correlation",
            "has_nightly_regressed",
        }
        return tool_name in es_tools


def create_header_encryption_interceptor(
    es_config_map: dict,
) -> HeaderEncryptionInterceptor:
    """
    Factory: build a ``HeaderEncryptionInterceptor`` from a channel -> ES config map.

    Intended for wiring into ``MultiServerMCPClient(..., tool_interceptors=[...])``
    after loading mappings (e.g. from ``get_es_channel_mappings()``).

    :param es_config_map: Same structure as ``ES_CHANNEL_MAPPINGS`` (see class docstring).
    :return: Configured interceptor instance.

    Example::

        from bugzooka.integrations.mcp_interceptors import (
            create_header_encryption_interceptor,
        )

        interceptor = create_header_encryption_interceptor(es_config_map)
        client = MultiServerMCPClient(servers, tool_interceptors=[interceptor])
    """
    return HeaderEncryptionInterceptor(es_config_map)
