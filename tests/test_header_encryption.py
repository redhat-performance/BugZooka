"""
Tests for AES-GCM header encryption (MCP encrypted-context payloads).
"""

import os
import json
import base64
import pytest
from unittest.mock import patch

from bugzooka.core.header_encryption import encrypt_payload, encrypt_es_config
from tests.helpers import (
    aes256_gcm_decrypt_blob,
    random_aes256_gcm_key_b64,
)


class TestPayloadEncryption:
    """Round-trip tests using test-only decrypt helper."""

    @pytest.fixture
    def valid_encryption_key(self):
        key = random_aes256_gcm_key_b64()
        with patch.dict(os.environ, {"HEADER_SYMMETRIC_KEY": key}):
            yield key

    def test_encrypt_decrypt_roundtrip(self, valid_encryption_key):
        original = "https://es-server.example.com:9200"
        encrypted = encrypt_payload(original)
        decrypted = aes256_gcm_decrypt_blob(encrypted, valid_encryption_key)
        assert decrypted == original

    def test_encrypted_format(self, valid_encryption_key):
        data = "https://es-server.example.com:9200"
        encrypted = encrypt_payload(data)
        assert isinstance(encrypted, str)
        decoded = base64.b64decode(encrypted)
        assert len(decoded) > 28

    def test_encryption_with_json_config(self, valid_encryption_key):
        config = {
            "es_server": "https://es-prod.example.com:9200",
            "es_metadata_index": "perf_scale_ci*",
            "es_benchmark_index": "ripsaw-kube-burner-*",
        }
        config_json = json.dumps(config)
        encrypted = encrypt_payload(config_json)
        decrypted = aes256_gcm_decrypt_blob(encrypted, valid_encryption_key)
        assert json.loads(decrypted) == config

    def test_encrypt_without_key_raises_error(self):
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(
                ValueError,
                match="HEADER_SYMMETRIC_KEY environment variable not set",
            ):
                encrypt_payload("https://es-server.example.com:9200")

    def test_decrypt_with_wrong_key_raises_error(self, valid_encryption_key):
        data = "https://es-server.example.com:9200"
        encrypted = encrypt_payload(data)
        wrong_key = random_aes256_gcm_key_b64()
        with pytest.raises(Exception):
            aes256_gcm_decrypt_blob(encrypted, wrong_key)

    def test_decrypt_invalid_base64_raises_error(self, valid_encryption_key):
        with pytest.raises(Exception):
            aes256_gcm_decrypt_blob("not-valid-base64!!!", valid_encryption_key)

    def test_decrypt_too_short_data_raises_error(self, valid_encryption_key):
        short_data = base64.b64encode(b"short").decode("utf-8")
        with pytest.raises(Exception):
            aes256_gcm_decrypt_blob(short_data, valid_encryption_key)


class TestESConfigEncryption:
    """encrypt_es_config combines channel lookup + encryption."""

    @pytest.fixture
    def valid_encryption_key(self):
        key = random_aes256_gcm_key_b64()
        with patch.dict(os.environ, {"HEADER_SYMMETRIC_KEY": key}):
            yield key

    @pytest.fixture
    def es_channel_mappings(self):
        return {
            "C12345": {
                "es_server": "https://es-prod.example.com:9200",
                "es_metadata_index": "perf_scale_ci*",
                "es_benchmark_index": "ripsaw-kube-burner-*",
            },
            "C67890": {
                "es_server": "https://es-staging.example.com:9200",
                "es_metadata_index": "staging_ci*",
                "es_benchmark_index": "staging_burner-*",
            },
        }

    def test_encrypt_config_success(self, valid_encryption_key, es_channel_mappings):
        encrypted = encrypt_es_config("C12345", es_channel_mappings)
        assert isinstance(encrypted, str)
        decrypted_json = aes256_gcm_decrypt_blob(encrypted, valid_encryption_key)
        assert json.loads(decrypted_json) == es_channel_mappings["C12345"]

    def test_encrypt_config_channel_not_found(self, valid_encryption_key, es_channel_mappings):
        with pytest.raises(ValueError, match="No ES config configured for channel C99999"):
            encrypt_es_config("C99999", es_channel_mappings)

    def test_encrypt_config_invalid_config_type(self, valid_encryption_key):
        invalid_mappings = {"C12345": "https://es-server.example.com:9200"}
        with pytest.raises(ValueError, match="ES config for channel C12345 must be a dict"):
            encrypt_es_config("C12345", invalid_mappings)

    def test_encrypt_config_missing_es_server(self, valid_encryption_key):
        invalid_mappings = {"C12345": {"es_metadata_index": "perf_scale_ci*"}}
        with pytest.raises(
            ValueError,
            match="ES config for channel C12345 missing required field 'es_server'",
        ):
            encrypt_es_config("C12345", invalid_mappings)

    def test_encrypt_config_with_optional_fields(self, valid_encryption_key):
        mappings = {"C12345": {"es_server": "https://es-prod.example.com:9200"}}
        encrypted = encrypt_es_config("C12345", mappings)
        decrypted_json = aes256_gcm_decrypt_blob(encrypted, valid_encryption_key)
        assert json.loads(decrypted_json) == {"es_server": "https://es-prod.example.com:9200"}


class TestEncryptionSecurity:
    """Security properties."""

    @pytest.fixture
    def valid_encryption_key(self):
        key = random_aes256_gcm_key_b64()
        with patch.dict(os.environ, {"HEADER_SYMMETRIC_KEY": key}):
            yield key

    def test_same_plaintext_different_ciphertext(self, valid_encryption_key):
        data = "https://es-server.example.com:9200"
        encrypted1 = encrypt_payload(data)
        encrypted2 = encrypt_payload(data)
        assert encrypted1 != encrypted2
        assert aes256_gcm_decrypt_blob(encrypted1, valid_encryption_key) == data
        assert aes256_gcm_decrypt_blob(encrypted2, valid_encryption_key) == data

    def test_tampering_detection(self, valid_encryption_key):
        data = "https://es-server.example.com:9200"
        encrypted = encrypt_payload(data)
        encrypted_bytes = base64.b64decode(encrypted)
        tampered_bytes = encrypted_bytes[:20] + b"X" + encrypted_bytes[21:]
        tampered_encrypted = base64.b64encode(tampered_bytes).decode("utf-8")
        with pytest.raises(Exception):
            aes256_gcm_decrypt_blob(tampered_encrypted, valid_encryption_key)
