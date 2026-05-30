"""
Tests for node_log_analyzer: parser correctness and Slack message structure.

The "real log" tests use the journal.folded.log checked into the repo under
tests/fixtures/. This file is a trimmed subset of the node journal from
ip-10-0-67-181, captured during the node-density RCA investigation, and
contains the known node-density-956 PLEG lag scenario (17s detection lag,
3 housekeeping overruns, root cause: PLEG relist saturation).
"""

import os
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bugzooka.analysis.node_log_analyzer import (
    NodeLogRCAResult,
    PlegGap,
    PlegLag,
    SloStats,
    TimelineEvent,
    analyze_node_journal,
    format_result_markdown,
)
from bugzooka.integrations.slack_client_base import SlackClientBase

# ---------------------------------------------------------------------------
# Fixture: minimal synthetic journal lines for fast unit tests
# ---------------------------------------------------------------------------

_MINIMAL_JOURNAL = textwrap.dedent("""\
    May 27 16:02:27.147175 ip-10-0-67-181 kubenswrapper[2509]: I0527 16:02:27.147102    2509 kubelet.go:2608] "SyncLoop ADD" source="api" pods=["node-density-0/node-density-956"]
    May 27 16:02:27.620058 ip-10-0-67-181 kubenswrapper[2509]: I0527 16:02:27.619909    2509 reconciler_common.go:225] "operationExecutor.MountVolume started for volume \\"kube-api-access-gxkkp\\" (UniqueName: \\"kubernetes.io/projected/cdcd7672-d87d-434e-86ea-0e2035dcacb8-kube-api-access-gxkkp\\") pod \\"node-density-956\\"" pod="node-density-0/node-density-956"
    May 27 16:02:27.731793 ip-10-0-67-181 kubenswrapper[2509]: I0527 16:02:27.730011    2509 operation_generator.go:614] "MountVolume.SetUp succeeded for volume \\"kube-api-access-gxkkp\\"" pod="node-density-0/node-density-956"
    May 27 16:02:27.950642 ip-10-0-67-181 crio[2440]: time="2026-05-27T16:02:27.950581297Z" level=info msg="Running pod sandbox: node-density-0/node-density-956/POD" id=0be966e9
    May 27 16:02:29.013534 ip-10-0-67-181 crio[2440]: time="2026-05-27T16:02:29.013490793Z" level=info msg="Adding pod node-density-0_node-density-956 to CNI network \\"multus-cni-network\\" (type=multus-shim)"
    May 27 16:02:31.769676 ip-10-0-67-181 crio[2440]: time="2026-05-27T16:02:31.769588511Z" level=info msg="Ran pod sandbox 337b3f3c6d63749cd0091d92e825729e9f818f74c626d58fe72978d0c2ea3320 with infra container: node-density-0/node-density-956/POD"
    May 27 16:02:33.451890 ip-10-0-67-181 crio[2440]: time="2026-05-27T16:02:33.43583158Z" level=info msg="Creating container: node-density-0/node-density-956/node-density"
    May 27 16:02:35.024982 ip-10-0-67-181 crio[2440]: time="2026-05-27T16:02:35.014226005Z" level=info msg="Created container 75ef25174d7d464246c9cb27d850db42604394b920e6f66b56a94ab3d9c5c983: node-density-0/node-density-956/node-density"
    May 27 16:02:35.291348 ip-10-0-67-181 crio[2440]: time="2026-05-27T16:02:35.269681149Z" level=info msg="Started container" PID=133675 containerID=75ef25174d7d464246c9cb27d850db42604394b920e6f66b56a94ab3d9c5c983 description=node-density-0/node-density-956/node-density sandboxID=337b3f3c6d63749cd0091d92e825729e9f818f74c626d58fe72978d0c2ea3320
    May 27 16:02:35.669260 ip-10-0-67-181 kubenswrapper[2509]: I0527 16:02:35.669208    2509 kubelet.go:2639] "SyncLoop (PLEG): event for pod" pod="node-density-0/node-density-956" event={"ID":"cdcd7672-d87d-434e-86ea-0e2035dcacb8","Type":"ContainerStarted","Data":"337b3f3c6d63749cd0091d92e825729e9f818f74c626d58fe72978d0c2ea3320"}
    May 27 16:02:37.208232 ip-10-0-67-181 kubenswrapper[2509]: E0527 16:02:37.198170    2509 kubelet.go:2711] "Housekeeping took longer than expected" err="housekeeping took too long" expected="1s" actual="1.534s"
    May 27 16:02:39.093457 ip-10-0-67-181 kubenswrapper[2509]: E0527 16:02:39.093410    2509 kubelet.go:2711] "Housekeeping took longer than expected" err="housekeeping took too long" expected="1s" actual="1.01s"
    May 27 16:02:52.403214 ip-10-0-67-181 kubenswrapper[2509]: I0527 16:02:52.403189    2509 kubelet.go:2639] "SyncLoop (PLEG): event for pod" pod="node-density-0/node-density-956" event={"ID":"cdcd7672-d87d-434e-86ea-0e2035dcacb8","Type":"ContainerStarted","Data":"75ef25174d7d464246c9cb27d850db42604394b920e6f66b56a94ab3d9c5c983"}
    May 27 16:02:52.388631 ip-10-0-67-181 kubenswrapper[2509]: I0527 16:02:52.388614    2509 pod_startup_latency_tracker.go:148] "Observed pod startup duration" pod="node-density-0/node-density-956" podStartSLOduration=25.388 podStartE2EDuration="25.388s" totalImagesPullingTime="0s" totalInitContainerRuntime="0s" isStatefulPod=false podCreationTimestamp="2026-05-27 16:02:27 +0000 UTC"
""")


@pytest.fixture
def minimal_journal(tmp_path):
    """Write the minimal journal to a temp file and return its path."""
    node_dir = tmp_path / "ip-10-0-67-181.us-west-2.compute.internal"
    node_dir.mkdir()
    journal = node_dir / "journal.decompressed"
    journal.write_text(_MINIMAL_JOURNAL, encoding="utf-8")
    return str(journal)


# ---------------------------------------------------------------------------
# Parser unit tests
# ---------------------------------------------------------------------------


class TestAnalyzeNodeJournal:
    def test_pod_uid_extracted(self, minimal_journal):
        result = analyze_node_journal(minimal_journal, pod_name="node-density-956")
        assert result.pod_uid == "cdcd7672-d87d-434e-86ea-0e2035dcacb8"

    def test_timeline_contains_key_events(self, minimal_journal):
        result = analyze_node_journal(minimal_journal, pod_name="node-density-956")
        event_names = [e.event for e in result.timeline]
        assert "SyncLoop ADD" in event_names
        assert "MountVolume started" in event_names
        assert "MountVolume.SetUp succeeded" in event_names
        assert "RunPodSandbox started" in event_names
        assert "Ran pod sandbox (sandbox ready)" in event_names
        assert "CreateContainer started" in event_names
        assert any("75ef25174d7d" in e for e in event_names), "app container start missing"
        assert any("PLEG ContainerStarted" in e for e in event_names)

    def test_timeline_is_sorted(self, minimal_journal):
        result = analyze_node_journal(minimal_journal, pod_name="node-density-956")
        timestamps = [e.timestamp for e in result.timeline]
        from bugzooka.analysis.node_log_analyzer import _ts_to_secs
        secs = [_ts_to_secs(t) for t in timestamps]
        assert secs == sorted(secs), "timeline events not in chronological order"

    def test_pleg_lag_app_container(self, minimal_journal):
        result = analyze_node_journal(minimal_journal, pod_name="node-density-956")
        app_lags = [l for l in result.pleg_lags if l.role == "app"]
        assert len(app_lags) == 1
        lag = app_lags[0]
        # crio started at 16:02:35.269..., PLEG fired at 16:02:52.403...  → ~17.13s
        assert lag.lag_secs == pytest.approx(17.13, abs=0.1)
        assert lag.container_id.startswith("75ef25174d7d")

    def test_pleg_lag_pause_container_zero(self, minimal_journal):
        result = analyze_node_journal(minimal_journal, pod_name="node-density-956")
        pause_lags = [l for l in result.pleg_lags if l.role == "pause"]
        assert len(pause_lags) == 1
        # no crio StartContainer for the pause container in this log → lag = 0
        assert pause_lags[0].lag_secs == 0.0

    def test_housekeeping_overruns(self, minimal_journal):
        result = analyze_node_journal(minimal_journal, pod_name="node-density-956")
        assert len(result.hk_overruns) == 2
        peak = max(result.hk_overruns, key=lambda h: h.actual_secs)
        assert peak.actual_secs == pytest.approx(1.534)

    def test_pleg_gaps(self, minimal_journal):
        result = analyze_node_journal(minimal_journal, pod_name="node-density-956")
        # gap between the two PLEG events: 52.403 - 35.669 = ~16.73s
        assert len(result.pleg_gaps) == 1
        assert result.pleg_gaps[0].gap_secs == pytest.approx(16.73, abs=0.1)

    def test_slo_stats(self, minimal_journal):
        result = analyze_node_journal(minimal_journal, pod_name="node-density-956")
        assert result.slo_stats is not None
        assert result.slo_stats.count == 1
        assert result.slo_stats.p50_secs == pytest.approx(25.388, abs=0.01)

    def test_no_pod_name_still_parses(self, minimal_journal):
        result = analyze_node_journal(minimal_journal)
        assert result.pod is None
        assert result.timeline == []
        assert result.slo_stats is not None  # SLO parsed regardless of anchor pod

    def test_missing_file_returns_error_result(self, tmp_path):
        result = analyze_node_journal(str(tmp_path / "nonexistent" / "journal"))
        assert "Could not read" in result.summary


# ---------------------------------------------------------------------------
# format_result_markdown output structure
# ---------------------------------------------------------------------------


class TestFormatResultMarkdown:
    def test_sections_present(self, minimal_journal):
        result = analyze_node_journal(minimal_journal, pod_name="node-density-956")
        md = format_result_markdown(result)

        assert "## Node RCA —" in md
        assert "**Anchor pod:** `node-density-956`" in md
        assert "### Lifecycle timeline" in md
        assert "### PLEG detection lag" in md
        assert "### Housekeeping overruns" in md
        assert "### PLEG silence gaps > 2s" in md
        assert "### Node-wide pod startup SLO" in md
        assert "### Summary" in md

    def test_pleg_lag_shown_in_table(self, minimal_journal):
        result = analyze_node_journal(minimal_journal, pod_name="node-density-956")
        md = format_result_markdown(result)
        assert "app" in md
        assert "17." in md  # ~17s lag

    def test_hk_overrun_count_shown(self, minimal_journal):
        result = analyze_node_journal(minimal_journal, pod_name="node-density-956")
        md = format_result_markdown(result)
        assert "2 overruns" in md
        assert "1.534s" in md

    def test_root_cause_in_summary(self, minimal_journal):
        result = analyze_node_journal(minimal_journal, pod_name="node-density-956")
        md = format_result_markdown(result)
        assert "PLEG relist saturation" in md
        assert "KEP-3386" in md

    def test_slack_block_structure(self, minimal_journal):
        """
        Verify the exact Slack block structure produced when the RCA report is
        posted to a thread.  This is what users see in Slack.

        Block layout (use_markdown=True):
            blocks[0]: section / mrkdwn  ← header ":mag: *Node Journal RCA*"
            blocks[1]: markdown           ← full format_result_markdown output
        """
        result = analyze_node_journal(minimal_journal, pod_name="node-density-956")
        report = format_result_markdown(result)

        # Replicate what slack_fetcher._process_message does
        header = ":mag: *Node Journal RCA* (podReadyLatency_P99 regression)\n"

        # Use a bare SlackClientBase instance (no real Slack connection needed)
        client = SlackClientBase.__new__(SlackClientBase)
        blocks = client.get_slack_message_blocks(
            markdown_header=header,
            content_text=report,
            use_markdown=True,
        )

        assert len(blocks) == 2

        # Header block
        header_block = blocks[0]
        assert header_block["type"] == "section"
        assert header_block["text"]["type"] == "mrkdwn"
        assert "Node Journal RCA" in header_block["text"]["text"]
        assert "podReadyLatency_P99" in header_block["text"]["text"]

        # Content block — full markdown report
        content_block = blocks[1]
        assert content_block["type"] == "markdown"
        content = content_block["text"]

        # Key structural assertions — what the user actually reads in Slack
        assert "## Node RCA —" in content
        assert "node-density-956" in content
        assert "### PLEG detection lag" in content
        assert "17." in content          # ~17s lag visible in table
        assert "2 overruns" in content   # housekeeping count
        assert "PLEG relist saturation" in content
        assert "KEP-3386" in content
