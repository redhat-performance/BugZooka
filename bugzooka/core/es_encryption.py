"""
ES Server Encryption/Decryption Module.

Provides AES-256-GCM encryption for ES_SERVER URLs transmitted via HTTP headers.
Uses shared encryption key between BugZooka and orion-mcp for secure communication.
"""
import base64
import logging
import os
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)


def generate_encryption_key() -> str:
    """
    Generate a new random 256-bit encryption key for AES-256-GCM.

    Use this once to generate ES_ENCRYPTION_KEY for .env file.
    The same key must be used in both BugZooka and orion-mcp.

    :return: Base64-encoded 256-bit key

    Example:
        >>> key = generate_encryption_key()
        >>> print(f"ES_ENCRYPTION_KEY={key}")
        ES_ENCRYPTION_KEY=rKz8Q7vN...base64string...==
    """
    key = AESGCM.generate_key(bit_length=256)
    return base64.b64encode(key).decode('utf-8')


def encrypt_es_server(es_data: str) -> str:
    """
    Encrypt ES configuration data using AES-256-GCM.

    Can encrypt either:
    - Plain ES_SERVER URL string
    - Full ES config JSON (with es_server, es_metadata_index, es_benchmark_index)

    Format of output: base64(nonce + ciphertext + authentication_tag)
    - nonce: 12 bytes (random, unique per encryption)
    - ciphertext: variable length (encrypted data)
    - authentication_tag: 16 bytes (GCM authentication tag)

    :param es_data: Plaintext data to encrypt (URL string or JSON config string)
    :return: Base64-encoded encrypted blob
    :raises ValueError: If ES_ENCRYPTION_KEY environment variable not set

    Example:
        >>> encrypted = encrypt_es_server("https://es-prod.example.com:9200")
        >>> print(encrypted)
        AQAAAACKzJ8R7vN...base64blob...==
    """
    # Get encryption key from environment
    encryption_key_b64 = os.environ.get("ES_ENCRYPTION_KEY")
    if not encryption_key_b64:
        raise ValueError(
            "ES_ENCRYPTION_KEY environment variable not set. "
            "Generate one with: python -c 'from bugzooka.core.es_encryption import generate_encryption_key; print(generate_encryption_key())'"
        )

    # Decode base64 key to bytes
    try:
        encryption_key = base64.b64decode(encryption_key_b64)
    except Exception as e:
        raise ValueError(f"Invalid ES_ENCRYPTION_KEY format (must be base64): {e}")

    # Validate key length (should be 32 bytes for AES-256)
    if len(encryption_key) != 32:
        raise ValueError(
            f"ES_ENCRYPTION_KEY must be 256 bits (32 bytes), got {len(encryption_key)} bytes"
        )

    # Create AES-GCM cipher
    aesgcm = AESGCM(encryption_key)

    # Generate random nonce (12 bytes for GCM)
    nonce = os.urandom(12)

    # Encrypt the ES data
    plaintext = es_data.encode('utf-8')
    ciphertext_with_tag = aesgcm.encrypt(nonce, plaintext, associated_data=None)

    # Concatenate nonce + ciphertext + tag
    encrypted_data = nonce + ciphertext_with_tag

    # Base64 encode for safe transmission
    encrypted_blob = base64.b64encode(encrypted_data).decode('utf-8')

    logger.debug(
        "Encrypted ES data (%d chars) to %d bytes",
        len(es_data),
        len(encrypted_data)
    )

    return encrypted_blob


def decrypt_es_server(encrypted_blob: str) -> str:
    """
    Decrypt ES configuration data from encrypted blob.

    Can decrypt either:
    - Plain ES_SERVER URL string
    - Full ES config JSON (with es_server, es_metadata_index, es_benchmark_index)

    :param encrypted_blob: Base64-encoded encrypted data (from encrypt_es_server)
    :return: Plaintext data (URL string or JSON config string)
    :raises ValueError: If decryption fails or ES_ENCRYPTION_KEY not set

    Example:
        >>> encrypted = "AQAAAACKzJ8R7vN...base64blob...=="
        >>> plaintext = decrypt_es_server(encrypted)
        >>> print(plaintext)
        https://es-prod.example.com:9200
    """
    # Get encryption key from environment
    encryption_key_b64 = os.environ.get("ES_ENCRYPTION_KEY")
    if not encryption_key_b64:
        raise ValueError("ES_ENCRYPTION_KEY environment variable not set")

    # Decode base64 key to bytes
    try:
        encryption_key = base64.b64decode(encryption_key_b64)
    except Exception as e:
        raise ValueError(f"Invalid ES_ENCRYPTION_KEY format: {e}")

    # Decode encrypted blob from base64
    try:
        encrypted_data = base64.b64decode(encrypted_blob)
    except Exception as e:
        raise ValueError(f"Invalid encrypted blob format (must be base64): {e}")

    # Extract nonce (first 12 bytes)
    if len(encrypted_data) < 12:
        raise ValueError("Encrypted data too short (minimum 12 bytes for nonce)")

    nonce = encrypted_data[:12]
    ciphertext_with_tag = encrypted_data[12:]

    # Decrypt using AES-GCM
    aesgcm = AESGCM(encryption_key)
    try:
        plaintext = aesgcm.decrypt(nonce, ciphertext_with_tag, associated_data=None)
    except Exception as e:
        raise ValueError(f"Decryption failed (wrong key or corrupted data): {e}")

    # Decode to string
    es_data = plaintext.decode('utf-8')

    logger.debug(
        "Decrypted ES data (%d bytes)",
        len(encrypted_data)
    )

    return es_data


def encrypt_es_config(channel_id: str, es_channel_mappings: dict) -> str:
    """
    Encrypt ES config (server, metadata index, benchmark index) for a specific channel.

    Convenience function that combines channel lookup + encryption.

    :param channel_id: Slack channel ID (e.g., "C12345")
    :param es_channel_mappings: Dict mapping channel_id -> es_config dict
                                 es_config must be dict with required key "es_server"
                                 and optional keys "es_metadata_index", "es_benchmark_index"
    :return: Base64-encoded encrypted blob
    :raises ValueError: If channel not found in mappings or config invalid

    Example:
        >>> mappings = {
        ...     "C12345": {
        ...         "es_server": "https://es-prod.example.com:9200",
        ...         "es_metadata_index": "perf_scale_ci*",
        ...         "es_benchmark_index": "ripsaw-kube-burner-*"
        ...     }
        ... }
        >>> encrypted = encrypt_es_config("C12345", mappings)
    """
    import json

    # Look up ES config for this channel
    channel_config = es_channel_mappings.get(channel_id)
    if not channel_config:
        raise ValueError(
            f"No ES config configured for channel {channel_id}. "
            f"Available channels: {list(es_channel_mappings.keys())}"
        )

    # Validate config is a dict
    if not isinstance(channel_config, dict):
        raise ValueError(
            f"ES config for channel {channel_id} must be a dict, got {type(channel_config)}"
        )

    # Validate required field
    if "es_server" not in channel_config:
        raise ValueError(
            f"ES config for channel {channel_id} missing required field 'es_server'"
        )

    # Encrypt the entire config as JSON
    config_json = json.dumps(channel_config)
    return encrypt_es_server(config_json)
