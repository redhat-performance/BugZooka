"""
AES-256-GCM encryption for payloads sent to MCP servers via HTTP headers.

Uses HEADER_SYMMETRIC_KEY environment variable for AES-256-GCM encryption.
Payload format: base64(nonce || ciphertext || tag).

This module provides:
- encrypt_payload(): Generic function to encrypt any string payload
- encrypt_es_config(): Convenience wrapper for ES configuration (current use case)

Future secrets can use encrypt_payload() directly or add new convenience wrappers.
The receiving MCP server decrypts with get_decrypted_context() and extracts needed fields.
"""
import base64
import json
import logging
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from bugzooka.core.constants import (
    AES_GCM_KEY_LENGTH_BYTES,
    AES_GCM_NONCE_LENGTH_BYTES,
)

logger = logging.getLogger(__name__)

_ENV_KEY_NAME = "HEADER_SYMMETRIC_KEY"


def encrypt_payload(plaintext: str) -> str:
    """
    Encrypt an UTF-8 string using AES-256-GCM.

    Output format: base64(nonce || ciphertext || authentication_tag). Nonce length
    matches ``AES_GCM_NONCE_LENGTH_BYTES``; tag is produced by GCM. Same format
    orion-mcp decrypts with ``HEADER_SYMMETRIC_KEY``.

    :param plaintext: String to encrypt (e.g. JSON for ``X-Encrypted-Context``).
    :return: Base64-encoded blob (nonce || ciphertext || tag)
    :raises ValueError: If ``HEADER_SYMMETRIC_KEY`` is missing, not valid base64,
        or not 32 bytes after decoding.
    """
    encryption_key_b64 = os.environ.get(_ENV_KEY_NAME)
    if not encryption_key_b64:
        raise ValueError(
            f"{_ENV_KEY_NAME} environment variable not set. "
            "Use the same base64-encoded 32-byte symmetric key as orion-mcp."
        )

    try:
        encryption_key = base64.b64decode(encryption_key_b64)
    except Exception as e:
        raise ValueError(
            f"Invalid {_ENV_KEY_NAME} format (must be base64): {e}"
        ) from e

    if len(encryption_key) != AES_GCM_KEY_LENGTH_BYTES:
        raise ValueError(
            f"{_ENV_KEY_NAME} must decode to {AES_GCM_KEY_LENGTH_BYTES} bytes "
            f"(AES-256), got {len(encryption_key)} bytes"
        )

    aesgcm = AESGCM(encryption_key)
    nonce = os.urandom(AES_GCM_NONCE_LENGTH_BYTES)
    plaintext_bytes = plaintext.encode("utf-8")
    ciphertext_with_tag = aesgcm.encrypt(nonce, plaintext_bytes, associated_data=None)
    encrypted_data = nonce + ciphertext_with_tag
    encrypted_blob = base64.b64encode(encrypted_data).decode("utf-8")

    logger.debug(
        "Encrypted payload (%d chars plaintext) to %d bytes",
        len(plaintext),
        len(encrypted_data),
    )
    return encrypted_blob


def encrypt_es_config(channel_id: str, es_config_map: dict) -> str:
    """
    Convenience wrapper: encrypt ES configuration for a Slack channel.

    This is a specific use case of encrypt_payload() for Elasticsearch config.
    Future secrets can either:
    - Add similar convenience wrappers (e.g., encrypt_api_keys())
    - Call encrypt_payload(json.dumps(my_dict)) directly

    Serializes the channel's ES config dict to JSON, then encrypts via ``encrypt_payload``.

    :param channel_id: Slack channel ID (e.g. ``C12345``).
    :param es_config_map: Map of channel_id -> ES config dict (same source as
        ``get_es_channel_mappings()`` / ``ES_CHANNEL_MAPPINGS``).
    :return: Base64 ciphertext suitable for the ``X-Encrypted-Context`` header.
    :raises ValueError: If the channel is missing, config is not a dict, or
        ``es_server`` is absent.

    Example::

        es_config_map = {
            "C12345": {
                "es_server": "https://es-prod.example.com:9200",
                "es_metadata_index": "perf_scale_ci*",
                "es_benchmark_index": "ripsaw-kube-burner-*",
            }
        }
        blob = encrypt_es_config("C12345", es_config_map)
    """
    channel_config = es_config_map.get(channel_id)
    if not channel_config:
        raise ValueError(
            f"No ES config configured for channel {channel_id}. "
            f"Available channels: {list(es_config_map.keys())}"
        )

    if not isinstance(channel_config, dict):
        raise ValueError(
            f"ES config for channel {channel_id} must be a dict, "
            f"got {type(channel_config)}"
        )

    if "es_server" not in channel_config:
        raise ValueError(
            f"ES config for channel {channel_id} missing required field 'es_server'"
        )

    config_json = json.dumps(channel_config)
    return encrypt_payload(config_json)
