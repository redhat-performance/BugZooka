"""Unit tests for general_query_analyzer."""

import sys
from types import ModuleType
from unittest.mock import AsyncMock, patch, MagicMock

import anyio

# Mock the commons module before any bugzooka imports
_mock_commons = ModuleType("commons")
_mock_inference = ModuleType("commons.inference")
_mock_inference.InferenceClient = MagicMock
_mock_inference.get_inference_client = MagicMock()
_mock_inference.analyze_with_agentic = AsyncMock()
_mock_inference.InferenceClientError = Exception
_mock_inference.InferenceAPIError = Exception
_mock_inference.InferenceIterationLimitError = Exception
_mock_inference.INFERENCE_MAX_TOKENS = 4096
_mock_inference.INFERENCE_TEMPERATURE = 0.7
_mock_inference.INFERENCE_MAX_TOOL_ITERATIONS = 10
_mock_inference.INFERENCE_API_TIMEOUT = 60
_mock_commons.inference = _mock_inference
sys.modules.setdefault("commons", _mock_commons)
sys.modules.setdefault("commons.inference", _mock_inference)

from bugzooka.analysis.general_query_analyzer import analyze_general_query  # noqa: E402


def test_analyze_general_query_success():
    conversation = [{"role": "user", "content": "how is cudn doing on 5.0?"}]

    async def _run():
        with patch(
            "bugzooka.analysis.general_query_analyzer.initialize_global_resources_async",
            new_callable=AsyncMock,
        ), patch(
            "bugzooka.analysis.general_query_analyzer.mcp_module"
        ) as mock_mcp, patch(
            "bugzooka.analysis.general_query_analyzer.analyze_with_agentic",
            new_callable=AsyncMock,
        ) as mock_agentic:
            mock_mcp.mcp_tools = [MagicMock()]
            mock_agentic.return_value = "Performance looks stable."

            result = await analyze_general_query(
                "how is cudn doing on 5.0?", conversation
            )

            assert result["success"] is True
            assert "stable" in result["message"]
            mock_agentic.assert_called_once()

            call_args = mock_agentic.call_args
            messages = call_args.kwargs.get("messages") or call_args[0][0]
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
        with patch(
            "bugzooka.analysis.general_query_analyzer.initialize_global_resources_async",
            new_callable=AsyncMock,
        ), patch(
            "bugzooka.analysis.general_query_analyzer.mcp_module"
        ) as mock_mcp, patch(
            "bugzooka.analysis.general_query_analyzer.analyze_with_agentic",
            new_callable=AsyncMock,
        ) as mock_agentic:
            mock_mcp.mcp_tools = [MagicMock()]
            mock_agentic.return_value = "ovn-controller CPU is at 9.0"

            result = await analyze_general_query(
                "what about ovn-controller CPU?", conversation
            )
            assert result["success"] is True

            call_args = mock_agentic.call_args
            messages = call_args.kwargs.get("messages") or call_args[0][0]
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

            result = await analyze_general_query(
                "hello", [{"role": "user", "content": "hello"}]
            )
            assert result["success"] is False
            assert "MCP" in result["message"]

    anyio.run(_run)


def test_analyze_empty_llm_result():
    async def _run():
        with patch(
            "bugzooka.analysis.general_query_analyzer.initialize_global_resources_async",
            new_callable=AsyncMock,
        ), patch(
            "bugzooka.analysis.general_query_analyzer.mcp_module"
        ) as mock_mcp, patch(
            "bugzooka.analysis.general_query_analyzer.analyze_with_agentic",
            new_callable=AsyncMock,
        ) as mock_agentic:
            mock_mcp.mcp_tools = [MagicMock()]
            mock_agentic.return_value = ""

            result = await analyze_general_query(
                "test", [{"role": "user", "content": "test"}]
            )
            assert result["success"] is False

    anyio.run(_run)


def test_analyze_exception_handling():
    async def _run():
        with patch(
            "bugzooka.analysis.general_query_analyzer.initialize_global_resources_async",
            new_callable=AsyncMock,
        ), patch(
            "bugzooka.analysis.general_query_analyzer.mcp_module"
        ) as mock_mcp, patch(
            "bugzooka.analysis.general_query_analyzer.analyze_with_agentic",
            new_callable=AsyncMock,
        ) as mock_agentic:
            mock_mcp.mcp_tools = [MagicMock()]
            mock_agentic.side_effect = RuntimeError("LLM timeout")

            result = await analyze_general_query(
                "test", [{"role": "user", "content": "test"}]
            )
            assert result["success"] is False
            assert "LLM timeout" in result["message"]

    anyio.run(_run)
