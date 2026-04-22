"""
Performance Summary Analyzer.
Provides performance metrics summary via MCP tools exposed by orion-mcp.
"""
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, List, Optional

from bugzooka.integrations.mcp_client import (
    get_mcp_tool,
    initialize_global_resources_async,
)

logger = logging.getLogger(__name__)

# Default control plane configs used when user doesn't specify a config
_DEFAULT_CONTROL_PLANE_CONFIGS = [
    "metal-perfscale-cpt-virt-udn-density.yaml",
    "trt-external-payload-cluster-density.yaml",
    "trt-external-payload-node-density.yaml",
    "trt-external-payload-node-density-cni.yaml",
    "trt-external-payload-crd-scale.yaml",
    "small-scale-udn-l3.yaml",
    "med-scale-udn-l3.yaml",
]

# Fallback list used when MCP config list is unavailable and ALL is requested
_ALL_CONFIGS_FALLBACK = [
    "chaos_tests.yaml",
    "label-small-scale-cluster-density.yaml",
    "med-scale-udn-l2.yaml",
    "med-scale-udn-l3.yaml",
    "metal-perfscale-cpt-data-path.yaml",
    "metal-perfscale-cpt-node-density-heavy.yaml",
    "metal-perfscale-cpt-node-density.yaml",
    "metal-perfscale-cpt-virt-density.yaml",
    "metal-perfscale-cpt-virt-udn-density.yaml",
    "netobserv-diff-meta-index.yaml",
    "node_scenarios.yaml",
    "okd-control-plane-cluster-density.yaml",
    "okd-control-plane-crd-scale.yaml",
    "okd-control-plane-node-density-cni.yaml",
    "okd-control-plane-node-density.yaml",
    "okd-data-plane-data-path.yaml",
    "olmv1.yaml",
    "ols-load-generator.yaml",
    "payload-scale.yaml",
    "pod_disruption_scenarios.yaml",
    "quay-load-test-stable-stage.yaml",
    "quay-load-test-stable.yaml",
    "readout-control-plane-cdv2.yaml",
    "readout-control-plane-node-density.yaml",
    "readout-ingress.yaml",
    "readout-netperf-tcp.yaml",
    "rhbok.yaml",
    "servicemesh-ingress.yaml",
    "servicemesh-netperf-tcp.yaml",
    "small-rosa-control-plane-node-density.yaml",
    "small-scale-cluster-density-report.yaml",
    "small-scale-cluster-density.yaml",
    "small-scale-node-density-cni.yaml",
    "small-scale-udn-l2.yaml",
    "small-scale-udn-l3.yaml",
    "trt-external-payload-cluster-density.yaml",
    "trt-external-payload-cpu.yaml",
    "trt-external-payload-crd-scale.yaml",
    "trt-external-payload-node-density-cni.yaml",
    "trt-external-payload-node-density.yaml",
]

# Default OpenShift versions used when none provided
_DEFAULT_VERSION = "4.19"
_DEFAULT_VERSIONS = [_DEFAULT_VERSION]

# Slack message size limit (actual is ~4000, use 3500 to be safe)
_SLACK_MESSAGE_LIMIT = int(os.getenv("PERF_SUMMARY_SLACK_MSG_LIMIT", "3500"))


@dataclass(frozen=True)
class PerformanceData:
    config: str
    metric: str
    version: str
    lookback: int
    values: List[float]
    error: Optional[str] = None

    @property
    def count(self) -> int:
        return len(self.values)


def _coerce_mcp_result(result: Any) -> Any:
    if isinstance(result, tuple) and len(result) == 2:
        result = result[0]
    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return result
    return result


async def _call_mcp_tool(tool_name: str, args: dict[str, Any]) -> Any:
    await initialize_global_resources_async()
    tool = get_mcp_tool(tool_name)
    if tool is None:
        raise RuntimeError(f"MCP tool '{tool_name}' not found")
    if hasattr(tool, "ainvoke"):
        result = await tool.ainvoke(args)
    else:
        result = tool.invoke(args)
    return _coerce_mcp_result(result)


def _calculate_stats(values: List[float]) -> dict:
    if not values:
        return {"min": None, "max": None, "avg": None}
    return {
        "min": round(min(values), 4),
        "max": round(max(values), 4),
        "avg": round(sum(values) / len(values), 4),
    }


def _format_metric_value(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, (int, float)):
        return f"{value:.2f}"
    return str(value)


def _format_runs(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, (int, float)):
        return str(int(value))
    return str(value)


def _calculate_percentage_change(
    current_values: List[float], previous_values: List[float]
) -> Optional[float]:
    """
    Calculate percent change between two periods.

    :param current_values: Values from the most recent period
    :param previous_values: Values from the prior period
    :return: Percent change, or None if insufficient data
    """
    if not current_values or not previous_values:
        return None

    current_avg = sum(current_values) / len(current_values)
    previous_avg = sum(previous_values) / len(previous_values)

    if previous_avg == 0:
        return None

    return round(((current_avg - previous_avg) / previous_avg) * 100, 2)


def _change_sort_key(row: dict[str, Any]) -> float:
    change_val = row.get("change")
    if isinstance(change_val, (int, float)):
        return abs(change_val)
    return -1


def _change_hint(change: Optional[float], meta: dict) -> str:
    if change is None:
        return "n/a"
    change_val = change

    direction = meta.get("direction")
    threshold = meta.get("threshold")
    if direction is None or threshold is None:
        return f"  {change_val:+.2f}"

    try:
        threshold_val = float(threshold)
    except (TypeError, ValueError):
        threshold_val = 0.0

    if abs(change_val) < threshold_val:
        return f"  {change_val:+.2f}"

    regression = (direction == 1 and change_val > 0) or (
        direction == -1 and change_val < 0
    )
    if regression:
        return f"{change_val:+.2f} 🆘"
    return f"{change_val:+.2f} 🟢"


def _truncate_text(text: str, max_len: int) -> str:
    if max_len <= 0:
        return text
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    return f"{text[: max_len - 3]}..."


def _render_table(headers: List[str], formatted_rows: List[List[str]]) -> str:
    col_widths = [len(h) for h in headers]
    for formatted_row in formatted_rows:
        for i, value in enumerate(formatted_row):
            col_widths[i] = max(col_widths[i], len(str(value)))

    header_line = " | ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers))
    sep_line = "-+-".join("-" * col_widths[i] for i in range(len(headers)))
    row_lines = [
        " | ".join(
            str(value).ljust(col_widths[i]) for i, value in enumerate(formatted_row)
        )
        for formatted_row in formatted_rows
    ]
    return "\n".join([header_line, sep_line, *row_lines])


def _format_metrics_table(
    *,
    title: str,
    version: str,
    rows: List[dict],
    total_metrics: int,
    lookback_days: int,
    include_config: bool,
    note_prefix: str,
) -> str:
    headers = ["Metric", "Runs", "Min", "Max", "Avg", "Change (%)"]
    if include_config:
        headers = ["Config", *headers]

    formatted_rows: List[List[str]] = []
    max_metric_len = int(os.getenv("PERF_SUMMARY_MAX_METRIC_LEN", "40"))
    max_config_len = int(os.getenv("PERF_SUMMARY_MAX_CONFIG_LEN", "25"))
    for row in rows:
        change_display = _change_hint(row.get("change"), row.get("meta", {}))
        metric_label = _truncate_text(str(row.get("metric", "")), max_metric_len)
        runs_display = _format_runs(row.get("runs"))
        row_values = [
            metric_label,
            runs_display,
            _format_metric_value(row.get("min", "n/a")),
            _format_metric_value(row.get("max", "n/a")),
            _format_metric_value(row.get("avg", "n/a")),
            str(change_display),
        ]
        if include_config:
            config_label = _truncate_text(str(row.get("config", "")), max_config_len)
            row_values = [config_label, *row_values]
        formatted_rows.append(row_values)

    run_values: list[int] = []
    for row in rows:
        runs = row.get("runs")
        if isinstance(runs, (int, float)):
            run_values.append(int(runs))
    if run_values:
        min_runs = min(run_values)
        max_runs = max(run_values)
        runs_note = (
            f"{min_runs} runs"
            if min_runs == max_runs
            else f"{min_runs}-{max_runs} runs"
        )
    else:
        runs_note = "0 runs"

    table_body = _render_table(headers, formatted_rows)
    metrics_note = (
        f"{note_prefix} {len(rows)} of {total_metrics} metrics, "
        f"{runs_note}, last {lookback_days}d vs prior {lookback_days}d"
    )
    return "\n".join(
        [
            f"{title} (Version: {version}, {metrics_note})",
            "```",
            table_body,
            "```",
        ]
    )


# Split a single oversized table into standalone code blocks so Slack keeps formatting intact.
def _split_metrics_table_for_slack(
    *,
    title: str,
    version: str,
    rows: List[dict],
    total_metrics: int,
    lookback_days: int,
    include_config: bool,
    note_prefix: str,
) -> List[str]:
    full_message = _format_metrics_table(
        title=title,
        version=version,
        rows=rows,
        total_metrics=total_metrics,
        lookback_days=lookback_days,
        include_config=include_config,
        note_prefix=note_prefix,
    )
    if len(full_message) <= _SLACK_MESSAGE_LIMIT:
        return [full_message]

    row_chunks: List[List[dict]] = []
    current_chunk: List[dict] = []
    for row in rows:
        candidate_chunk = [*current_chunk, row]
        candidate_message = _format_metrics_table(
            title=title,
            version=version,
            rows=candidate_chunk,
            total_metrics=total_metrics,
            lookback_days=lookback_days,
            include_config=include_config,
            note_prefix=note_prefix,
        )
        if current_chunk and len(candidate_message) > _SLACK_MESSAGE_LIMIT:
            row_chunks.append(current_chunk)
            current_chunk = [row]
        else:
            current_chunk = candidate_chunk

    if current_chunk:
        row_chunks.append(current_chunk)

    if len(row_chunks) <= 1:
        return [full_message]

    chunk_messages: List[str] = []
    total_parts = len(row_chunks)
    for index, row_chunk in enumerate(row_chunks, start=1):
        chunk_messages.append(
            _format_metrics_table(
                title=f"{title} (part {index}/{total_parts})",
                version=version,
                rows=row_chunk,
                total_metrics=total_metrics,
                lookback_days=lookback_days,
                include_config=include_config,
                note_prefix=note_prefix,
            )
        )

    return chunk_messages


def _normalize_metric_names(metrics_data: Any) -> List[str]:
    raw_metrics: List[str] = []

    if isinstance(metrics_data, str):
        raw_metrics.append(metrics_data)
    elif isinstance(metrics_data, list):
        for item in metrics_data:
            raw_metrics.extend(_normalize_metric_names(item))
    elif isinstance(metrics_data, dict):
        for value in metrics_data.values():
            raw_metrics.extend(_normalize_metric_names(value))

    normalized_metrics: List[str] = []
    seen_metrics: set[str] = set()
    for metric in raw_metrics:
        if metric in {"runs", "timestamp"}:
            continue
        if metric not in seen_metrics:
            normalized_metrics.append(metric)
            seen_metrics.add(metric)
    return normalized_metrics


# true means it's empty result just no data found(no error).
def _is_no_data_fetch_result(data: PerformanceData) -> bool:
    if data.values:
        return False
    if data.error is None:
        return True
    normalized_error = " ".join(str(data.error).split()).lower()
    return normalized_error in {"", "no data found"} or normalized_error.startswith(
        "no data found"
    )


async def get_configs() -> List[str]:
    """
    Get list of available Orion configuration files via MCP tool.

    :return: List of config file names (e.g., ['small-scale-udn-l3.yaml', ...])
    """
    logger.info("Fetching available configs from orion-mcp MCP tool")
    result = await _call_mcp_tool("get_orion_configs", {})
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        configs = result.get("configs")
        if isinstance(configs, list):
            return configs
    return []


async def get_metrics(
    config: str, version: Optional[str] = None
) -> tuple[List[str], dict[str, Any]]:
    """
    Get list of available metrics for a specific config via MCP tool.

    :param config: Config file name (e.g., 'small-scale-udn-l3.yaml')
    :return: Tuple of metrics and metadata map
    """
    await initialize_global_resources_async()
    logger.info(f"Fetching metrics for config '{config}' from orion-mcp MCP tool")
    meta_map: dict[str, Any] = {}
    effective_version = version or _DEFAULT_VERSION

    if get_mcp_tool("get_orion_metrics_with_meta") is not None:
        try:
            result = await _call_mcp_tool(
                "get_orion_metrics_with_meta",
                {"config_name": config, "version": effective_version},
            )
            if isinstance(result, dict):
                meta_map = (
                    result.get("meta", {})
                    if isinstance(result.get("meta"), dict)
                    else {}
                )
                metrics = _normalize_metric_names(result.get("metrics", []))
                if metrics:
                    return metrics, meta_map
            else:
                metrics = _normalize_metric_names(result)
                if metrics:
                    return metrics, meta_map
        except Exception as e:
            logger.warning(
                "get_orion_metrics_with_meta unavailable, falling back to get_orion_metrics: %s",
                e,
            )

    if get_mcp_tool("get_orion_metrics") is None:
        return [], meta_map

    try:
        result = await _call_mcp_tool(
            "get_orion_metrics",
            {"config_name": config, "version": effective_version},
        )
    except Exception:
        return [], meta_map

    metrics_data: Any = result
    if isinstance(result, dict):
        metrics_data = {
            key: value
            for key, value in result.items()
            if key not in {"error", "warning", "meta"}
        }

    metrics = _normalize_metric_names(metrics_data)
    if metrics:
        return metrics, meta_map

    return [], meta_map


async def get_performance_data(
    config: str, metric: str, version: str = _DEFAULT_VERSION, lookback: int = 14
) -> PerformanceData:
    """
    Get performance data for a specific config/metric/version via MCP tool.

    :param config: Config file name
    :param metric: Metric name
    :param version: OpenShift version
    :param lookback: Number of days to look back
    :return: PerformanceData with values and optional error
    """
    logger.info(
        "Fetching performance data via MCP: config=%s, metric=%s, version=%s, lookback=%s",
        config,
        metric,
        version,
        lookback,
    )

    try:
        result = await _call_mcp_tool(
            "get_orion_performance_data",
            {
                "config_name": config,
                "metric": metric,
                "version": version,
                "lookback": str(lookback),
            },
        )
    except Exception as e:
        logger.warning(
            "get_orion_performance_data unavailable, falling back to openshift_report_on: %s",
            e,
        )
        try:
            result = await _call_mcp_tool(
                "openshift_report_on",
                {
                    "versions": version,
                    "metric": metric,
                    "config_name": config,
                    "lookback": str(lookback),
                    "options": "json",
                },
            )
        except Exception:
            return PerformanceData(
                config=config,
                metric=metric,
                version=version,
                lookback=lookback,
                values=[],
                error="performance data unavailable for requested version/metric",
            )

    if isinstance(result, dict) and "values" in result:
        values = result.get("values", [])
        if isinstance(values, list):
            return PerformanceData(
                config=config,
                metric=metric,
                version=version,
                lookback=lookback,
                values=[v for v in values if v is not None],
                error=result.get("error"),
            )
        return PerformanceData(
            config=config,
            metric=metric,
            version=version,
            lookback=lookback,
            values=[],
            error="Unexpected data format for metric values",
        )

    if isinstance(result, dict) and "data" in result:
        data = result.get("data", {})
        version_data = data.get(version)
        if isinstance(version_data, dict):
            metric_data = version_data.get(metric, {})
            values = metric_data.get("value", [])
            if isinstance(values, list):
                values = [v for v in values if v is not None]
                return PerformanceData(
                    config=config,
                    metric=metric,
                    version=version,
                    lookback=lookback,
                    values=values,
                )

    if isinstance(result, dict):
        error_msg = result.get("error")
        if isinstance(error_msg, str) and error_msg:
            return PerformanceData(
                config=config,
                metric=metric,
                version=version,
                lookback=lookback,
                values=[],
                error=error_msg,
            )

    return PerformanceData(
        config=config,
        metric=metric,
        version=version,
        lookback=lookback,
        values=[],
        error="no data found",
    )


def parse_perf_summary_args(
    text: str,
) -> tuple[List[str], List[str], Optional[int], bool]:
    """
    Parse configs, versions, and lookback days from a message.
    Expected format: "performance summary <Nd> [config.yaml,...] [version ...]"
    """
    text_lower = text.lower()
    match = re.search(r"performance\s+summary\s*(.*)", text_lower)
    if not match:
        return [], [], None, False

    args_text = match.group(1).strip()
    if not args_text:
        return [], [], None, False

    parts = args_text.split()
    configs: List[str] = []
    versions: List[str] = []
    seen_configs: set[str] = set()
    seen_versions: set[str] = set()
    lookback_days: Optional[int] = None
    all_configs = False
    comma_present = "," in args_text

    def _add_config(value: str) -> None:
        if value.endswith(".yaml") and value not in seen_configs:
            configs.append(value)
            seen_configs.add(value)

    def _add_version(value: str) -> None:
        if re.match(r"^\d+\.\d+$", value) and value not in seen_versions:
            versions.append(value)
            seen_versions.add(value)

    for part in parts:
        token = part.strip()
        if not token:
            continue

        token_lower = token.lower().strip(",")
        # Ignore legacy mode keywords so they don't get parsed as versions/configs.
        if token_lower in {"concise", "summary", "simple", "verbose"}:
            continue
        if token_lower in {"all", "all-configs", "allconfigs"}:
            all_configs = True
            continue

        if re.match(r"^\d+d$", token_lower):
            lookback_days = int(token_lower[:-1])
            continue

        if token_lower.isdigit():
            lookback_days = int(token_lower)
            continue

        if ".yaml" in token_lower:
            for cfg in token.split(","):
                cfg = cfg.strip()
                if cfg:
                    _add_config(cfg)
            continue

        if "," in token:
            for ver in token.split(","):
                ver = ver.strip()
                if ver:
                    _add_version(ver)
            continue

        _add_version(token_lower)

    if not comma_present and len(configs) > 1:
        configs = configs[:1]

    return configs, versions, lookback_days, all_configs


async def analyze_performance(
    configs: Optional[List[str]] = None,
    versions: Optional[List[str]] = None,
    lookback_days: Optional[int] = None,
    use_all_configs: bool = False,
    channel_id: str = None,
) -> dict:
    """
    Analyze performance metrics for the specified config and version.

    :param configs: Optional list of config file names
                   (e.g., ['small-scale-udn-l3.yaml']).
                   If None or empty, uses defaults (or ALL if requested).
    :param versions: Optional list of OpenShift versions
                    (defaults to [_DEFAULT_VERSION] when empty).
                    If None or empty, uses default version.
    :param lookback_days: Lookback period in days for stats and change calculation
    :param channel_id: Slack channel ID for ES_SERVER routing (optional)
    :return: Dict with 'success' boolean and 'message' string
    """
    # Set channel context for ES encryption interceptor
    if channel_id:
        from bugzooka.integrations.mcp_interceptors import current_channel
        current_channel.set(channel_id)
        logger.debug("Set channel context for performance summary: %s", channel_id)

    logger.info(
        "analyze_performance called with configs=%s, versions=%s, lookback_days=%s, use_all_configs=%s",
        configs,
        versions,
        lookback_days,
        use_all_configs,
    )

    try:
        # Step 2: Get configs (default to control plane configs unless ALL requested)
        requested_configs = list(configs or [])
        if use_all_configs:
            configs = await get_configs()
            if configs:
                logger.info("Using ALL configs from MCP: %s", configs)
            else:
                configs = _ALL_CONFIGS_FALLBACK
                logger.info(
                    "No configs returned from MCP, using fallback ALL list: %s",
                    configs,
                )
        elif requested_configs:
            configs = requested_configs
        else:
            configs = _DEFAULT_CONTROL_PLANE_CONFIGS
            logger.info(
                "No config specified, using default control plane configs: %s",
                configs,
            )

        # Step 3: Normalize lookback days
        if lookback_days is None:
            lookback_days = int(os.getenv("PERF_SUMMARY_LOOKBACK_DAYS", "14"))
        if lookback_days <= 0:
            lookback_days = 14
        lookback_window = lookback_days * 2

        # Step 4: Get metrics for each config
        result_parts: List[str] = []
        requested_versions = list(versions or [])
        if not requested_versions:
            versions = list(_DEFAULT_VERSIONS)
        else:
            versions = requested_versions
        for cfg in configs:
            for ver in versions:
                metrics, meta_map = await get_metrics(cfg, ver)
                if not metrics:
                    result_parts.append(
                        f"Could not obtain metric definitions for {cfg} "
                        f"(version {ver})"
                    )
                    continue

                rows: List[dict[str, Any]] = []
                for metric in metrics:
                    this_period_data = await get_performance_data(
                        config=cfg,
                        metric=metric,
                        version=ver,
                        lookback=lookback_days,
                    )
                    this_period_values = this_period_data.values
                    run_count = len(this_period_values)

                    if not this_period_values:
                        if not _is_no_data_fetch_result(this_period_data):
                            result_parts.append(
                                f"Could not fetch current-period performance data for "
                                f"{cfg} (version {ver}, metric {metric})"
                            )
                            continue
                        row = {
                            "config": cfg,
                            "metric": metric,
                            "runs": 0,
                            "min": "n/a",
                            "max": "n/a",
                            "avg": "n/a",
                            "change": None,
                            "meta": meta_map.get(metric, {}),
                        }
                        rows.append(row)
                        continue

                    two_period_data = await get_performance_data(
                        config=cfg,
                        metric=metric,
                        version=ver,
                        lookback=lookback_window,
                    )
                    two_period_values = two_period_data.values

                    this_period_count = len(this_period_values)
                    previous_period_values = []
                    if len(two_period_values) > this_period_count:
                        # Orion values are ordered oldest -> newest, so the
                        # trailing slice overlaps the current lookback window.
                        previous_period_values = two_period_values[:-this_period_count]

                    stats = _calculate_stats(this_period_values)
                    change = _calculate_percentage_change(
                        this_period_values, previous_period_values
                    )
                    row = {
                        "config": cfg,
                        "metric": metric,
                        "runs": run_count,
                        "min": stats["min"],
                        "max": stats["max"],
                        "avg": stats["avg"],
                        "change": change,
                        "meta": meta_map.get(metric, {}),
                    }
                    rows.append(row)

                if rows:
                    result_parts.extend(
                        _split_metrics_table_for_slack(
                            title=f"*Config: {cfg}*",
                            version=ver,
                            rows=sorted(rows, key=_change_sort_key, reverse=True),
                            total_metrics=len(metrics),
                            lookback_days=lookback_days,
                            include_config=False,
                            note_prefix="showing",
                        )
                    )
        return {
            "success": True,
            "messages": result_parts,
        }
    except Exception as e:
        logger.error(f"Error in analyze_performance: {e}", exc_info=True)
        return {"success": False, "message": f"Error: {str(e)}"}
