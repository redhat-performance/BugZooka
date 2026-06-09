#!/usr/bin/env python3
"""Fetch BugZooka telemetry from Elasticsearch and write a static JSON file.

Usage:
    ES_URL=https://user:pass@your-es-host python scripts/fetch_analytics.py

Or with explicit credentials:
    ES_URL=https://your-es-host ES_USER=user ES_PASS=pass python scripts/fetch_analytics.py

The output is written to docs/analytics-data.json, ready for GitHub Pages.
"""

import base64
import json
import os
import ssl
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ES_URL = os.environ.get("ES_URL", "")
ES_USER = os.environ.get("ES_USER", "")
ES_PASS = os.environ.get("ES_PASS", "")
ES_INDEX = os.environ.get("ES_INDEX", "bugzooka-telemetry")
OUTPUT = os.environ.get("OUTPUT", os.path.join(os.path.dirname(__file__), "..", "docs", "analytics-data.json"))


def get_auth():
    if ES_USER and ES_PASS:
        return (ES_USER, ES_PASS)
    parsed = urlparse(ES_URL)
    if parsed.username and parsed.password:
        return (parsed.username, parsed.password)
    return None


def get_base_url():
    parsed = urlparse(ES_URL)
    if parsed.username:
        return f"{parsed.scheme}://{parsed.hostname}" + (f":{parsed.port}" if parsed.port else "")
    return ES_URL.rstrip("/")


def query_es(body):
    base = get_base_url()
    url = f"{base}/{ES_INDEX}/_search"
    data = json.dumps(body).encode("utf-8")
    req = Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    auth = get_auth()
    if auth:
        cred = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        req.add_header("Authorization", f"Basic {cred}")
    ctx = ssl.create_default_context()
    with urlopen(req, timeout=30, context=ctx) as resp:
        return json.loads(resp.read())


def fetch_all():
    return query_es({
        "size": 0,
        "aggs": {
            "unique_users": {"cardinality": {"field": "user_id"}},
            "unique_teams": {"cardinality": {"field": "team"}},
            "unique_channels": {"cardinality": {"field": "channel_id"}},
            "success_buckets": {"terms": {"field": "success", "size": 10}},
            "percentiles": {"percentiles": {"field": "duration_ms", "percents": [50, 95]}},
            "total_tokens": {"sum": {"field": "total_tokens"}},
            "by_team": {"terms": {"field": "team", "size": 20}},
            "by_trigger": {"terms": {"field": "trigger_type", "size": 10}},
            "by_command": {
                "terms": {"field": "command", "size": 20},
                "aggs": {"total_tokens": {"sum": {"field": "total_tokens"}}},
            },
            "fail_by_command": {
                "filter": {"term": {"success": False}},
                "aggs": {"cmds": {"terms": {"field": "command", "size": 20}}},
            },
            "over_time": {
                "date_histogram": {"field": "timestamp", "calendar_interval": "day"},
                "aggs": {
                    "success_count": {"filter": {"term": {"success": True}}},
                    "fail_count": {"filter": {"term": {"success": False}}},
                    "p50": {"percentiles": {"field": "duration_ms", "percents": [50]}},
                    "p95": {"percentiles": {"field": "duration_ms", "percents": [95]}},
                    "tokens": {"sum": {"field": "total_tokens"}},
                },
            },
            "commands_over_time": {
                "date_histogram": {"field": "timestamp", "calendar_interval": "day"},
                "aggs": {"cmds": {"terms": {"field": "command", "size": 10}}},
            },
            "latency_by_cmd": {
                "terms": {"field": "command", "size": 20},
                "aggs": {
                    "p50": {"percentiles": {"field": "duration_ms", "percents": [50]}},
                    "p95": {"percentiles": {"field": "duration_ms", "percents": [95]}},
                },
            },
            "team_details": {
                "terms": {"field": "team", "size": 20},
                "aggs": {
                    "success": {"filter": {"term": {"success": True}}},
                    "fail": {"filter": {"term": {"success": False}}},
                    "p50_latency": {"percentiles": {"field": "duration_ms", "percents": [50]}},
                    "total_tokens": {"sum": {"field": "total_tokens"}},
                    "unique_users": {"cardinality": {"field": "user_id"}},
                    "by_trigger": {"terms": {"field": "trigger_type", "size": 5}},
                    "cmd_detail": {
                        "terms": {"field": "command", "size": 10},
                        "aggs": {
                            "ok": {"filter": {"term": {"success": True}}},
                            "fail": {"filter": {"term": {"success": False}}},
                            "avg_latency": {"avg": {"field": "duration_ms"}},
                            "tokens": {"sum": {"field": "total_tokens"}},
                        },
                    },
                },
            },
        },
    })


def transform(raw):
    aggs = raw["aggregations"]
    sb = aggs["success_buckets"]["buckets"]
    true_count = next((b["doc_count"] for b in sb if b.get("key_as_string") == "true"), 0)
    false_count = next((b["doc_count"] for b in sb if b.get("key_as_string") == "false"), 0)
    total = true_count + false_count

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kpis": {
            "total_requests": total,
            "unique_users": aggs["unique_users"]["value"],
            "unique_teams": aggs["unique_teams"]["value"],
            "active_channels": aggs["unique_channels"]["value"],
            "success_rate": round((true_count / total) * 100, 2) if total else 0,
            "success_count": true_count,
            "fail_count": false_count,
            "p50_latency_ms": round(aggs["percentiles"]["values"]["50.0"] or 0),
            "p95_latency_ms": round(aggs["percentiles"]["values"]["95.0"] or 0),
            "total_tokens": int(aggs["total_tokens"]["value"]),
        },
        "usage": {
            "by_team": [{"team": b["key"], "count": b["doc_count"]} for b in aggs["by_team"]["buckets"]],
            "by_trigger": [{"trigger": b["key"], "count": b["doc_count"]} for b in aggs["by_trigger"]["buckets"]],
        },
        "features": {
            "commands": [
                {"command": b["key"], "count": b["doc_count"], "total_tokens": int(b["total_tokens"]["value"])}
                for b in aggs["by_command"]["buckets"]
            ],
            "commands_over_time": [
                {"date": b["key_as_string"], "commands": [{"command": c["key"], "count": c["doc_count"]} for c in b["cmds"]["buckets"]]}
                for b in aggs["commands_over_time"]["buckets"]
            ],
        },
        "reliability": {
            "fail_by_command": [
                {"command": b["key"], "count": b["doc_count"]}
                for b in aggs["fail_by_command"]["cmds"]["buckets"]
            ],
            "over_time": [
                {"date": b["key_as_string"], "success": b["success_count"]["doc_count"], "fail": b["fail_count"]["doc_count"]}
                for b in aggs["over_time"]["buckets"]
            ],
        },
        "performance": {
            "over_time": [
                {
                    "date": b["key_as_string"],
                    "p50": round(b["p50"]["values"]["50.0"]) if b["p50"]["values"]["50.0"] is not None else None,
                    "p95": round(b["p95"]["values"]["95.0"]) if b["p95"]["values"]["95.0"] is not None else None,
                }
                for b in aggs["over_time"]["buckets"]
            ],
            "by_command": [
                {
                    "command": b["key"],
                    "p50": round(b["p50"]["values"]["50.0"]) if b["p50"]["values"]["50.0"] is not None else None,
                    "p95": round(b["p95"]["values"]["95.0"]) if b["p95"]["values"]["95.0"] is not None else None,
                }
                for b in aggs["latency_by_cmd"]["buckets"]
            ],
        },
        "tokens": {
            "over_time": [
                {"date": b["key_as_string"], "tokens": int(b["tokens"]["value"])}
                for b in aggs["over_time"]["buckets"]
            ],
            "by_command": [
                {"command": b["key"], "count": b["doc_count"], "tokens": int(b["total_tokens"]["value"])}
                for b in aggs["by_command"]["buckets"]
            ],
        },
        "team_details": [
            {
                "team": b["key"],
                "total": b["doc_count"],
                "success": b["success"]["doc_count"],
                "fail": b["fail"]["doc_count"],
                "success_rate": round((b["success"]["doc_count"] / b["doc_count"]) * 100, 2) if b["doc_count"] else 0,
                "p50_latency": round(b["p50_latency"]["values"]["50.0"]) if b["p50_latency"]["values"]["50.0"] is not None else None,
                "tokens": int(b["total_tokens"]["value"]),
                "users": b["unique_users"]["value"],
                "trigger": ", ".join(t["key"] for t in b["by_trigger"]["buckets"]),
                "command_details": [
                    {
                        "command": c["key"],
                        "total": c["doc_count"],
                        "success": c["ok"]["doc_count"],
                        "fail": c["fail"]["doc_count"],
                        "success_rate": round((c["ok"]["doc_count"] / c["doc_count"]) * 100, 2) if c["doc_count"] else 0,
                        "avg_latency": round(c["avg_latency"]["value"] or 0),
                        "tokens": int(c["tokens"]["value"]),
                    }
                    for c in b["cmd_detail"]["buckets"]
                ],
            }
            for b in aggs["team_details"]["buckets"]
        ],
    }


def main():
    if not ES_URL:
        print("ERROR: ES_URL environment variable is required", file=sys.stderr)
        print(__doc__, file=sys.stderr)
        sys.exit(1)

    print(f"Fetching telemetry from {get_base_url()}/{ES_INDEX}...")
    raw = fetch_all()
    data = transform(raw)

    output_path = os.path.abspath(OUTPUT)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(data, f, indent=2)

    print(f"Written {output_path}")
    print(f"  Total requests: {data['kpis']['total_requests']}")
    print(f"  Unique users:   {data['kpis']['unique_users']}")
    print(f"  Teams:          {data['kpis']['unique_teams']}")
    print(f"  Success rate:   {data['kpis']['success_rate']}%")
    print(f"  Total tokens:   {data['kpis']['total_tokens']}")


if __name__ == "__main__":
    main()
