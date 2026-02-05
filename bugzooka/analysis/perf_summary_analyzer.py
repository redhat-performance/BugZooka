"""
Performance Summary Analyzer.
Provides performance metrics summary via MCP tools exposed by orion-mcp.
"""
import json
import logging
import os
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


def _calculate_weekly_change_calendar(
    this_week_values: List[float], last_week_values: List[float]
) -> Optional[float]:
    """
    Calculate weekly change using calendar-based data.
    Compares average of this week (last 7 days) vs last week (prior 7 days).

    :param this_week_values: Values from the last 7 calendar days
    :param last_week_values: Values from the prior 7 calendar days
    :return: Percent change, or None if insufficient data
    """
    if not this_week_values or not last_week_values:
        return None

    this_week_avg = sum(this_week_values) / len(this_week_values)
    last_week_avg = sum(last_week_values) / len(last_week_values)

    if last_week_avg == 0:
        return None

    return round(((this_week_avg - last_week_avg) / last_week_avg) * 100, 2)


def _weekly_hint(change: Optional[float], meta: dict) -> str:
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


def _select_metric_of_interest(meta: dict) -> str:
    moi = meta.get("metric_of_interest")
    agg = meta.get("agg_type")
    if isinstance(moi, str) and moi.lower() != "value":
        return moi
    if agg:
        return str(agg)
    if moi:
        return str(moi)
    return "value"


def _truncate_text(text: str, max_len: int) -> str:
    if max_len <= 0:
        return text
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    return f"{text[: max_len - 3]}..."


def _format_table(
    config: str,
    version: str,
    rows: List[dict],
    total_metrics: int,
    meta_map: dict,
) -> str:
    headers = ["Metric", "Metric_of_interest", "Min", "Max", "Avg", "Weekly Change (%)"]
    formatted_rows: List[List[str]] = []
    max_metric_len = int(os.getenv("PERF_SUMMARY_MAX_METRIC_LEN", "40"))
    max_moi_len = int(os.getenv("PERF_SUMMARY_MAX_MOI_LEN", "24"))
    for row in rows:
        weekly = row.get("weekly_change")
        metric_name = row.get("metric", "")
        meta = meta_map.get(metric_name, {})
        moi = _select_metric_of_interest(meta)
        # Dynamically select value based on metric of interest
        moi_key = moi.lower()
        # Check if moi is a simple stat type that exists in row
        if moi_key in ("min", "max", "avg") and row.get(moi_key) is not None:
            moi_value = row.get(moi_key)
        elif row.get("avg") is not None:
            # For complex moi (e.g., p99Latency), show the avg of those values
            moi_value = row.get("avg")
        else:
            moi_value = "n/a"
        moi_display = f"{moi} = {moi_value}"
        weekly_display = _weekly_hint(weekly, meta_map.get(metric_name, {}))
        metric_label = _truncate_text(str(metric_name), max_metric_len)
        moi_label = _truncate_text(str(moi_display), max_moi_len)
        formatted_rows.append(
            [
                metric_label,
                moi_label,
                str(row.get("min", "n/a")),
                str(row.get("max", "n/a")),
                str(row.get("avg", "n/a")),
                str(weekly_display),
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

    metrics_note = f"showing {len(rows)} of {total_metrics} metrics"
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


async def analyze_performance(
    config: Optional[Any] = None, version: Optional[Any] = None
) -> dict:
    """
    Analyze performance metrics for the specified config and version.

    :param config: Optional config file name (e.g., 'small-scale-udn-l3.yaml')
                   If None, analyzes all available configs.
    :param version: Optional OpenShift version (e.g., '4.19')
                    If None, uses default version.
    :return: Dict with 'success' boolean and 'message' string
    """
    logger.info(f"analyze_performance called with config={config}, version={version}")

    try:
        # Step 2: Get configs (use default control plane configs if not specified)
        config_list = _normalize_list(config)
        if config_list:
            configs = config_list
        else:
            # Use default control plane configs as fallback
            configs = _DEFAULT_CONTROL_PLANE_CONFIGS
            logger.info(
                f"No config specified, using default control plane configs: {configs}"
            )

        # Step 3: Get metrics for each config
        result_parts: List[str] = []
        versions = _normalize_list(version)
        if not versions:
            versions = ["4.19"]

        for cfg in configs:
            metrics, meta_map = await get_metrics(
                cfg, versions[0] if versions else None
            )
            if not metrics:
                result_parts.append(f"*{cfg}*: no metrics found")
                continue

            # Step 4: Fetch raw data for all metrics and all versions
            metrics_to_show = metrics
            for ver in versions:
                rows: List[dict[str, Any]] = []
                for metric in metrics_to_show:
                    # Fetch this week's data (last 7 days) for stats display
                    this_week_data = await get_performance_data(
                        config=cfg,
                        metric=metric,
                        version=ver,
                        lookback=7,
                    )
                    this_week_values = this_week_data.get("values", [])

                    if not this_week_values:
                        rows.append(
                            {
                                "metric": metric,
                                "min": "n/a",
                                "max": "n/a",
                                "avg": "n/a",
                                "weekly_change": None,
                            }
                        )
                        continue

                    # Fetch last 14 days data to calculate weekly change
                    two_weeks_data = await get_performance_data(
                        config=cfg,
                        metric=metric,
                        version=ver,
                        lookback=14,
                    )
                    two_weeks_values = two_weeks_data.get("values", [])

                    # Derive last week's values (14 days minus this week)
                    # Values are ordered, so last_week = values beyond this_week count
                    this_week_count = len(this_week_values)
                    if len(two_weeks_values) > this_week_count:
                        last_week_values = two_weeks_values[this_week_count:]
                    else:
                        last_week_values = []

                    stats = _calculate_stats(this_week_values)
                    weekly_change = _calculate_weekly_change_calendar(
                        this_week_values, last_week_values
                    )
                    rows.append(
                        {
                            "metric": metric,
                            "min": stats["min"],
                            "max": stats["max"],
                            "avg": stats["avg"],
                            "weekly_change": weekly_change,
                        }
                    )

                result_parts.append(
                    _format_table(cfg, ver, rows, len(metrics), meta_map)
                )

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
