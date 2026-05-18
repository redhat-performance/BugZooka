"""
Tests for the telemetry module.

Tests emit(), start(), shutdown(), team enrichment, timestamp enrichment,
and queue overflow behavior without requiring an actual ES connection.
"""

import os
import time
import json
import queue
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

import pytest

from bugzooka.telemetry import telemetry_client


@pytest.fixture(autouse=True)
def reset_telemetry_state():
    """Reset module-level telemetry state before and after each test."""
    telemetry_client._config = None
    telemetry_client._channel_team_mappings = {}
    telemetry_client._es_client = None
    telemetry_client._started = False
    telemetry_client._shutdown_event.clear()
    telemetry_client._thread = None
    # Drain any leftover events
    while not telemetry_client._queue.empty():
        try:
            telemetry_client._queue.get_nowait()
        except queue.Empty:
            break
    yield
    # Cleanup after test
    if telemetry_client._started:
        telemetry_client._shutdown_event.set()
        if telemetry_client._thread:
            telemetry_client._thread.join(timeout=2)
    telemetry_client._config = None
    telemetry_client._channel_team_mappings = {}
    telemetry_client._es_client = None
    telemetry_client._started = False
    telemetry_client._shutdown_event.clear()
    telemetry_client._thread = None


class TestEmit:
    """Test the emit() function."""

    def test_emit_noop_before_start(self):
        """emit() should silently return when telemetry hasn't started."""
        telemetry_client.emit({"command": "test"})
        assert telemetry_client._queue.empty()

    def test_emit_queues_event(self):
        """emit() should place event on the queue when config is set."""
        telemetry_client._config = {"es_server": "http://localhost:9200"}

        telemetry_client.emit({"command": "analyze_pr", "success": True})

        assert telemetry_client._queue.qsize() == 1
        event = telemetry_client._queue.get_nowait()
        assert event["command"] == "analyze_pr"
        assert event["success"] is True

    def test_emit_adds_timestamp(self):
        """emit() should add a UTC timestamp if not present."""
        telemetry_client._config = {"es_server": "http://localhost:9200"}

        telemetry_client.emit({"command": "help"})

        event = telemetry_client._queue.get_nowait()
        assert "timestamp" in event
        # Should be parseable as ISO format
        ts = datetime.fromisoformat(event["timestamp"])
        assert ts.tzinfo is not None

    def test_emit_preserves_existing_timestamp(self):
        """emit() should not overwrite an existing timestamp."""
        telemetry_client._config = {"es_server": "http://localhost:9200"}
        custom_ts = "2026-01-01T00:00:00+00:00"

        telemetry_client.emit({"command": "help", "timestamp": custom_ts})

        event = telemetry_client._queue.get_nowait()
        assert event["timestamp"] == custom_ts

    def test_emit_enriches_team_from_channel_mapping(self):
        """emit() should add team based on channel_id mapping."""
        telemetry_client._config = {"es_server": "http://localhost:9200"}
        telemetry_client._channel_team_mappings = {"C12345": "OCP-PerfScale"}

        telemetry_client.emit({"command": "analyze_pr", "channel_id": "C12345"})

        event = telemetry_client._queue.get_nowait()
        assert event["team"] == "OCP-PerfScale"

    def test_emit_sets_unknown_team_for_unmapped_channel(self):
        """emit() should set team to 'unknown' for unmapped channels."""
        telemetry_client._config = {"es_server": "http://localhost:9200"}
        telemetry_client._channel_team_mappings = {"C12345": "OCP-PerfScale"}

        telemetry_client.emit({"command": "help", "channel_id": "C99999"})

        event = telemetry_client._queue.get_nowait()
        assert event["team"] == "unknown"

    def test_emit_does_not_overwrite_existing_team(self):
        """emit() should not overwrite team if already set in event."""
        telemetry_client._config = {"es_server": "http://localhost:9200"}
        telemetry_client._channel_team_mappings = {"C12345": "OCP-PerfScale"}

        telemetry_client.emit({
            "command": "help",
            "channel_id": "C12345",
            "team": "CustomTeam",
        })

        event = telemetry_client._queue.get_nowait()
        assert event["team"] == "CustomTeam"

    def test_emit_drops_on_full_queue(self):
        """emit() should drop events when queue is full without raising."""
        telemetry_client._config = {"es_server": "http://localhost:9200"}

        # Fill the queue
        for i in range(1000):
            telemetry_client._queue.put_nowait({"command": f"fill_{i}"})

        # This should not raise
        telemetry_client.emit({"command": "dropped"})

        assert telemetry_client._queue.qsize() == 1000


class TestDrainAndFlush:
    """Test the _drain_and_flush() function."""

    def test_drain_empties_queue_into_bulk_write(self):
        """_drain_and_flush should drain events and call _bulk_write."""
        telemetry_client._config = {"index_prefix": "test", "flush_interval": 5, "batch_size": 50}
        telemetry_client._es_client = MagicMock()

        telemetry_client._queue.put_nowait({"command": "test1"})
        telemetry_client._queue.put_nowait({"command": "test2"})

        with patch.object(telemetry_client, "_bulk_write") as mock_write:
            telemetry_client._drain_and_flush(50)

            mock_write.assert_called_once()
            events = mock_write.call_args[0][0]
            assert len(events) == 2
            assert events[0]["command"] == "test1"
            assert events[1]["command"] == "test2"

    def test_drain_respects_batch_size(self):
        """_drain_and_flush should drain at most batch_size events."""
        telemetry_client._config = {"index_prefix": "test"}
        telemetry_client._es_client = MagicMock()

        for i in range(10):
            telemetry_client._queue.put_nowait({"command": f"event_{i}"})

        with patch.object(telemetry_client, "_bulk_write") as mock_write:
            telemetry_client._drain_and_flush(3)

            events = mock_write.call_args[0][0]
            assert len(events) == 3

        # 7 should remain in queue
        assert telemetry_client._queue.qsize() == 7

    def test_drain_does_nothing_on_empty_queue(self):
        """_drain_and_flush should not call _bulk_write when queue is empty."""
        with patch.object(telemetry_client, "_bulk_write") as mock_write:
            telemetry_client._drain_and_flush(50)
            mock_write.assert_not_called()


class TestBulkWrite:
    """Test the _bulk_write() function."""

    def test_bulk_write_drops_when_client_none(self):
        """_bulk_write should log warning and return when ES client is None."""
        telemetry_client._es_client = None
        telemetry_client._config = {"index_prefix": "test"}

        # Should not raise
        telemetry_client._bulk_write([{"command": "test"}])

    def test_bulk_write_generates_correct_index_name(self):
        """_bulk_write should use monthly index pattern."""
        mock_client = MagicMock()
        telemetry_client._es_client = mock_client
        telemetry_client._config = {"index_prefix": "bugzooka-telemetry"}

        mock_bulk_fn = MagicMock(return_value=(1, []))
        mock_helpers = MagicMock()
        mock_helpers.bulk = mock_bulk_fn

        import sys
        with patch.dict(sys.modules, {"opensearchpy.helpers": mock_helpers}):
            telemetry_client._bulk_write([{"command": "test"}])

            actions = mock_bulk_fn.call_args[0][1]
            expected_prefix = "bugzooka-telemetry-"
            assert actions[0]["_index"].startswith(expected_prefix)


def _mock_opensearch_modules():
    """Create mock opensearchpy modules for tests that need start()."""
    import sys

    mock_opensearch_cls = MagicMock()
    mock_opensearch_cls.return_value = MagicMock()

    mock_opensearchpy = MagicMock()
    mock_opensearchpy.OpenSearch = mock_opensearch_cls

    return patch.dict(sys.modules, {
        "opensearchpy": mock_opensearchpy,
        "opensearchpy.helpers": MagicMock(),
    })


class TestStartShutdown:
    """Test start() and shutdown() lifecycle."""

    def test_start_creates_thread(self):
        """start() should create and start the daemon thread."""
        env = {"TELEMETRY_ES_SERVER": "http://localhost:9200"}
        with patch.dict(os.environ, env, clear=True):
            with _mock_opensearch_modules():
                telemetry_client.start()

                assert telemetry_client._started is True
                assert telemetry_client._thread is not None
                assert telemetry_client._thread.is_alive()
                assert telemetry_client._thread.daemon is True

                telemetry_client.shutdown()

    def test_start_is_idempotent(self):
        """Calling start() twice should not create a second thread."""
        env = {"TELEMETRY_ES_SERVER": "http://localhost:9200"}
        with patch.dict(os.environ, env, clear=True):
            with _mock_opensearch_modules():
                telemetry_client.start()
                first_thread = telemetry_client._thread

                telemetry_client.start()
                assert telemetry_client._thread is first_thread

                telemetry_client.shutdown()

    def test_shutdown_sets_started_false(self):
        """shutdown() should mark telemetry as stopped."""
        env = {"TELEMETRY_ES_SERVER": "http://localhost:9200"}
        with patch.dict(os.environ, env, clear=True):
            with _mock_opensearch_modules():
                telemetry_client.start()
                telemetry_client.shutdown()

                assert telemetry_client._started is False

    def test_shutdown_noop_when_not_started(self):
        """shutdown() should do nothing when telemetry was never started."""
        telemetry_client.shutdown()
        assert telemetry_client._started is False

    def test_start_fails_without_es_server(self):
        """start() should raise ValueError when TELEMETRY_ES_SERVER is not set."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError, match="TELEMETRY_ES_SERVER is required"):
                telemetry_client.start()
