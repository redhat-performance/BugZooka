"""
Tests for MCP interceptors module.

Tests HeaderEncryptionInterceptor (encrypted ES config MCP header).
"""

import os
import json
import pytest
from unittest.mock import AsyncMock, patch
from contextvars import copy_context

from bugzooka.integrations.mcp_interceptors import (
    HeaderEncryptionInterceptor,
    current_channel,
    create_header_encryption_interceptor,
)
from tests.helpers import aes256_gcm_decrypt_blob, random_aes256_gcm_key_b64


# Mock MCP types for testing
class MockMCPToolCallRequest:
    """Mock MCPToolCallRequest for testing."""

    def __init__(self, name, args, headers=None):
        self.name = name
        self.args = args
        self.headers = headers or {}

    def override(self, **kwargs):
        """Create new request with overrides."""
        new_name = kwargs.get('name', self.name)
        new_args = kwargs.get('args', self.args)
        new_headers = kwargs.get('headers', self.headers)
        return MockMCPToolCallRequest(new_name, new_args, new_headers)


class MockMCPToolCallResult:
    """Mock MCPToolCallResult for testing."""

    def __init__(self, content):
        self.content = content


class TestHeaderEncryptionInterceptor:
    """Test HeaderEncryptionInterceptor class."""

    @pytest.fixture
    def valid_encryption_key(self):
        key = random_aes256_gcm_key_b64()
        with patch.dict(os.environ, {"HEADER_SYMMETRIC_KEY": key}):
            yield key

    @pytest.fixture
    def es_channel_mappings(self):
        """Provide sample ES channel mappings."""
        return {
            "C12345": {
                "es_server": "https://es-prod.example.com:9200",
                "es_metadata_index": "perf_scale_ci*",
                "es_benchmark_index": "ripsaw-kube-burner-*"
            },
            "C67890": {
                "es_server": "https://es-staging.example.com:9200",
            }
        }

    @pytest.fixture
    def interceptor(self, es_channel_mappings):
        return HeaderEncryptionInterceptor(es_channel_mappings)

    @pytest.mark.asyncio
    async def test_interceptor_adds_header_for_orion_tool(
        self, valid_encryption_key, interceptor
    ):
        """Test that interceptor adds encrypted header for orion tools."""
        # Set channel context
        current_channel.set("C12345")

        request = MockMCPToolCallRequest(
            name="has_nightly_regressed",
            args={"nightly_version": "4.22.0-0.nightly-2026-02-03-002928"}
        )

        # Mock handler
        handler = AsyncMock(return_value=MockMCPToolCallResult("test result"))

        # Call interceptor
        result = await interceptor(request, handler)

        # Check handler was called with modified request
        assert handler.called
        modified_request = handler.call_args[0][0]

        assert "X-Encrypted-Context" in modified_request.headers

        encrypted_blob = modified_request.headers["X-Encrypted-Context"]
        decrypted_json = aes256_gcm_decrypt_blob(encrypted_blob, valid_encryption_key)
        decrypted_config = json.loads(decrypted_json)

        assert decrypted_config["es_server"] == "https://es-prod.example.com:9200"
        assert decrypted_config["es_metadata_index"] == "perf_scale_ci*"
        assert decrypted_config["es_benchmark_index"] == "ripsaw-kube-burner-*"

    @pytest.mark.asyncio
    async def test_interceptor_skips_non_orion_tools(self, valid_encryption_key, interceptor):
        """Test that interceptor skips non-orion tools."""
        current_channel.set("C12345")

        request = MockMCPToolCallRequest(
            name="some_other_tool",
            args={"param": "value"}
        )

        handler = AsyncMock(return_value=MockMCPToolCallResult("test result"))

        result = await interceptor(request, handler)

        # Handler should be called with original request (no header added)
        modified_request = handler.call_args[0][0]
        assert "X-Encrypted-Context" not in modified_request.headers

    @pytest.mark.asyncio
    async def test_interceptor_skips_when_no_channel(self, valid_encryption_key, interceptor):
        """Test that interceptor skips when no channel is set in context."""
        # Don't set current_channel (defaults to None)
        current_channel.set(None)

        request = MockMCPToolCallRequest(
            name="has_nightly_regressed",
            args={"nightly_version": "4.22.0-0.nightly-2026-02-03-002928"}
        )

        handler = AsyncMock(return_value=MockMCPToolCallResult("test result"))

        result = await interceptor(request, handler)

        # Should skip adding header
        modified_request = handler.call_args[0][0]
        assert "X-Encrypted-Context" not in modified_request.headers

    @pytest.mark.asyncio
    async def test_interceptor_preserves_existing_headers(
        self, valid_encryption_key, interceptor
    ):
        """Test that interceptor preserves existing headers."""
        current_channel.set("C12345")

        request = MockMCPToolCallRequest(
            name="has_nightly_regressed",
            args={},
            headers={"Authorization": "Bearer token123", "X-Custom": "value"}
        )

        handler = AsyncMock(return_value=MockMCPToolCallResult("test result"))

        await interceptor(request, handler)

        modified_request = handler.call_args[0][0]

        # Should preserve existing headers
        assert modified_request.headers["Authorization"] == "Bearer token123"
        assert modified_request.headers["X-Custom"] == "value"

        assert "X-Encrypted-Context" in modified_request.headers

    @pytest.mark.asyncio
    async def test_interceptor_handles_encryption_error(
        self, valid_encryption_key, interceptor
    ):
        """Test that interceptor handles encryption errors gracefully."""
        # Set channel that doesn't exist in mappings
        current_channel.set("C99999")

        request = MockMCPToolCallRequest(
            name="has_nightly_regressed",
            args={}
        )

        handler = AsyncMock(return_value=MockMCPToolCallResult("test result"))

        # Should not raise, should pass through without header
        result = await interceptor(request, handler)

        modified_request = handler.call_args[0][0]
        assert "X-Encrypted-Context" not in modified_request.headers

    @pytest.mark.asyncio
    async def test_interceptor_returns_handler_result(self, valid_encryption_key, interceptor):
        """Test that interceptor returns the handler's result."""
        current_channel.set("C12345")

        request = MockMCPToolCallRequest(name="has_nightly_regressed", args={})

        expected_result = MockMCPToolCallResult("expected content")
        handler = AsyncMock(return_value=expected_result)

        result = await interceptor(request, handler)

        assert result == expected_result


class TestIsOrionTool:
    """Test _is_orion_tool method."""

    @pytest.fixture
    def interceptor(self):
        return HeaderEncryptionInterceptor({})

    def test_recognizes_orion_tools(self, interceptor):
        """Test that all orion-mcp tools are recognized."""
        orion_tools = [
            "get_orion_metrics",
            "get_orion_metrics_with_meta",
            "openshift_report_on",
            "get_orion_performance_data",
            "openshift_report_on_pr",
            "has_openshift_regressed",
            "has_networking_regressed",
            "metrics_correlation",
            "has_nightly_regressed",
        ]

        for tool_name in orion_tools:
            assert interceptor._is_orion_tool(tool_name), f"{tool_name} should be recognized as orion tool"

    def test_rejects_non_orion_tools(self, interceptor):
        """Test that non-orion tools are not recognized."""
        non_orion_tools = [
            "get_release_date",  # Doesn't use ES
            "get_orion_configs",  # Doesn't use ES
            "some_other_tool",
            "random_function",
        ]

        for tool_name in non_orion_tools:
            assert not interceptor._is_orion_tool(tool_name), f"{tool_name} should not be recognized as orion tool"


class TestCurrentChannelContextVar:
    """Test current_channel ContextVar isolation."""

    def test_context_var_isolation(self):
        """Test that ContextVar is isolated per context."""
        # Set value in current context
        current_channel.set("C12345")
        assert current_channel.get() == "C12345"

        # Copy context and modify
        ctx = copy_context()

        def modify_context():
            current_channel.set("C67890")
            return current_channel.get()

        # Run in copied context
        result = ctx.run(modify_context)
        assert result == "C67890"

        # Original context should be unchanged
        assert current_channel.get() == "C12345"

    def test_context_var_default(self):
        """Test that ContextVar has None default."""
        # Reset to default
        current_channel.set(None)
        assert current_channel.get() is None


class TestCreateHeaderEncryptionInterceptor:
    """Test create_header_encryption_interceptor factory."""

    def test_creates_interceptor_instance(self):
        es_config_map = {
            "C12345": {
                "es_server": "https://es-prod.example.com:9200",
                "es_metadata_index": "perf_scale_ci*",
                "es_benchmark_index": "ripsaw-kube-burner-*",
            }
        }

        interceptor = create_header_encryption_interceptor(es_config_map)

        assert isinstance(interceptor, HeaderEncryptionInterceptor)
        assert interceptor.es_config_map == es_config_map

    def test_creates_interceptor_with_empty_mappings(self):
        interceptor = create_header_encryption_interceptor({})

        assert isinstance(interceptor, HeaderEncryptionInterceptor)
        assert interceptor.es_config_map == {}


class TestInterceptorIntegration:
    """Integration tests for interceptor in realistic scenarios."""

    @pytest.fixture
    def valid_encryption_key(self):
        key = random_aes256_gcm_key_b64()
        with patch.dict(os.environ, {"HEADER_SYMMETRIC_KEY": key}):
            yield key

    @pytest.mark.asyncio
    async def test_multiple_channels_different_configs(self, valid_encryption_key):
        """Test that different channels get different ES configs."""
        mappings = {
            "C_PROD": {
                "es_server": "https://es-prod.example.com:9200",
                "es_metadata_index": "prod_ci*",
            },
            "C_STAGING": {
                "es_server": "https://es-staging.example.com:9200",
                "es_metadata_index": "staging_ci*",
            }
        }

        interceptor = HeaderEncryptionInterceptor(mappings)
        handler = AsyncMock(return_value=MockMCPToolCallResult("result"))
        request = MockMCPToolCallRequest(name="has_nightly_regressed", args={})

        # Test prod channel
        current_channel.set("C_PROD")
        await interceptor(request, handler)
        prod_request = handler.call_args[0][0]
        prod_encrypted = prod_request.headers["X-Encrypted-Context"]
        prod_config = json.loads(
            aes256_gcm_decrypt_blob(prod_encrypted, valid_encryption_key)
        )

        assert prod_config["es_server"] == "https://es-prod.example.com:9200"
        assert prod_config["es_metadata_index"] == "prod_ci*"

        # Test staging channel
        handler.reset_mock()
        current_channel.set("C_STAGING")
        await interceptor(request, handler)
        staging_request = handler.call_args[0][0]
        staging_encrypted = staging_request.headers["X-Encrypted-Context"]
        staging_config = json.loads(
            aes256_gcm_decrypt_blob(staging_encrypted, valid_encryption_key)
        )

        assert staging_config["es_server"] == "https://es-staging.example.com:9200"
        assert staging_config["es_metadata_index"] == "staging_ci*"

        # Encrypted blobs should be different
        assert prod_encrypted != staging_encrypted
