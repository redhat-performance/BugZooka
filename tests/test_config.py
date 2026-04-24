"""
Tests for config module.

Tests ES channel mappings configuration parsing from environment variables.
"""

import os
import json
import pytest
from unittest.mock import patch

from bugzooka.core.config import get_es_channel_mappings


class TestGetESChannelMappings:
    """Test get_es_channel_mappings function."""

    def test_parse_valid_mappings(self):
        """Test parsing valid ES channel mappings JSON."""
        mappings_json = json.dumps({
            "C12345": {
                "es_server": "https://es-prod.example.com:9200",
                "es_metadata_index": "perf_scale_ci*",
                "es_benchmark_index": "ripsaw-kube-burner-*"
            },
            "C67890": {
                "es_server": "https://es-staging.example.com:9200"
            }
        })

        with patch.dict(os.environ, {"ES_CHANNEL_MAPPINGS": mappings_json}):
            result = get_es_channel_mappings()

            assert isinstance(result, dict)
            assert len(result) == 2

            # Check first mapping
            assert "C12345" in result
            assert result["C12345"]["es_server"] == "https://es-prod.example.com:9200"
            assert result["C12345"]["es_metadata_index"] == "perf_scale_ci*"
            assert result["C12345"]["es_benchmark_index"] == "ripsaw-kube-burner-*"

            # Check second mapping
            assert "C67890" in result
            assert result["C67890"]["es_server"] == "https://es-staging.example.com:9200"

    def test_parse_single_channel(self):
        """Test parsing mappings with single channel."""
        mappings_json = json.dumps({
            "C_SINGLE": {
                "es_server": "https://es-server.example.com:9200",
                "es_metadata_index": "metadata*",
                "es_benchmark_index": "benchmark*"
            }
        })

        with patch.dict(os.environ, {"ES_CHANNEL_MAPPINGS": mappings_json}):
            result = get_es_channel_mappings()

            assert len(result) == 1
            assert "C_SINGLE" in result

    def test_missing_env_var_raises_error(self):
        """Test that missing ES_CHANNEL_MAPPINGS raises ValueError."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError, match="ES_CHANNEL_MAPPINGS environment variable not set"):
                get_es_channel_mappings()

    def test_invalid_json_raises_error(self):
        """Test that invalid JSON raises ValueError."""
        invalid_json = '{"C12345": invalid json}'

        with patch.dict(os.environ, {"ES_CHANNEL_MAPPINGS": invalid_json}):
            with pytest.raises(ValueError, match="Invalid ES_CHANNEL_MAPPINGS JSON format"):
                get_es_channel_mappings()

    def test_non_dict_json_raises_error(self):
        """Test that non-dict JSON raises ValueError."""
        # Array instead of object
        array_json = json.dumps(["C12345", "C67890"])

        with patch.dict(os.environ, {"ES_CHANNEL_MAPPINGS": array_json}):
            with pytest.raises(ValueError, match="ES_CHANNEL_MAPPINGS must be a JSON object/dict"):
                get_es_channel_mappings()

        # String instead of object
        string_json = json.dumps("not a dict")

        with patch.dict(os.environ, {"ES_CHANNEL_MAPPINGS": string_json}):
            with pytest.raises(ValueError, match="ES_CHANNEL_MAPPINGS must be a JSON object/dict"):
                get_es_channel_mappings()

    def test_empty_mappings_raises_error(self):
        """Test that empty mappings dict raises ValueError."""
        empty_json = json.dumps({})

        with patch.dict(os.environ, {"ES_CHANNEL_MAPPINGS": empty_json}):
            with pytest.raises(ValueError, match="ES_CHANNEL_MAPPINGS cannot be empty"):
                get_es_channel_mappings()

    def test_mappings_with_optional_fields_only(self):
        """Test mappings where some channels have minimal config."""
        mappings_json = json.dumps({
            "C_MINIMAL": {
                "es_server": "https://es-server.example.com:9200"
                # No optional index fields
            },
            "C_FULL": {
                "es_server": "https://es-full.example.com:9200",
                "es_metadata_index": "metadata*",
                "es_benchmark_index": "benchmark*"
            }
        })

        with patch.dict(os.environ, {"ES_CHANNEL_MAPPINGS": mappings_json}):
            result = get_es_channel_mappings()

            # Minimal config should only have es_server
            assert "C_MINIMAL" in result
            assert result["C_MINIMAL"]["es_server"] == "https://es-server.example.com:9200"
            assert "es_metadata_index" not in result["C_MINIMAL"]
            assert "es_benchmark_index" not in result["C_MINIMAL"]

            # Full config should have all fields
            assert "C_FULL" in result
            assert result["C_FULL"]["es_server"] == "https://es-full.example.com:9200"
            assert result["C_FULL"]["es_metadata_index"] == "metadata*"
            assert result["C_FULL"]["es_benchmark_index"] == "benchmark*"

    def test_mappings_with_special_characters_in_url(self):
        """Test mappings with special characters in ES server URL."""
        # URL with credentials
        mappings_json = json.dumps({
            "C12345": {
                "es_server": "https://user:pass@es-server.example.com:9200",
                "es_metadata_index": "perf_scale_ci*",
                "es_benchmark_index": "ripsaw-kube-burner-*"
            }
        })

        with patch.dict(os.environ, {"ES_CHANNEL_MAPPINGS": mappings_json}):
            result = get_es_channel_mappings()

            assert result["C12345"]["es_server"] == "https://user:pass@es-server.example.com:9200"

    def test_mappings_preserve_all_fields(self):
        """Test that all fields in config are preserved."""
        mappings_json = json.dumps({
            "C12345": {
                "es_server": "https://es-server.example.com:9200",
                "es_metadata_index": "perf_scale_ci*",
                "es_benchmark_index": "ripsaw-kube-burner-*",
                "custom_field": "custom_value"  # Extra field
            }
        })

        with patch.dict(os.environ, {"ES_CHANNEL_MAPPINGS": mappings_json}):
            result = get_es_channel_mappings()

            # Should preserve custom field
            assert result["C12345"]["custom_field"] == "custom_value"

    def test_multiple_channels_different_indices(self):
        """Test that different channels can have different index patterns."""
        mappings_json = json.dumps({
            "C_TEAM_A": {
                "es_server": "https://es-shared.example.com:9200",
                "es_metadata_index": "team_a_metadata*",
                "es_benchmark_index": "team_a_benchmark*"
            },
            "C_TEAM_B": {
                "es_server": "https://es-shared.example.com:9200",
                "es_metadata_index": "team_b_metadata*",
                "es_benchmark_index": "team_b_benchmark*"
            }
        })

        with patch.dict(os.environ, {"ES_CHANNEL_MAPPINGS": mappings_json}):
            result = get_es_channel_mappings()

            # Both teams share same ES server but different indices
            assert result["C_TEAM_A"]["es_server"] == result["C_TEAM_B"]["es_server"]
            assert result["C_TEAM_A"]["es_metadata_index"] != result["C_TEAM_B"]["es_metadata_index"]
            assert result["C_TEAM_A"]["es_benchmark_index"] != result["C_TEAM_B"]["es_benchmark_index"]