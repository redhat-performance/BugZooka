import logging
import os
import ssl
import httpx

from openai import OpenAI
from bugzooka.core.constants import (
    INFERENCE_MAX_TOKENS,
    INFERENCE_TEMPERATURE,
)
from bugzooka.integrations.inference import InferenceAPIUnavailableError


logger = logging.getLogger(__name__)


class GeminiClient:
    """
    Gemini API client that implements OpenAI-compatible interface.
    """

    def __init__(self, api_key=None, base_url=None, verify_ssl=None):
        """
        Initialize Gemini client.

        :param api_key: Gemini API key (defaults to GEMINI_API_KEY env var)
        :param base_url: Gemini API base URL (defaults to GEMINI_API_URL env var)
        :param verify_ssl: Whether to verify SSL certificates (defaults to GEMINI_VERIFY_SSL env var, then True)
        """
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY environment variable is required")

        self.base_url = base_url or os.getenv("GEMINI_API_URL")
        if not self.base_url:
            raise ValueError(
                "GEMINI_API_URL environment variable or base_url parameter is required"
            )

        # SSL verification configuration
        if verify_ssl is None:
            verify_ssl_env = os.getenv("GEMINI_VERIFY_SSL").lower()
            verify_ssl = verify_ssl_env == "true"

        # Create custom HTTP client with SSL configuration
        if not verify_ssl:
            logger.warning("SSL certificate verification disabled for Gemini API")
            # Create SSL context that doesn't verify certificates
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

            http_client = httpx.Client(verify=False)
        else:
            http_client = None  # Use default

        self.client = OpenAI(
            api_key=self.api_key, base_url=self.base_url, http_client=http_client
        )

    def chat_completions_create(self, messages, model="gemini-2.0-flash", **kwargs):
        """
        Create chat completion using Gemini API.

        :param messages: List of message dictionaries
        :param model: Gemini model name (default: gemini-2.0-flash)
        :param kwargs: Additional parameters
        :return: Chat completion response
        """
        try:
            logger.info("Calling Gemini API: URL=%s, Model=%s", self.base_url, model)
            response = self.client.chat.completions.create(
                model=model, messages=messages, **kwargs
            )
            logger.info("Gemini API call successful")
            return response
        except Exception as e:
            # Enhanced error logging with more details
            error_type = type(e).__name__
            error_msg = str(e)

            logger.error("Error calling Gemini API:")
            logger.error("  - Error Type: %s", error_type)
            logger.error("  - Error Message: %s", error_msg)
            logger.error("  - Base URL: %s", self.base_url)
            logger.error("  - Model: %s", model)

            raise InferenceAPIUnavailableError(
                f"Gemini API error ({error_type}): {error_msg}"
            ) from e


def analyze_log_with_gemini(
    product: str, product_config: dict, error_summary: str, model="gemini-2.0-flash"
):
    """
    Analyzes log summaries using Gemini API with product-specific prompts.

    :param product: Product name (e.g., "OPENSHIFT", "ANSIBLE", or "GENERIC")
    :param product_config: Config dict with prompt, endpoint, token, and model info
    :param error_summary: Error summary text to analyze
    :param model: Gemini model to use (default: gemini-2.0-flash)
    :return: Analysis result from Gemini
    """
    try:
        gemini_client = GeminiClient()

        prompt_config = product_config["prompt"][product]
        try:
            formatted_content = prompt_config["user"].format(
                error_summary=error_summary
            )
        except KeyError:
            formatted_content = prompt_config["user"].format(summary=error_summary)

        messages = [
            {"role": "system", "content": prompt_config["system"]},
            {"role": "user", "content": formatted_content},
            {"role": "assistant", "content": prompt_config["assistant"]},
        ]

        response = gemini_client.chat_completions_create(
            messages=messages,
            model=model,
            max_tokens=INFERENCE_MAX_TOKENS,
            temperature=INFERENCE_TEMPERATURE,
        )

        return response.choices[0].message.content

    except Exception as e:
        logger.error("Error analyzing %s log with Gemini: %s", product, e.__cause__)
        raise InferenceAPIUnavailableError(
            f"Error analyzing {product} log with Gemini: {str(e)}"
        ) from e
