"""
Telemetry client for BugZooka.

Non-blocking event emission to Elasticsearch via a background daemon thread.
Events are queued and bulk-written on a configurable interval or batch size.
"""

import logging
import queue
import threading
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_queue: queue.Queue = queue.Queue(maxsize=1000)
_thread: Optional[threading.Thread] = None
_es_client = None
_config: Optional[dict] = None
_channel_team_mappings: dict = {}
_shutdown_event = threading.Event()
_started = False


def start() -> None:
    """Initialize and start the telemetry daemon thread."""
    global _config, _channel_team_mappings, _es_client, _thread, _started

    if _started:
        return

    from bugzooka.core.config import get_telemetry_config, get_channel_team_mappings

    _config = get_telemetry_config()
    _channel_team_mappings = get_channel_team_mappings()
    logger.info("Loaded %d channel-team mapping(s)", len(_channel_team_mappings))

    from opensearchpy import OpenSearch

    _es_client = OpenSearch(
        _config["es_server"],
        timeout=30,
        retry_on_timeout=True,
        max_retries=2,
    )

    try:
        _es_client.info()
        logger.info("Telemetry ES connection verified: %s", _config["es_server"])
    except Exception as e:
        logger.warning(
            "Telemetry ES connection check failed: %s. Will retry on first flush.", e
        )

    _ensure_index_template()

    _thread = threading.Thread(
        target=_flush_thread,
        daemon=True,
        name="TelemetryFlusher",
    )
    _thread.start()
    _started = True
    logger.info(
        "Telemetry started (index_prefix=%s, flush=%ds, batch=%d)",
        _config["index_prefix"],
        _config["flush_interval"],
        _config["batch_size"],
    )


def emit(event: dict) -> None:
    """Enqueue a telemetry event. Non-blocking, never raises."""
    if _config is None:
        return

    try:
        channel_id = event.get("channel_id")
        if channel_id and "team" not in event:
            event["team"] = _channel_team_mappings.get(channel_id, "unknown")

        if "timestamp" not in event:
            event["timestamp"] = datetime.now(timezone.utc).isoformat()

        _queue.put_nowait(event)
        logger.debug(
            "Telemetry event queued: command=%s, queue_size=%d",
            event.get("command"),
            _queue.qsize(),
        )
    except queue.Full:
        logger.warning("Telemetry queue full, dropping event: %s", event.get("command"))
    except Exception as e:
        logger.warning("Telemetry emit error: %s", e)


def shutdown() -> None:
    """Signal the daemon thread to flush remaining events and stop."""
    global _started
    if not _started:
        return

    logger.info("Shutting down telemetry...")
    _shutdown_event.set()

    if _thread is not None:
        _thread.join(timeout=15)

    _started = False
    logger.info("Telemetry shutdown complete")


def _flush_thread() -> None:
    """Daemon thread: drain queue and bulk-write to ES periodically."""
    assert _config is not None
    flush_interval = _config["flush_interval"]
    batch_size = _config["batch_size"]

    while not _shutdown_event.is_set():
        _shutdown_event.wait(timeout=flush_interval)
        logger.debug("Telemetry flush cycle, queue_size=%d", _queue.qsize())
        _drain_and_flush(batch_size)

    remaining = _queue.qsize()
    if remaining:
        logger.info("Telemetry final flush: draining %d remaining event(s)", remaining)
    _drain_and_flush(max(remaining, batch_size))


def _drain_and_flush(batch_size: int) -> None:
    """Drain up to batch_size events from queue and write to ES."""
    batch: list[dict] = []
    while len(batch) < batch_size:
        try:
            event = _queue.get_nowait()
            batch.append(event)
        except queue.Empty:
            break

    if batch:
        _bulk_write(batch)


def _bulk_write(events: list) -> None:
    """Write a batch of events to Elasticsearch."""
    if _es_client is None:
        logger.warning(
            "Telemetry ES client not initialized, dropping %d events", len(events)
        )
        return

    assert _config is not None
    index_name = _config["index_prefix"]

    actions = [{"_index": index_name, "_source": event} for event in events]

    try:
        from opensearchpy.helpers import bulk

        success, errors = bulk(_es_client, actions, raise_on_error=False)
        if errors:
            logger.warning("Telemetry bulk write had %d errors", len(errors))
        else:
            logger.debug("Telemetry flushed %d events to %s", success, index_name)
    except Exception as e:
        logger.error(
            "Telemetry bulk write failed: %s. Dropping %d events.", e, len(events)
        )


def _ensure_index_template() -> None:
    """Create ES index template for telemetry indices (best-effort)."""
    if _es_client is None:
        return

    assert _config is not None
    template_name = f"{_config['index_prefix']}-template"
    template_body = {
        "index_patterns": [f"{_config['index_prefix']}"],
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 1,
        },
        "mappings": {
            "properties": {
                "timestamp": {"type": "date"},
                "command": {"type": "keyword"},
                "trigger_type": {"type": "keyword"},
                "channel_id": {"type": "keyword"},
                "user_id": {"type": "keyword"},
                "team": {"type": "keyword"},
                "success": {"type": "boolean"},
                "error_message": {"type": "text"},
                "error_type": {"type": "keyword"},
                "duration_ms": {"type": "long"},
                "retry_count": {"type": "integer"},
                "failure_type": {"type": "keyword"},
                "used_llm": {"type": "boolean"},
                "total_tokens": {"type": "integer"},
                "lookback_seconds": {"type": "integer"},
                "total_failures": {"type": "integer"},
                "pr_repo": {"type": "keyword"},
                "tool_calls_count": {"type": "integer"},
                "nightly_version": {"type": "keyword"},
                "configs_count": {"type": "integer"},
                "versions_count": {"type": "integer"},
            }
        },
    }

    try:
        _es_client.indices.put_template(
            name=template_name,
            body=template_body,
        )
        logger.info("Telemetry index template created: %s", template_name)
    except Exception as e:
        logger.warning("Failed to create telemetry index template: %s", e)
