import json
import os
import logging.config
from dotenv import load_dotenv

from bugzooka.analysis.prompts import GENERIC_APP_PROMPT
from bugzooka.core.constants import (
    INFERENCE_API_TIMEOUT_SECONDS,
    INFERENCE_API_RETRY_ATTEMPTS,
    INFERENCE_API_RETRY_DELAY,
    INFERENCE_API_RETRY_BACKOFF_MULTIPLIER,
    INFERENCE_API_MAX_RETRY_DELAY,
    SUMMARY_LOOKBACK_SECONDS_DEFAULT,
)


load_dotenv()  # Load environment variables

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", None)
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN", None)  # For Socket Mode (xapp-*)
SLACK_CHANNEL_ID = os.getenv("SLACK_CHANNEL_ID", None)
JEDI_BOT_SLACK_USER_ID = os.getenv("JEDI_BOT_SLACK_USER_ID", None)
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", None)

# Weekly summary lookback window (seconds). Default: 7 days
SUMMARY_LOOKBACK_SECONDS = int(
    os.getenv("SUMMARY_LOOKBACK_SECONDS", SUMMARY_LOOKBACK_SECONDS_DEFAULT)
)


def get_inference_config():
    """
    Get unified inference configuration from environment variables.

    Required env vars: INFERENCE_URL, INFERENCE_TOKEN, INFERENCE_MODEL
    Optional env vars:
        - INFERENCE_VERIFY_SSL (default: true)
        - INFERENCE_API_TIMEOUT_SECONDS (default: 120)
        - INFERENCE_TOP_P (optional, not all APIs support this)
        - INFERENCE_FREQUENCY_PENALTY (optional, not all APIs support this)

    :return: dict with url, token, model, verify_ssl, timeout, and retry settings
    """
    url = os.getenv("INFERENCE_URL")
    if not url:
        raise ValueError("INFERENCE_URL environment variable is required")

    token = os.getenv("INFERENCE_TOKEN")
    if not token:
        raise ValueError("INFERENCE_TOKEN environment variable is required")

    model = os.getenv("INFERENCE_MODEL")
    if not model:
        raise ValueError("INFERENCE_MODEL environment variable is required")

    verify_ssl_env = os.getenv("INFERENCE_VERIFY_SSL", "true").lower()
    verify_ssl = verify_ssl_env == "true"
    timeout = float(
        os.getenv("INFERENCE_API_TIMEOUT_SECONDS", str(INFERENCE_API_TIMEOUT_SECONDS))
    )

    # Optional parameters (not all APIs support these, e.g. Gemini doesn't support frequency_penalty)
    top_p_env = os.getenv("INFERENCE_TOP_P")
    top_p = float(top_p_env) if top_p_env else None

    frequency_penalty_env = os.getenv("INFERENCE_FREQUENCY_PENALTY")
    frequency_penalty = float(frequency_penalty_env) if frequency_penalty_env else None

    return {
        "url": url,
        "token": token,
        "model": model,
        "verify_ssl": verify_ssl,
        "timeout": timeout,
        "top_p": top_p,
        "frequency_penalty": frequency_penalty,
        "retry": {
            "max_attempts": int(
                os.getenv(
                    "INFERENCE_API_RETRY_MAX_ATTEMPTS",
                    str(INFERENCE_API_RETRY_ATTEMPTS),
                )
            ),
            "delay": float(
                os.getenv("INFERENCE_API_RETRY_DELAY", str(INFERENCE_API_RETRY_DELAY))
            ),
            "backoff": float(
                os.getenv(
                    "INFERENCE_API_RETRY_BACKOFF_MULTIPLIER",
                    str(INFERENCE_API_RETRY_BACKOFF_MULTIPLIER),
                )
            ),
            "max_delay": float(
                os.getenv(
                    "INFERENCE_API_RETRY_MAX_DELAY", str(INFERENCE_API_MAX_RETRY_DELAY)
                )
            ),
        },
    }


def get_prompt_config():
    """
    Get product specific prompt configuration. If not provided, generic prompt will be used.

    :return: dict with system, user, assistant prompts
    """
    with open("prompt.json", encoding="utf-8") as f:
        prompt_data = json.load(f)

    return prompt_data.get("PROMPT", GENERIC_APP_PROMPT)


def configure_logging(log_level):
    """
    Configure application logging.

    param log_level: log level for logging
    return: None
    """
    log_msg_fmt = (
        "%(asctime)s [%(name)s:%(filename)s:%(lineno)d] %(levelname)s: %(message)s"
    )
    log_config_dict = {
        "version": 1,
        "disable_existing_loggers": False,
        "loggers": {
            "root": {
                "level": log_level,
                "handlers": ["console"],
            },
            "bugzooka": {
                "level": log_level,
                "handlers": ["console"],
                "propagate": False,
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "standard",
            },
        },
        "formatters": {
            "standard": {"format": log_msg_fmt},
        },
    }

    logging.config.dictConfig(log_config_dict)
