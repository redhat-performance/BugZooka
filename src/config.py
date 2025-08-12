import json
import os
from dotenv import load_dotenv

from src.prompts import GENERIC_APP_PROMPT
from src.constants import (
    GENERIC_INFERENCE_URL,
    GENERIC_MODEL,
    INFERENCE_API_RETRY_ATTEMPTS,
    INFERENCE_API_RETRY_DELAY,
    INFERENCE_API_RETRY_BACKOFF_MULTIPLIER,
    INFERENCE_API_MAX_RETRY_DELAY,
)


load_dotenv()  # Load environment variables

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", None)
SLACK_CHANNEL_ID = os.getenv("SLACK_CHANNEL_ID", None)
JEDI_BOT_SLACK_USER_ID = os.getenv("JEDI_BOT_SLACK_USER_ID", None)

# Analysis mode configuration
# Options: "gemini", or any other value defaults to agent-based analysis
ANALYSIS_MODE = os.getenv("ANALYSIS_MODE", None)


def get_product_config(product_name: str):
    """
    Dynamically fetch inference config based on the product name.

    :param product_name: product name
    :return: inference details based on product
    """
    with open("prompts.json", encoding="utf-8") as f:
        PROMPT_DATA = json.load(f)
    INFERENCE_ENDPOINTS = {
        "GENERIC": os.getenv("GENERIC_INFERENCE_URL", GENERIC_INFERENCE_URL),
    }
    INFERENCE_MODEL_MAP = {
        "GENERIC": os.getenv("GENERIC_MODEL", GENERIC_MODEL),
    }
    INFERENCE_TOKENS = {
        "GENERIC": os.getenv("GENERIC_INFERENCE_TOKEN", ""),
    }
    INFERENCE_PROMPT_MAP = {
        "GENERIC": PROMPT_DATA.get("GENERIC_PROMPT", GENERIC_APP_PROMPT),
    }
    INFERENCE_TOKENS[product_name] = os.getenv(f"{product_name}_INFERENCE_TOKEN") or ""
    INFERENCE_MODEL_MAP[product_name] = os.getenv(f"{product_name}_MODEL") or ""
    INFERENCE_ENDPOINTS[product_name] = os.getenv(f"{product_name}_INFERENCE_URL") or ""
    INFERENCE_PROMPT_MAP[product_name] = PROMPT_DATA.get(
        f"{product_name}_PROMPT", GENERIC_APP_PROMPT
    )

    if (
        not INFERENCE_TOKENS[product_name]
        or not INFERENCE_MODEL_MAP[product_name]
        or not INFERENCE_ENDPOINTS[product_name]
        or not INFERENCE_PROMPT_MAP[product_name]
    ):
        raise ValueError(
            f"Missing env vars for product: {product_name}. Expected: {product_name}_INFERENCE_TOKEN, {product_name}_MODEL, {product_name}_INFERENCE_URL, {product_name}_PROMPT"
        )

    return {
        "endpoint": INFERENCE_ENDPOINTS,
        "token": INFERENCE_TOKENS,
        "model": INFERENCE_MODEL_MAP,
        "prompt": INFERENCE_PROMPT_MAP,
        "retry": {
            "max_attempts": int(
                os.getenv(
                    "INFERENCE_API_RETRY_MAX_ATTEMPTS", INFERENCE_API_RETRY_ATTEMPTS
                )
            ),
            "delay": float(
                os.getenv("INFERENCE_API_RETRY_DELAY", INFERENCE_API_RETRY_DELAY)
            ),
            "backoff": float(
                os.getenv(
                    "INFERENCE_API_RETRY_BACKOFF_MULTIPLIER",
                    INFERENCE_API_RETRY_BACKOFF_MULTIPLIER,
                )
            ),
            "max_delay": float(
                os.getenv(
                    "INFERENCE_API_RETRY_MAX_DELAY", INFERENCE_API_MAX_RETRY_DELAY
                )
            ),
        },
    }
