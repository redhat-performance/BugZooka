"""
Tests for ES encryption module.

Tests encryption/decryption functionality for securely transmitting
ES configuration between BugZooka and orion-mcp.
"""

import os
import json
import base64
import pytest
from unittest.mock import patch

from bugzooka.core.es_encryption import (
    generate_encryption_key,
    encrypt_es_server,
    decrypt_es_server,
    encrypt_es_config,
)


class TestEncryptionKeyGeneration:
    """Test encryption key generation."""

    def test_generate_key_format(self):
        """Test that generated key is valid base64."""
        key = generate_encryption_key()

        # Should be base64 string
        assert isinstance(key, str)

        # Should be decodable
        decoded = base64.b64decode(key)

        # Should be 32 bytes (256 bits)
        assert len(decoded) == 32

    def test_generate_key_uniqueness(self):
        """Test that each generated key is unique."""
        key1 = generate_encryption_key()
        key2 = generate_encryption_key()

        assert key1 != key2


class TestESServerEncryption:
    """Test ES server URL encryption/decryption."""

    @pytest.fixture
    def valid_encryption_key(self):
        """Provide a valid 256-bit encryption key."""
        key = generate_encryption_key()
        with patch.dict(os.environ, {"ES_ENCRYPTION_KEY": key}):
            yield key

    def test_encrypt_decrypt_roundtrip(self, valid_encryption_key):
        """Test that encryption and decryption work correctly."""
        original = "https://es-server.example.com:9200"

        encrypted = encrypt_es_server(original)
        decrypted = decrypt_es_server(encrypted)

        assert decrypted == original

    def test_encrypted_format(self, valid_encryption_key):
        """Test that encrypted output is base64."""
        data = "https://es-server.example.com:9200"
        encrypted = encrypt_es_server(data)

        # Should be base64 string
        assert isinstance(encrypted, str)

        # Should be decodable
        decoded = base64.b64decode(encrypted)

        # Should be at least 12 bytes (nonce) + data + 16 bytes (tag)
        assert len(decoded) > 28

    def test_encryption_with_json_config(self, valid_encryption_key):
        """Test encryption of full ES config JSON."""
        config = {
            "es_server": "https://es-prod.example.com:9200",
            "es_metadata_index": "perf_scale_ci*",
            "es_benchmark_index": "ripsaw-kube-burner-*"
        }
        config_json = json.dumps(config)

        encrypted = encrypt_es_server(config_json)
        decrypted = decrypt_es_server(encrypted)

        assert json.loads(decrypted) == config

    def test_encrypt_without_key_raises_error(self):
        """Test that encryption fails without ES_ENCRYPTION_KEY."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError, match="ES_ENCRYPTION_KEY environment variable not set"):
                encrypt_es_server("https://es-server.example.com:9200")

    def test_decrypt_with_wrong_key_raises_error(self, valid_encryption_key):
        """Test that decryption fails with wrong key."""
        data = "https://es-server.example.com:9200"
        encrypted = encrypt_es_server(data)

        # Use different key for decryption
        wrong_key = generate_encryption_key()
        with patch.dict(os.environ, {"ES_ENCRYPTION_KEY": wrong_key}):
            with pytest.raises(ValueError, match="Decryption failed"):
                decrypt_es_server(encrypted)

    def test_decrypt_invalid_base64_raises_error(self, valid_encryption_key):
        """Test that decryption fails with invalid base64."""
        with pytest.raises(ValueError, match="Invalid encrypted blob format"):
            decrypt_es_server("not-valid-base64!!!")

    def test_decrypt_too_short_data_raises_error(self, valid_encryption_key):
        """Test that decryption fails with too short data."""
        # Create valid base64 but too short (< 12 bytes)
        short_data = base64.b64encode(b"short").decode('utf-8')

        with pytest.raises(ValueError, match="Encrypted data too short"):
            decrypt_es_server(short_data)


class TestESConfigEncryption:
    """Test encrypt_es_config function that combines lookup + encryption."""

    @pytest.fixture
    def valid_encryption_key(self):
        """Provide a valid 256-bit encryption key."""
        key = generate_encryption_key()
        with patch.dict(os.environ, {"ES_ENCRYPTION_KEY": key}):
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
                "es_metadata_index": "staging_ci*",
                "es_benchmark_index": "staging_burner-*"
            }
        }

    def test_encrypt_config_success(self, valid_encryption_key, es_channel_mappings):
        """Test successful encryption of channel ES config."""
        channel_id = "C12345"

        encrypted = encrypt_es_config(channel_id, es_channel_mappings)

        # Should return base64 string
        assert isinstance(encrypted, str)

        # Decrypt and verify
        decrypted_json = decrypt_es_server(encrypted)
        decrypted_config = json.loads(decrypted_json)

        assert decrypted_config == es_channel_mappings[channel_id]

    def test_encrypt_config_channel_not_found(self, valid_encryption_key, es_channel_mappings):
        """Test error when channel not in mappings."""
        with pytest.raises(ValueError, match="No ES config configured for channel C99999"):
            encrypt_es_config("C99999", es_channel_mappings)

    def test_encrypt_config_invalid_config_type(self, valid_encryption_key):
        """Test error when config is not a dict."""
        invalid_mappings = {
            "C12345": "https://es-server.example.com:9200"  # String instead of dict
        }

        with pytest.raises(ValueError, match="ES config for channel C12345 must be a dict"):
            encrypt_es_config("C12345", invalid_mappings)

    def test_encrypt_config_missing_es_server(self, valid_encryption_key):
        """Test error when config missing required es_server field."""
        invalid_mappings = {
            "C12345": {
                "es_metadata_index": "perf_scale_ci*",
                # Missing es_server
            }
        }

        with pytest.raises(ValueError, match="ES config for channel C12345 missing required field 'es_server'"):
            encrypt_es_config("C12345", invalid_mappings)

    def test_encrypt_config_with_optional_fields(self, valid_encryption_key):
        """Test encryption with optional index fields missing."""
        mappings = {
            "C12345": {
                "es_server": "https://es-prod.example.com:9200",
                # Optional fields omitted
            }
        }

        encrypted = encrypt_es_config("C12345", mappings)
        decrypted_json = decrypt_es_server(encrypted)
        decrypted_config = json.loads(decrypted_json)

        # Should only have es_server
        assert decrypted_config == {"es_server": "https://es-prod.example.com:9200"}


class TestEncryptionSecurity:
    """Test security properties of encryption."""

    @pytest.fixture
    def valid_encryption_key(self):
        """Provide a valid 256-bit encryption key."""
        key = generate_encryption_key()
        with patch.dict(os.environ, {"ES_ENCRYPTION_KEY": key}):
            yield key

    def test_same_plaintext_different_ciphertext(self, valid_encryption_key):
        """Test that encrypting same data twice produces different ciphertext (due to random nonce)."""
        data = "https://es-server.example.com:9200"

        encrypted1 = encrypt_es_server(data)
        encrypted2 = encrypt_es_server(data)

        # Different ciphertext (different nonces)
        assert encrypted1 != encrypted2

        # But both decrypt to same plaintext
        assert decrypt_es_server(encrypted1) == data
        assert decrypt_es_server(encrypted2) == data

    def test_tampering_detection(self, valid_encryption_key):
        """Test that tampered ciphertext fails decryption (GCM authentication)."""
        data = "https://es-server.example.com:9200"
        encrypted = encrypt_es_server(data)

        # Tamper with the encrypted data
        encrypted_bytes = base64.b64decode(encrypted)
        tampered_bytes = encrypted_bytes[:20] + b'X' + encrypted_bytes[21:]  # Flip one byte
        tampered_encrypted = base64.b64encode(tampered_bytes).decode('utf-8')

        # Should fail due to authentication tag mismatch
        with pytest.raises(ValueError, match="Decryption failed"):
            decrypt_es_server(tampered_encrypted)
