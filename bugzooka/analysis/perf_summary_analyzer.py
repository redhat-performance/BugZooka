"""
Performance Summary Analyzer.
Provides performance metrics summary via simple REST API calls to orion-mcp server.

Uses direct HTTP calls to /api/* endpoints instead of MCP protocol,
which avoids the parameter-dropping bugs in langchain-mcp-adapters.
"""
import hashlib
import logging
import os
import random
from typing import Any, List, Optional

import httpx

logger = logging.getLogger(__name__)

# Orion MCP REST API base URL
ORION_API_BASE_URL = os.getenv("ORION_API_BASE_URL", "http://localhost:3030")

# Test mode: bypass OpenSearch by returning mock data
_TEST_MODE = os.getenv("PERF_SUMMARY_TEST_MODE", "false").lower() in (
    "1",
    "true",
    "yes",
)
_TEST_VERSIONS = {
    v.strip()
    for v in os.getenv("PERF_SUMMARY_TEST_VERSIONS", "4.19,4.20").split(",")
    if v.strip()
}

_MOCK_CONFIGS = [
    "small-scale-udn-l3.yaml",
    "med-scale-udn-l3.yaml",
]
_DEFAULT_MOCK_METRICS = [
    "podReadyLatency_P99",
    "podReadyLatency_P50",
    "ovnCPU_avg",
    "ovnCPU_P99",
    "kubeAPIServerThroughput",
]
_MOCK_METRICS_BY_CONFIG = {
    "small-scale-udn-l3.yaml": [
        "podReadyLatency_P99",
        "podReadyLatency_P50",
        "ovnCPU_avg",
        "kubeAPIServerThroughput",
    ],
    "med-scale-udn-l3.yaml": [
        "podReadyLatency_P99",
        "podReadyLatency_P50",
        "ovnCPU_avg",
        "ovnCPU_P99",
        "kubeAPIServerThroughput",
    ],
}


def _mock_metrics_for_config(config: str) -> List[str]:
    return _MOCK_METRICS_BY_CONFIG.get(config, _DEFAULT_MOCK_METRICS)


def _mock_meta_for_config(config: str) -> dict:
    metrics = _mock_metrics_for_config(config)

    def _infer_moi(metric_name: str) -> str:
        if "_" in metric_name:
            suffix = metric_name.split("_")[-1]
            suffix_upper = suffix.upper()
            if suffix_upper.startswith("P") and suffix_upper[1:].isdigit():
                return suffix_upper
            if suffix in ("avg", "max", "min", "value"):
                return suffix
        return "value"

    return {
        metric: {
            "direction": 1,
            "threshold": 10.0,
            "metric_of_interest": _infer_moi(metric),
        }
        for metric in metrics
    }


def _stable_seed(*parts: str) -> int:
    joined = "::".join(parts).encode("utf-8")
    digest = hashlib.sha256(joined).hexdigest()
    return int(digest[:8], 16)


def _mock_series(config: str, metric: str, version: str, lookback: int) -> List[float]:
    seed = _stable_seed(config, metric, version)
    rng = random.Random(seed)
    base = rng.uniform(10.0, 100.0)
    trend = rng.uniform(-0.5, 0.5)
    values = []
    for i in range(lookback):
        noise = rng.uniform(-1.5, 1.5)
        values.append(round(base + (trend * i) + noise, 4))
    return values


def _calculate_stats(values: List[float]) -> dict:
    if not values:
        return {"min": None, "max": None, "avg": None}
    return {
        "min": round(min(values), 4),
        "max": round(max(values), 4),
        "avg": round(sum(values) / len(values), 4),
    }


def _calculate_weekly_change(values: List[float]) -> Optional[float]:
    """
    Compare last 7 values vs prior 7 values.
    Returns percent change, or None if insufficient data.
    """
    if len(values) < 14:
        return None
    week1 = values[:7]
    week2 = values[7:14]
    avg1 = sum(week1) / len(week1)
    avg2 = sum(week2) / len(week2)
    if avg1 == 0:
        return None
    return round(((avg2 - avg1) / avg1) * 100, 2)


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


def _format_table(
    config: str,
    version: str,
    rows: List[dict],
    total_metrics: int,
    meta_map: dict,
) -> str:
    headers = ["Metric", "Metric_of_interest", "Min", "Max", "Avg", "Weekly Change (%)"]
    formatted_rows: List[List[str]] = []
    for row in rows:
        weekly = row.get("weekly_change")
        metric_name = row.get("metric", "")
        meta = meta_map.get(metric_name, {})
        moi = meta.get("metric_of_interest") or meta.get("agg_type") or "value"
        moi_value = row.get("avg") if row.get("avg") is not None else "n/a"
        moi_display = f"{moi} = {moi_value}"
        weekly_display = _weekly_hint(weekly, meta_map.get(metric_name, {}))
        formatted_rows.append(
            [
                str(row.get("metric", "n/a")),
                str(moi_display),
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
    Get list of available Orion configuration files via REST API.

    :return: List of config file names (e.g., ['small-scale-udn-l3.yaml', ...])
    """
    if _TEST_MODE:
        logger.info("Test mode enabled: returning mock configs")
        return _MOCK_CONFIGS

    logger.info("Fetching available configs from orion-mcp REST API")

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(f"{ORION_API_BASE_URL}/api/configs")
        response.raise_for_status()
        data = response.json()
        return data.get("configs", [])


async def get_metrics(
    config: str, version: Optional[str] = None
) -> tuple[List[str], dict]:
    """
    Get list of available metrics for a specific config via REST API.

    :param config: Config file name (e.g., 'small-scale-udn-l3.yaml')
    :return: List of metric names
    """
    if _TEST_MODE:
        logger.info("Test mode enabled: returning mock metrics")
        return _mock_metrics_for_config(config), _mock_meta_for_config(config)

    logger.info(f"Fetching metrics for config '{config}' from orion-mcp REST API")

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(
            f"{ORION_API_BASE_URL}/api/metrics",
            params={
                "config": config,
                "include_meta": "1",
                "version": version or "4.19",
            },
        )
        response.raise_for_status()
        data = response.json()
        meta_map: dict[str, Any] = (
            data.get("meta", {}) if isinstance(data, dict) else {}
        )
        metrics_data: Any = data.get("metrics", []) if isinstance(data, dict) else data
        if isinstance(metrics_data, list):
            return metrics_data, meta_map

        # The metrics endpoint returns {metrics: {config_path: [metric1, metric2, ...]}}
        if isinstance(metrics_data, dict):
            for config_path, metrics_list in metrics_data.items():
                if isinstance(metrics_list, list):
                    return metrics_list, meta_map

        return [], meta_map


async def get_performance_data(
    config: str, metric: str, version: str = "4.19", lookback: int = 14
) -> dict:
    """
    Get performance data for a specific config/metric/version via REST API.

    :param config: Config file name
    :param metric: Metric name
    :param version: OpenShift version
    :param lookback: Number of days to look back
    :return: Dict with 'values' list and metadata
    """
    if _TEST_MODE:
        if version not in _TEST_VERSIONS:
            logger.info(
                "Test mode enabled: no mock data for version '%s' (allowed: %s)",
                version,
                sorted(_TEST_VERSIONS),
            )
            return {
                "config": config,
                "metric": metric,
                "version": version,
                "lookback": str(lookback),
                "values": [],
                "count": 0,
                "source": "mock",
                "error": "no data for version",
            }
        logger.info("Test mode enabled: returning mock performance data")
        values = _mock_series(config, metric, version, lookback)
        return {
            "config": config,
            "metric": metric,
            "version": version,
            "lookback": str(lookback),
            "values": values,
            "count": len(values),
            "source": "mock",
        }

    logger.info(
        f"Fetching performance data: config={config}, metric={metric}, version={version}, lookback={lookback}"
    )

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.get(
            f"{ORION_API_BASE_URL}/api/performance",
            params={
                "config": config,
                "metric": metric,
                "version": version,
                "lookback": str(lookback),
            },
        )
        if response.status_code == 404:
            return {
                "config": config,
                "metric": metric,
                "version": version,
                "lookback": str(lookback),
                "values": [],
                "count": 0,
                "error": "no data found",
            }
        response.raise_for_status()
        return response.json()


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
        # Step 2: Get configs
        config_list = _normalize_list(config)
        if config_list:
            configs = config_list
        else:
            configs = await get_configs()

        # Step 3: Get metrics for each config
        result_parts: List[str] = []
        versions = _normalize_list(version)
        if not versions:
            versions = ["4.19"]
        lookback_days = 14

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
                    perf_data = await get_performance_data(
                        config=cfg,
                        metric=metric,
                        version=ver,
                        lookback=lookback_days,
                    )
                    values = perf_data.get("values", [])
                    if not values:
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

                    stats = _calculate_stats(values)
                    weekly_change = _calculate_weekly_change(values)
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

        if _TEST_MODE:
            result_parts.insert(0, "_Using mock data (PERF_SUMMARY_TEST_MODE=true)_")

        return {"success": True, "message": "\n\n".join(result_parts)}
    except Exception as e:
        logger.error(f"Error in analyze_performance: {e}", exc_info=True)
        return {"success": False, "message": f"Error: {str(e)}"}
