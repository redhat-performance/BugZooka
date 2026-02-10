"""
Performance Summary Analyzer.
Provides performance metrics summary via MCP tools exposed by orion-mcp.
"""
import json
import logging
import os
import re
from typing import Any, List, Optional

from bugzooka.integrations.mcp_client import (
    get_mcp_tool,
    initialize_global_resources_async,
)

logger = logging.getLogger(__name__)

# Default control plane configs used when user doesn't specify a config
_DEFAULT_CONTROL_PLANE_CONFIGS = [
    "okd-control-plane-cluster-density.yaml",
    "okd-control-plane-node-density.yaml",
    "okd-control-plane-node-density-cni.yaml",
    "okd-control-plane-crd-scale.yaml",
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

# Slack message size limit (actual is ~4000, use 3500 to be safe)
_SLACK_MESSAGE_LIMIT = int(os.getenv("PERF_SUMMARY_SLACK_MSG_LIMIT", "3500"))


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


def _calculate_period_change(
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


def _change_hint(change: Optional[float], meta: dict) -> str:
    if change is None:
        return "n/a"
    try:
        change_val = float(change)
    except (TypeError, ValueError):
        return "n/a"

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


def _format_config_table(
    config: str,
    version: str,
    rows: List[dict],
    total_metrics: int,
    lookback_days: int,
) -> str:
    headers = ["Metric", "Min", "Max", "Avg", "Change (%)"]
    formatted_rows: List[List[str]] = []
    max_metric_len = int(os.getenv("PERF_SUMMARY_MAX_METRIC_LEN", "40"))
    for row in rows:
        change_display = _change_hint(row.get("change"), row.get("meta", {}))
        metric_label = _truncate_text(str(row.get("metric", "")), max_metric_len)
        formatted_rows.append(
            [
                metric_label,
                str(row.get("min", "n/a")),
                str(row.get("max", "n/a")),
                str(row.get("avg", "n/a")),
                str(change_display),
            ]
        )

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

    metrics_note = (
        f"showing {len(rows)} of {total_metrics} metrics, "
        f"last {lookback_days}d vs prior {lookback_days}d"
    )
    return "\n".join(
        [
            f"*Config: {config}* (Version: {version}, {metrics_note})",
            "```",
            header_line,
            sep_line,
            *row_lines,
            "```",
        ]
    )


def _format_summary_table(
    version: str,
    rows: List[dict],
    total_metrics: int,
    lookback_days: int,
    limit: int,
) -> str:
    headers = ["Config", "Metric", "Min", "Max", "Avg", "Change (%)"]
    formatted_rows: List[List[str]] = []
    max_metric_len = int(os.getenv("PERF_SUMMARY_MAX_METRIC_LEN", "40"))
    max_config_len = int(os.getenv("PERF_SUMMARY_MAX_CONFIG_LEN", "25"))
    for row in rows:
        change_display = _change_hint(row.get("change"), row.get("meta", {}))
        config_label = _truncate_text(str(row.get("config", "")), max_config_len)
        metric_label = _truncate_text(str(row.get("metric", "")), max_metric_len)
        formatted_rows.append(
            [
                config_label,
                metric_label,
                str(row.get("min", "n/a")),
                str(row.get("max", "n/a")),
                str(row.get("avg", "n/a")),
                str(change_display),
            ]
        )

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

    metrics_note = (
        f"top {len(rows)} of {total_metrics} metrics, "
        f"last {lookback_days}d vs prior {lookback_days}d"
    )
    return "\n".join(
        [
            f"*Top {limit} Changes* (Version: {version}, {metrics_note})",
            "```",
            header_line,
            sep_line,
            *row_lines,
            "```",
        ]
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
) -> tuple[List[str], dict]:
    """
    Get list of available metrics for a specific config via MCP tool.

    :param config: Config file name (e.g., 'small-scale-udn-l3.yaml')
    :return: List of metric names
    """
    logger.info(f"Fetching metrics for config '{config}' from orion-mcp MCP tool")
    meta_map: dict[str, Any] = {}
    metrics_data: Any = []
    try:
        result = await _call_mcp_tool(
            "get_orion_metrics_with_meta",
            {"config_name": config, "version": version or "4.19"},
        )
    except Exception as e:
        logger.warning(
            "get_orion_metrics_with_meta unavailable, falling back to get_orion_metrics: %s",
            e,
        )
        result = await _call_mcp_tool("get_orion_metrics", {"config_name": config})

    if isinstance(result, dict) and "metrics" in result:
        meta_map = (
            result.get("meta", {}) if isinstance(result.get("meta"), dict) else {}
        )
        metrics_data = result.get("metrics", [])
    else:
        metrics_data = result

    if isinstance(metrics_data, list):
        return metrics_data, meta_map

    # The metrics tool may return {config_path: [metric1, metric2, ...]}
    if isinstance(metrics_data, dict):
        for _config_path, metrics_list in metrics_data.items():
            if isinstance(metrics_list, list):
                return metrics_list, meta_map

    return [], meta_map


async def get_performance_data(
    config: str, metric: str, version: str = "4.19", lookback: int = 14
) -> dict:
    """
    Get performance data for a specific config/metric/version via MCP tool.

    :param config: Config file name
    :param metric: Metric name
    :param version: OpenShift version
    :param lookback: Number of days to look back
    :return: Dict with 'values' list and metadata
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

    if isinstance(result, dict) and "values" in result:
        return result

    if isinstance(result, dict) and "data" in result:
        data = result.get("data", {})
        version_data = data.get(version)
        if isinstance(version_data, dict):
            metric_data = version_data.get(metric, {})
            values = metric_data.get("value", [])
            if isinstance(values, list):
                values = [v for v in values if v is not None]
                return {
                    "config": config,
                    "metric": metric,
                    "version": version,
                    "lookback": str(lookback),
                    "values": values,
                    "count": len(values),
                }

    return {
        "config": config,
        "metric": metric,
        "version": version,
        "lookback": str(lookback),
        "values": [],
        "count": 0,
        "error": "no data found",
    }


def _normalize_list(value: Optional[Any]) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    if isinstance(value, tuple):
        return [str(v) for v in value if str(v).strip()]
    text = str(value).strip()
    if not text:
        return []
    if "," in text:
        return [part.strip() for part in text.split(",") if part.strip()]
    return [text]


def parse_perf_summary_args(
    text: str,
) -> tuple[List[str], List[str], Optional[int], bool, bool]:
    """
    Parse configs, versions, lookback days, and verbose flag from a message.
    Expected format: "performance summary <Nd> [config.yaml,...] [version ...] [verbose]"
    """
    text_lower = text.lower()
    match = re.search(r"performance\s+summary\s*(.*)", text_lower)
    if not match:
        return [], [], None, False, False

    args_text = match.group(1).strip()
    if not args_text:
        return [], [], None, False, False

    parts = args_text.split()
    configs: List[str] = []
    versions: List[str] = []
    seen_configs: set[str] = set()
    seen_versions: set[str] = set()
    lookback_days: Optional[int] = None
    verbose = False
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
        if token_lower == "verbose":
            verbose = True
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

    return configs, versions, lookback_days, verbose, all_configs


async def analyze_performance(
    config: Optional[Any] = None,
    version: Optional[Any] = None,
    lookback_days: Optional[int] = None,
    verbose: bool = False,
    use_all_configs: bool = False,
) -> dict:
    """
    Analyze performance metrics for the specified config and version.

    :param config: Optional config file name (e.g., 'small-scale-udn-l3.yaml')
                   If None, analyzes all available configs.
    :param version: Optional OpenShift version (e.g., '4.19')
                    If None, uses default version.
    :param lookback_days: Lookback period in days for stats and change calculation
    :param verbose: If True, show per-config tables; otherwise show top changes summary
    :return: Dict with 'success' boolean and 'message' string
    """
    logger.info(
        "analyze_performance called with config=%s, version=%s, lookback_days=%s, verbose=%s, use_all_configs=%s",
        config,
        version,
        lookback_days,
        verbose,
        use_all_configs,
    )

    try:
        # Step 2: Get configs (default to control plane configs unless ALL requested)
        config_list = _normalize_list(config)
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
        elif config_list:
            configs = config_list
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
        lookback_period = lookback_days
        lookback_window = lookback_days * 2

        # Step 4: Get metrics for each config
        result_parts: List[str] = []
        versions = _normalize_list(version)
        if not versions:
            versions = ["4.19"]
        missing_configs: List[str] = []
        aggregated_rows: dict[str, List[dict[str, Any]]] = {ver: [] for ver in versions}
        total_metrics = 0

        for cfg in configs:
            metrics, meta_map = await get_metrics(
                cfg, versions[0] if versions else None
            )
            if not metrics:
                missing_configs.append(cfg)
                continue

            total_metrics += len(metrics)

            # Fetch raw data for all metrics and all versions
            for ver in versions:
                rows: List[dict[str, Any]] = []
                for metric in metrics:
                    this_period_data = await get_performance_data(
                        config=cfg,
                        metric=metric,
                        version=ver,
                        lookback=lookback_period,
                    )
                    this_period_values = this_period_data.get("values", [])

                    if not this_period_values:
                        row = {
                            "config": cfg,
                            "metric": metric,
                            "min": "n/a",
                            "max": "n/a",
                            "avg": "n/a",
                            "change": None,
                            "meta": meta_map.get(metric, {}),
                        }
                        rows.append(row)
                        aggregated_rows[ver].append(row)
                        continue

                    two_period_data = await get_performance_data(
                        config=cfg,
                        metric=metric,
                        version=ver,
                        lookback=lookback_window,
                    )
                    two_period_values = two_period_data.get("values", [])

                    this_period_count = len(this_period_values)
                    if len(two_period_values) > this_period_count:
                        previous_period_values = two_period_values[this_period_count:]
                    else:
                        previous_period_values = []

                    stats = _calculate_stats(this_period_values)
                    change = _calculate_period_change(
                        this_period_values, previous_period_values
                    )
                    row = {
                        "config": cfg,
                        "metric": metric,
                        "min": stats["min"],
                        "max": stats["max"],
                        "avg": stats["avg"],
                        "change": change,
                        "meta": meta_map.get(metric, {}),
                    }
                    rows.append(row)
                    aggregated_rows[ver].append(row)

                if verbose:
                    result_parts.append(
                        _format_config_table(
                            cfg, ver, rows, len(metrics), lookback_days
                        )
                    )

        if not verbose:
            limit = 15

            def _change_sort_key(row: dict[str, Any]) -> float:
                change_val = row.get("change")
                if isinstance(change_val, (int, float)):
                    return abs(change_val)
                return -1

            def _include_in_summary(row: dict[str, Any]) -> bool:
                keys = ("change", "min", "max", "avg")
                for key in keys:
                    val = row.get(key, "n/a")
                    if isinstance(val, (int, float)):
                        return True
                    if isinstance(val, str) and val.lower() != "n/a":
                        return True
                return False

            for ver in versions:
                rows = [
                    row
                    for row in aggregated_rows.get(ver, [])
                    if _include_in_summary(row)
                ]
                sorted_rows = sorted(rows, key=_change_sort_key, reverse=True)
                top_rows = sorted_rows[:limit]
                if top_rows:
                    result_parts.append(
                        _format_summary_table(
                            ver, top_rows, total_metrics, lookback_days, limit
                        )
                    )
                else:
                    result_parts.append(f"*Version {ver}*: no performance data found")

        if missing_configs:
            result_parts.append(f"*No metrics found for:* {', '.join(missing_configs)}")

        # Split result_parts into multiple messages to avoid Slack's size limit
        messages: List[str] = []
        current_message_parts: List[str] = []
        current_length = 0

        for part in result_parts:
            part_length = len(part) + 2  # +2 for "\n\n" separator
            if (
                current_length + part_length > _SLACK_MESSAGE_LIMIT
                and current_message_parts
            ):
                # Current message would exceed limit, start a new message
                messages.append("\n\n".join(current_message_parts))
                current_message_parts = [part]
                current_length = len(part)
            else:
                current_message_parts.append(part)
                current_length += part_length

        # Don't forget the last message
        if current_message_parts:
            messages.append("\n\n".join(current_message_parts))

        return {"success": True, "messages": messages}
    except Exception as e:
        logger.error(f"Error in analyze_performance: {e}", exc_info=True)
        return {"success": False, "message": f"Error: {str(e)}"}
