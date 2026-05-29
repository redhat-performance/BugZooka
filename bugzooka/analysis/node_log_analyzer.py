"""
Node journal log analyzer for Kubernetes node-density / pod startup RCA.

Parses journal.trimmed / journal.folded.log / gzip-decompressed journal files
from prow gather-extra artifacts and extracts deterministic metrics:
  - Pod lifecycle timeline (ADD → sandbox → CNI → container start → PLEG)
  - PLEG detection lag per container (crio StartContainer vs PLEG event)
  - Housekeeping overruns (count, peak, time-bucketed)
  - PLEG relist silence gaps > 2s
  - Node-wide SLO distribution (pod_startup_latency_tracker)
  - Peak concurrent pod activity

Standalone CLI:
    python -m bugzooka.analysis.node_log_analyzer \\
        --log /path/to/journal --pod node-density-956

BugZooka integration:
    from bugzooka.analysis.node_log_analyzer import analyze_node_journal, format_result_markdown
    result = analyze_node_journal("/tmp/journal.decompressed", pod_name="node-density-956")
    print(format_result_markdown(result))
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path
from typing import NamedTuple, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

_TS_RE = re.compile(
    r"^\w{3}\s+\d+\s+(\d{2}:\d{2}:\d{2}\.\d+)\s+\S+\s+(\S+):\s*(.*)"
)
_CRIO_TS_RE = re.compile(r'time="[^"]*T(\d{2}:\d{2}:\d{2}\.\d+)')
_PLEG_DATA_RE = re.compile(r'"Data":"([a-f0-9]+)"')
_CRIO_STARTED_ID_RE = re.compile(r"containerID=([a-f0-9]+)")
_HK_ACTUAL_RE = re.compile(r'actual="([\d.]+)')
_SLO_RE = re.compile(r"podStartSLOduration=([\d.]+)")
_CRIO_CREATED_ID_RE = re.compile(r"Created container ([a-f0-9]+):")
_UID_RE = re.compile(
    r'kubernetes\.io/projected/([a-f0-9-]+)-kube-api-access'
)


def _ts_to_secs(ts: str) -> float:
    h, m, rest = ts.split(":", 2)
    return int(h) * 3600 + int(m) * 60 + float(rest)


def _fmt(secs: float) -> str:
    return f"{secs:.3f}s"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class TimelineEvent(NamedTuple):
    timestamp: str
    source: str
    event: str


class PlegLag(NamedTuple):
    container_id: str
    role: str               # "pause" or "app"
    crio_started_ts: Optional[str]
    pleg_event_ts: str
    lag_secs: float


class HkOverrun(NamedTuple):
    timestamp: str
    actual_secs: float


class PlegGap(NamedTuple):
    start_ts: str
    end_ts: str
    gap_secs: float


class SloStats(NamedTuple):
    count: int
    min_secs: float
    p50_secs: float
    p90_secs: float
    max_secs: float


class NodeLogRCAResult(NamedTuple):
    node: str
    log_path: str
    pod: Optional[str]
    pod_uid: Optional[str]
    timeline: list
    pleg_lags: list
    hk_overruns: list
    pleg_gaps: list
    slo_stats: Optional[SloStats]
    peak_concurrency: int
    peak_concurrency_ts: Optional[str]
    summary: str


# ---------------------------------------------------------------------------
# Core parser
# ---------------------------------------------------------------------------


def analyze_node_journal(
    log_path: str,
    pod_name: Optional[str] = None,
) -> NodeLogRCAResult:
    """
    Parse a node journal file and return structured RCA metrics.

    :param log_path: path to journal file (plain text or gzip-decompressed)
    :param pod_name: pod name to anchor the lifecycle timeline
    :return: NodeLogRCAResult
    """
    path = Path(log_path)
    node = path.parent.name

    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError as exc:
        logger.error("Cannot read %s: %s", log_path, exc)
        return NodeLogRCAResult(
            node=node, log_path=log_path, pod=pod_name, pod_uid=None,
            timeline=[], pleg_lags=[], hk_overruns=[], pleg_gaps=[],
            slo_stats=None, peak_concurrency=0, peak_concurrency_ts=None,
            summary=f"Could not read log file: {exc}",
        )

    pod_uid: Optional[str] = None
    timeline_raw: list[TimelineEvent] = []

    # container_id -> crio StartContainer timestamp
    crio_start_ts: dict[str, str] = {}
    # anchor pod PLEG events: [(ts, container_id)]
    pleg_pod_events: list[tuple[str, str]] = []
    # all PLEG ContainerStarted timestamps (any pod) for gap detection
    pleg_all_ts: list[str] = []

    hk_overruns: list[HkOverrun] = []
    slo_values: list[float] = []
    concurrency_by_sec: dict[str, int] = {}

    for raw in lines:
        line = raw.rstrip()
        # skip folded-log section markers
        if line in ("{{{", "}}}"):
            continue

        m = _TS_RE.match(line)
        if not m:
            continue
        hms, proc, body = m.group(1), m.group(2), m.group(3)
        sec_key = hms[:8]

        # --- uid extraction (once per pod) ---
        if pod_uid is None and pod_name and pod_name in body:
            uid_m = _UID_RE.search(body)
            if uid_m:
                pod_uid = uid_m.group(1)

        # --- concurrency counter ---
        if any(k in body for k in ("SyncLoop ADD", "SyncLoop UPDATE", "ContainerStarted")):
            concurrency_by_sec[sec_key] = concurrency_by_sec.get(sec_key, 0) + 1

        is_anchor = pod_name and pod_name in body

        # --- kubelet events ---
        if "SyncLoop ADD" in body and is_anchor:
            timeline_raw.append(TimelineEvent(hms, proc, "SyncLoop ADD"))
        elif "SyncLoop UPDATE" in body and is_anchor:
            pass  # too noisy for timeline
        elif ("MountVolume started" in body or "MountVolume.SetUp succeeded" in body) and is_anchor:
            verb = "MountVolume.SetUp succeeded" if "succeeded" in body else "MountVolume started"
            timeline_raw.append(TimelineEvent(hms, proc, verb))
        elif ("No sandbox for pod" in body or "No ready sandbox" in body) and is_anchor:
            timeline_raw.append(TimelineEvent(hms, proc, "No sandbox — need new one"))

        # --- crio events ---
        if "crio" in proc:
            ts = _crio_ts(body, hms)
            if is_anchor:
                if "Running pod sandbox" in body:
                    timeline_raw.append(TimelineEvent(ts, "crio", "RunPodSandbox started"))
                elif "Adding pod" in body and "CNI" in body:
                    timeline_raw.append(TimelineEvent(ts, "crio", "Adding pod to CNI (multus-shim)"))
                elif "Ran pod sandbox" in body:
                    timeline_raw.append(TimelineEvent(ts, "crio", "Ran pod sandbox (sandbox ready)"))
                elif "Creating container" in body:
                    timeline_raw.append(TimelineEvent(ts, "crio", "CreateContainer started"))
                elif "Created container" in body:
                    cid_m = _CRIO_CREATED_ID_RE.search(body)
                    if cid_m:
                        cid = cid_m.group(1)
                        timeline_raw.append(TimelineEvent(ts, "crio", f"Container {cid[:12]} created"))

            if "Started container" in body:
                cid_m = _CRIO_STARTED_ID_RE.search(body)
                if cid_m:
                    cid = cid_m.group(1)
                    if cid not in crio_start_ts:
                        crio_start_ts[cid] = ts
                    if is_anchor:
                        timeline_raw.append(TimelineEvent(ts, "crio", f"StartContainer succeeded ({cid[:12]})"))

        # --- PLEG ContainerStarted ---
        if "SyncLoop (PLEG)" in body and "ContainerStarted" in body:
            pleg_all_ts.append(hms)
            if is_anchor:
                data_m = _PLEG_DATA_RE.search(body)
                if data_m:
                    cid = data_m.group(1)
                    pleg_pod_events.append((hms, cid))
                    timeline_raw.append(TimelineEvent(hms, proc, f"PLEG ContainerStarted ({cid[:12]})"))

        # --- pod_startup_latency_tracker ---
        if "pod_startup_latency_tracker" in body:
            slo_m = _SLO_RE.search(body)
            if slo_m:
                slo_values.append(float(slo_m.group(1)))
            if is_anchor and slo_m:
                timeline_raw.append(TimelineEvent(
                    hms, proc, f"pod_startup_latency_tracker SLO = {float(slo_m.group(1)):.3f}s"
                ))

        # --- housekeeping overruns ---
        if "Housekeeping took longer" in body:
            hk_m = _HK_ACTUAL_RE.search(body)
            if hk_m:
                hk_overruns.append(HkOverrun(hms, float(hk_m.group(1))))

    # --- PLEG lags for anchor pod ---
    pleg_lags: list[PlegLag] = []
    if pleg_pod_events:
        sandbox_id = pleg_pod_events[0][1]
        for pleg_ts, cid in pleg_pod_events:
            role = "pause" if cid == sandbox_id else "app"
            start = crio_start_ts.get(cid)
            lag = (_ts_to_secs(pleg_ts) - _ts_to_secs(start)) if start else 0.0
            pleg_lags.append(PlegLag(cid, role, start, pleg_ts, lag))

    # --- PLEG silence gaps ---
    pleg_gaps: list[PlegGap] = sorted(
        [
            PlegGap(pleg_all_ts[i - 1], pleg_all_ts[i],
                    _ts_to_secs(pleg_all_ts[i]) - _ts_to_secs(pleg_all_ts[i - 1]))
            for i in range(1, len(pleg_all_ts))
            if (_ts_to_secs(pleg_all_ts[i]) - _ts_to_secs(pleg_all_ts[i - 1])) > 2.0
        ],
        key=lambda g: g.gap_secs,
        reverse=True,
    )

    # --- SLO stats ---
    slo_stats: Optional[SloStats] = None
    if slo_values:
        slo_values.sort()
        n = len(slo_values)
        slo_stats = SloStats(
            count=n,
            min_secs=slo_values[0],
            p50_secs=slo_values[n // 2],
            p90_secs=slo_values[int(n * 0.9)],
            max_secs=slo_values[-1],
        )

    # --- peak concurrency ---
    peak_ts, peak_count = None, 0
    for ts, cnt in concurrency_by_sec.items():
        if cnt > peak_count:
            peak_count, peak_ts = cnt, ts

    # --- deduplicate + sort timeline ---
    seen: set[tuple] = set()
    timeline: list[TimelineEvent] = []
    for ev in timeline_raw:
        key = (ev.timestamp, ev.event)
        if key not in seen:
            seen.add(key)
            timeline.append(ev)
    timeline.sort(key=lambda e: _ts_to_secs(e.timestamp))

    summary = _build_summary(
        node=node, pod=pod_name, pleg_lags=pleg_lags,
        hk_overruns=hk_overruns, pleg_gaps=pleg_gaps,
        slo_stats=slo_stats, peak_count=peak_count, peak_ts=peak_ts,
    )

    return NodeLogRCAResult(
        node=node, log_path=log_path, pod=pod_name, pod_uid=pod_uid,
        timeline=timeline, pleg_lags=pleg_lags,
        hk_overruns=hk_overruns, pleg_gaps=pleg_gaps,
        slo_stats=slo_stats,
        peak_concurrency=peak_count, peak_concurrency_ts=peak_ts,
        summary=summary,
    )


def _crio_ts(body: str, fallback: str) -> str:
    m = _CRIO_TS_RE.search(body)
    return m.group(1) if m else fallback


def _build_summary(
    node, pod, pleg_lags, hk_overruns, pleg_gaps, slo_stats, peak_count, peak_ts
) -> str:
    parts: list[str] = []

    if pod and pleg_lags:
        app_lags = [lag for lag in pleg_lags if lag.role == "app"]
        if app_lags:
            worst = max(app_lags, key=lambda l: l.lag_secs)
            parts.append(
                f"Pod {pod} on {node}: app container PLEG detection lag = "
                f"{_fmt(worst.lag_secs)} "
                f"(crio started {worst.crio_started_ts}, PLEG fired {worst.pleg_event_ts})."
            )
    elif pod:
        parts.append(f"Pod {pod} on {node}: no PLEG ContainerStarted events found.")

    if hk_overruns:
        peak_hk = max(hk_overruns, key=lambda h: h.actual_secs)
        parts.append(
            f"Housekeeping overruns: {len(hk_overruns)} total, "
            f"peak {_fmt(peak_hk.actual_secs)} at {peak_hk.timestamp}."
        )
    else:
        parts.append("No housekeeping overruns.")

    if pleg_gaps:
        worst_gap = pleg_gaps[0]
        parts.append(
            f"PLEG silence gaps >2s: {len(pleg_gaps)} total, "
            f"worst {_fmt(worst_gap.gap_secs)} ({worst_gap.start_ts} → {worst_gap.end_ts})."
        )

    if slo_stats:
        parts.append(
            f"Node-wide SLO ({slo_stats.count} pods): "
            f"p50={_fmt(slo_stats.p50_secs)}, "
            f"p90={_fmt(slo_stats.p90_secs)}, "
            f"max={_fmt(slo_stats.max_secs)}."
        )

    if peak_count:
        parts.append(f"Peak pod concurrency: {peak_count} events/s at {peak_ts}.")

    # Root cause classification
    app_lag_max = max((l.lag_secs for l in pleg_lags if l.role == "app"), default=0.0)
    if app_lag_max > 5:
        if hk_overruns:
            parts.append(
                "Root cause: continuous PLEG relist saturation — CRI ListContainers "
                "calls blocking under high pod-creation burst; housekeeping overruns "
                "confirm kubelet goroutine pressure. Fix: Evented PLEG (KEP-3386)."
            )
        elif pleg_gaps and pleg_gaps[0].gap_secs > 30:
            parts.append(
                "Root cause: periodic PLEG blackouts — relist blocked on a single slow "
                "CRI call during container churn; no housekeeping overruns. "
                "Fix: Evented PLEG (KEP-3386)."
            )
        else:
            parts.append(
                "Root cause: PLEG detection lag under pod-creation pressure. "
                "Fix: Evented PLEG (KEP-3386)."
            )
    elif pleg_gaps and pleg_gaps[0].gap_secs > 30:
        parts.append(
            "Significant PLEG blackout windows detected. "
            "Evented PLEG (KEP-3386) recommended."
        )

    return " ".join(parts) if parts else f"No significant events found for node {node}."


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_result_markdown(result: NodeLogRCAResult) -> str:
    lines: list[str] = [f"## Node RCA — {result.node}", ""]

    if result.pod:
        lines += [
            f"**Anchor pod:** `{result.pod}`",
            f"**UID:** `{result.pod_uid or 'unknown'}`",
            "",
        ]

    if result.timeline:
        lines += [
            "### Lifecycle timeline",
            "",
            "| Timestamp | Source | Event |",
            "|-----------|--------|-------|",
        ]
        for ev in result.timeline:
            lines.append(f"| `{ev.timestamp}` | {ev.source} | {ev.event} |")
        lines.append("")

    if result.pleg_lags:
        lines += [
            "### PLEG detection lag",
            "",
            "| Role | Container | crio Started | PLEG Event | Lag |",
            "|------|-----------|-------------|------------|-----|",
        ]
        for lag in result.pleg_lags:
            lines.append(
                f"| {lag.role} | `{lag.container_id[:12]}` "
                f"| {lag.crio_started_ts or '—'} | {lag.pleg_event_ts} "
                f"| **{_fmt(lag.lag_secs)}** |"
            )
        lines.append("")

    if result.hk_overruns:
        peak = max(result.hk_overruns, key=lambda h: h.actual_secs)
        lines += [
            "### Housekeeping overruns",
            "",
            f"{len(result.hk_overruns)} overruns. "
            f"Peak: {_fmt(peak.actual_secs)} at `{peak.timestamp}`.",
            "",
        ]
    else:
        lines += ["### Housekeeping overruns", "", "None detected.", ""]

    if result.pleg_gaps:
        lines += [
            "### PLEG silence gaps > 2s",
            "",
            "| Start | End | Gap |",
            "|-------|-----|-----|",
        ]
        for gap in result.pleg_gaps[:10]:
            lines.append(
                f"| `{gap.start_ts}` | `{gap.end_ts}` | **{_fmt(gap.gap_secs)}** |"
            )
        lines.append("")

    if result.slo_stats:
        s = result.slo_stats
        lines += [
            "### Node-wide pod startup SLO",
            "",
            f"count={s.count}  "
            f"min={_fmt(s.min_secs)}  "
            f"p50={_fmt(s.p50_secs)}  "
            f"p90={_fmt(s.p90_secs)}  "
            f"max={_fmt(s.max_secs)}",
            "",
        ]

    lines += ["### Summary", "", result.summary, ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Analyze a Kubernetes node journal for pod startup RCA."
    )
    parser.add_argument("--log", required=True, help="Path to journal file")
    parser.add_argument("--pod", default=None, help="Pod name to anchor timeline")
    parser.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Output raw JSON instead of markdown",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    result = analyze_node_journal(args.log, pod_name=args.pod)

    if args.as_json:
        import json as _json
        print(_json.dumps(result._asdict(), indent=2, default=str))
    else:
        print(format_result_markdown(result))


if __name__ == "__main__":
    main()
