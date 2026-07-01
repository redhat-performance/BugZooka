"""Unit tests for general_query_analyzer."""

import sys
from types import ModuleType
from unittest.mock import AsyncMock, patch, MagicMock

import anyio

# Mock the commons module before any bugzooka imports
_mock_commons = ModuleType("commons")
_mock_inference = ModuleType("commons.inference")
_mock_inference.InferenceClient = MagicMock  # type: ignore[attr-defined]
_mock_inference.get_inference_client = MagicMock()  # type: ignore[attr-defined]
_mock_inference.analyze_with_agentic = AsyncMock()  # type: ignore[attr-defined]
_mock_inference.InferenceClientError = Exception  # type: ignore[attr-defined]
_mock_inference.InferenceAPIError = Exception  # type: ignore[attr-defined]
_mock_inference.InferenceIterationLimitError = Exception  # type: ignore[attr-defined]
_mock_inference.INFERENCE_MAX_TOKENS = 4096  # type: ignore[attr-defined]
_mock_inference.INFERENCE_TEMPERATURE = 0.7  # type: ignore[attr-defined]
_mock_inference.INFERENCE_MAX_TOOL_ITERATIONS = 10  # type: ignore[attr-defined]
_mock_inference.INFERENCE_API_TIMEOUT = 60  # type: ignore[attr-defined]
_mock_commons.inference = _mock_inference  # type: ignore[attr-defined]
sys.modules.setdefault("commons", _mock_commons)
sys.modules.setdefault("commons.inference", _mock_inference)

from bugzooka.analysis.general_query_analyzer import analyze_general_query  # noqa: E402


def _make_mock_client(return_value="Performance looks stable."):
    mock_client = MagicMock()
    mock_client.chat_with_tools_async = AsyncMock(return_value=return_value)
    return mock_client


def _patch_convert_to_openai_tool():
    return patch(
        "bugzooka.analysis.general_query_analyzer.convert_to_openai_tool",
        side_effect=lambda t: {"type": "function", "function": {"name": t.name}},
    )


def test_analyze_general_query_success():
    conversation = [{"role": "user", "content": "how is cudn doing on 5.0?"}]

    async def _run():
        mock_client = _make_mock_client("Performance looks stable.")
        mock_tool = MagicMock()
        mock_tool.name = "get_orion_performance_data"

        with patch(
            "bugzooka.analysis.general_query_analyzer.initialize_global_resources_async",
            new_callable=AsyncMock,
        ), patch(
            "bugzooka.analysis.general_query_analyzer.mcp_module"
        ) as mock_mcp, patch(
            "bugzooka.analysis.general_query_analyzer.get_inference_client",
            return_value=mock_client,
        ), _patch_convert_to_openai_tool():
            mock_mcp.mcp_tools = [mock_tool]

            result = await analyze_general_query(conversation)

            assert result["success"] is True
            assert "stable" in result["message"]
            mock_client.chat_with_tools_async.assert_called_once()

            call_kwargs = mock_client.chat_with_tools_async.call_args.kwargs
            messages = call_kwargs["messages"]
            assert messages[0]["role"] == "system"
            assert messages[1]["role"] == "user"
            assert messages[1]["content"] == "how is cudn doing on 5.0?"

    anyio.run(_run)


def test_analyze_with_conversation_history():
    conversation = [
        {"role": "user", "content": "report on cudn for 5.0"},
        {"role": "assistant", "content": "Here are the metrics..."},
        {"role": "user", "content": "what about ovn-controller CPU?"},
    ]

    async def _run():
        mock_client = _make_mock_client("ovn-controller CPU is at 9.0")
        mock_tool = MagicMock()
        mock_tool.name = "get_orion_metrics"

        with patch(
            "bugzooka.analysis.general_query_analyzer.initialize_global_resources_async",
            new_callable=AsyncMock,
        ), patch(
            "bugzooka.analysis.general_query_analyzer.mcp_module"
        ) as mock_mcp, patch(
            "bugzooka.analysis.general_query_analyzer.get_inference_client",
            return_value=mock_client,
        ), _patch_convert_to_openai_tool():
            mock_mcp.mcp_tools = [mock_tool]

            result = await analyze_general_query(conversation)
            assert result["success"] is True

            call_kwargs = mock_client.chat_with_tools_async.call_args.kwargs
            messages = call_kwargs["messages"]
            assert len(messages) == 4  # system + 3 conversation messages
            assert messages[1]["content"] == "report on cudn for 5.0"
            assert messages[2]["role"] == "assistant"
            assert messages[3]["content"] == "what about ovn-controller CPU?"

    anyio.run(_run)


def test_analyze_no_mcp_tools():
    async def _run():
        with patch(
            "bugzooka.analysis.general_query_analyzer.initialize_global_resources_async",
            new_callable=AsyncMock,
        ), patch("bugzooka.analysis.general_query_analyzer.mcp_module") as mock_mcp:
            mock_mcp.mcp_tools = []

            result = await analyze_general_query([{"role": "user", "content": "hello"}])
            assert result["success"] is False
            assert "MCP" in result["message"]

    anyio.run(_run)


def test_analyze_empty_llm_result():
    async def _run():
        mock_client = _make_mock_client("")
        mock_tool = MagicMock()
        mock_tool.name = "test_tool"

        with patch(
            "bugzooka.analysis.general_query_analyzer.initialize_global_resources_async",
            new_callable=AsyncMock,
        ), patch(
            "bugzooka.analysis.general_query_analyzer.mcp_module"
        ) as mock_mcp, patch(
            "bugzooka.analysis.general_query_analyzer.get_inference_client",
            return_value=mock_client,
        ), _patch_convert_to_openai_tool():
            mock_mcp.mcp_tools = [mock_tool]

            result = await analyze_general_query([{"role": "user", "content": "test"}])
            assert result["success"] is False

    anyio.run(_run)


def test_analyze_exception_handling():
    async def _run():
        mock_client = MagicMock()
        mock_client.chat_with_tools_async = AsyncMock(
            side_effect=RuntimeError("LLM timeout")
        )
        mock_tool = MagicMock()
        mock_tool.name = "test_tool"

        with patch(
            "bugzooka.analysis.general_query_analyzer.initialize_global_resources_async",
            new_callable=AsyncMock,
        ), patch(
            "bugzooka.analysis.general_query_analyzer.mcp_module"
        ) as mock_mcp, patch(
            "bugzooka.analysis.general_query_analyzer.get_inference_client",
            return_value=mock_client,
        ), _patch_convert_to_openai_tool():
            mock_mcp.mcp_tools = [mock_tool]

            result = await analyze_general_query([{"role": "user", "content": "test"}])
            assert result["success"] is False
            assert "LLM timeout" in result["message"]

    anyio.run(_run)


def test_execute_tool_routes_through_invoke_mcp_tool():
    """Verify the custom execute_tool closure calls invoke_mcp_tool."""
    conversation = [{"role": "user", "content": "test"}]

    async def _run():
        mock_client = _make_mock_client("done")
        mock_tool = MagicMock()
        mock_tool.name = "get_orion_configs"

        with patch(
            "bugzooka.analysis.general_query_analyzer.initialize_global_resources_async",
            new_callable=AsyncMock,
        ), patch(
            "bugzooka.analysis.general_query_analyzer.mcp_module"
        ) as mock_mcp, patch(
            "bugzooka.analysis.general_query_analyzer.get_inference_client",
            return_value=mock_client,
        ), _patch_convert_to_openai_tool(), patch(
            "bugzooka.analysis.general_query_analyzer.invoke_mcp_tool",
            new_callable=AsyncMock,
            return_value="tool result",
        ) as mock_invoke:
            mock_mcp.mcp_tools = [mock_tool]

            await analyze_general_query(conversation)

            # Extract the execute_tool_func that was passed to chat_with_tools_async
            call_kwargs = mock_client.chat_with_tools_async.call_args.kwargs
            execute_func = call_kwargs["execute_tool_func"]

            # Call it directly to verify it routes through invoke_mcp_tool
            result = await execute_func("get_orion_configs", {})
            mock_invoke.assert_called_once_with(mock_tool, {})
            assert result == "tool result"

    anyio.run(_run)


def test_execute_tool_unknown_tool():
    """Verify execute_tool returns error for unknown tool names."""
    conversation = [{"role": "user", "content": "test"}]

    async def _run():
        mock_client = _make_mock_client("done")
        mock_tool = MagicMock()
        mock_tool.name = "known_tool"

        with patch(
            "bugzooka.analysis.general_query_analyzer.initialize_global_resources_async",
            new_callable=AsyncMock,
        ), patch(
            "bugzooka.analysis.general_query_analyzer.mcp_module"
        ) as mock_mcp, patch(
            "bugzooka.analysis.general_query_analyzer.get_inference_client",
            return_value=mock_client,
        ), _patch_convert_to_openai_tool():
            mock_mcp.mcp_tools = [mock_tool]

            await analyze_general_query(conversation)

            call_kwargs = mock_client.chat_with_tools_async.call_args.kwargs
            execute_func = call_kwargs["execute_tool_func"]

            result = await execute_func("nonexistent_tool", {})
            assert "not found" in result

    anyio.run(_run)
