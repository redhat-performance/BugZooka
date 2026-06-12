"""
Inference client - re-exports from py-commons.

This module re-exports all inference client functionality from the py-commons
shared library, maintaining backward compatibility with existing BugZooka code.
"""

from commons.inference import (
    InferenceClient,
    get_inference_client,
    analyze_with_agentic,
    InferenceClientError,
    InferenceAPIError,
    InferenceIterationLimitError,
    INFERENCE_MAX_TOKENS,
    INFERENCE_TEMPERATURE,
    INFERENCE_MAX_TOOL_ITERATIONS,
    INFERENCE_API_TIMEOUT,
)

InferenceAPIUnavailableError = InferenceAPIError
AgentAnalysisLimitExceededError = InferenceIterationLimitError

__all__ = [
    "InferenceClient",
    "get_inference_client",
    "analyze_with_agentic",
    "InferenceClientError",
    "InferenceAPIError",
    "InferenceIterationLimitError",
    "InferenceAPIUnavailableError",
    "AgentAnalysisLimitExceededError",
    "INFERENCE_MAX_TOKENS",
    "INFERENCE_TEMPERATURE",
    "INFERENCE_MAX_TOOL_ITERATIONS",
    "INFERENCE_API_TIMEOUT",
]
