"""The dashboard reads local files, serves them to this computer only, and changes nothing."""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from bone_reaper.dashboard.data import (
    SHADOW_RECENT_RECORDS,
    ReportCache,
    build_state,
    parse_readiness_memo,
)
from bone_reaper.dashboard.server import BIND_HOST, make_server
from bone_reaper.settings import parse_settings

ROOT = Path(__file__).parents[2]
STATIC = ROOT / "src" / "bone_reaper" / "dashboard" / "static"
MEMO = """# Readiness memo

**Date:** 2026-11-07
**Verdict: FAIL. Do not proceed.**

| Question | Verdict |
|---|---|
| Is it safe? | **PASS** |
| Can it place a real order? | **No.** Every route refuses |
| Is there an edge? | **FAIL:** none observed |
| Does the model help? | **FAIL (NO-GO):** still negative |
| Could another design work? | **INCONCLUSIVE:** not tested |

| Item | Value |
|---|---|
| Markets | 37 |
"""


def state_for(root: Path, cache: ReportCache | None = None) -> dict:
    return build_state(root, parse_settings({}), cache or ReportCache())


def write_json(path: Path, doc: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


class TestMemoParsing:
    def test_reads_the_verdict_and_each_answer(self):
        memo = parse_readiness_memo(MEMO)
        assert (memo["verdict"], memo["tone"]) == ("FAIL", "critical")
        assert memo["statement"] == "Do not proceed."
        assert memo["date"] == "2026-11-07"
        assert [(q["status"], q["tone"]) for q in memo["questions"]] == [
            ("PASS", "good"),
            ("No", "neutral"),
            ("FAIL", "critical"),
            ("FAIL", "critical"),
            ("INCONCLUSIVE", "warning"),
        ]
        assert memo["questions"][1]["detail"] == "Every route refuses"
        assert memo["questions"][3]["detail"] == "NO-GO: still negative"

    def test_a_memo_without_a_verdict_is_not_given_one(self):
        memo = parse_readiness_memo("# Draft\n")
        assert memo["verdict"] is None and memo["tone"] == "neutral" and memo["questions"] == []

    def test_the_real_memo_parses(self):
        memo = parse_readiness_memo((ROOT / "reports" / "readiness_memo.md").read_text("utf-8"))
        assert memo["verdict"] and len(memo["questions"]) >= 5


class TestState:
    def test_an_empty_project_reports_nothing_rather_than_zeros(self, tmp_path):
        state = state_for(tmp_path)
        for name in ("memo", "compare", "scan", "forecast", "audit", "coverage", "performance"):
            assert state[name]["available"] is False, name
            assert "doc" not in state[name]
        assert state["recorders"] == [] and state["sessions"] == []

    def test_live_trading_is_shown_as_blocked_by_the_real_gate(self, tmp_path):
        reasons = state_for(tmp_path)["meta"]["live_disabled_reasons"]
        assert "real_adapter_not_implemented_p7_blocked" in reasons

    def test_money_in_reports_passes_through_exactly(self, tmp_path):
        write_json(tmp_path / "reports/compare/compare.json", {"rows": [{"pnl": "-2.70260000"}]})
        section = state_for(tmp_path)["compare"]
        assert section["doc"]["rows"][0]["pnl"] == "-2.70260000"
        assert section["modified_utc_ms"] > 0

    def test_a_corrupt_report_is_unavailable(self, tmp_path):
        path = tmp_path / "reports/scan_real/scan.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        assert state_for(tmp_path)["scan"]["available"] is False

    def test_a_failed_read_keeps_the_last_good_copy(self, tmp_path):
        path = tmp_path / "reports/scan_real/scan.json"
        write_json(path, {"summary": 1})
        cache = ReportCache()
        assert state_for(tmp_path, cache)["scan"]["doc"] == {"summary": 1}
        path.write_text("{half writ", encoding="utf-8")
        assert state_for(tmp_path, cache)["scan"]["doc"] == {"summary": 1}

    def test_a_deleted_report_stops_being_shown(self, tmp_path):
        path = tmp_path / "reports/scan_real/scan.json"
        write_json(path, {"summary": 1})
        cache = ReportCache()
        assert state_for(tmp_path, cache)["scan"]["available"] is True
        path.unlink()
        assert state_for(tmp_path, cache)["scan"]["available"] is False

    def test_recorders_are_newest_first_with_alerts(self, tmp_path):
        write_json(tmp_path / "data/old/health.json", {"ts_utc_ms": 1, "gaps": 0})
        write_json(tmp_path / "data/raw/health.json", {"ts_utc_ms": 2, "gaps": 3})
        recorders = state_for(tmp_path)["recorders"]
        assert [r["name"] for r in recorders] == ["data/raw", "data/old"]
        assert any(a["code"] == "data_gaps" for a in recorders[0]["alerts"])

    def test_session_kinds(self, tmp_path):
        metrics = {"events_processed": 1}
        write_json(tmp_path / "reports/one/metrics.json", metrics)
        write_json(tmp_path / "reports/two/metrics.json", metrics)
        (tmp_path / "reports/two/raw").mkdir()
        write_json(tmp_path / "reports/three/metrics.json", metrics)
        (tmp_path / "reports/three/shadow_records.jsonl").write_text("", encoding="utf-8")
        kinds = {s["name"]: s["kind"] for s in state_for(tmp_path)["sessions"]}
        assert kinds == {"one": "replay", "two": "paper", "three": "shadow"}

    def test_research_folders_are_not_sessions(self, tmp_path):
        write_json(tmp_path / "reports/compare/metrics.json", {"x": 1})
        assert state_for(tmp_path)["sessions"] == []

    def test_a_running_session_shows_its_snapshot_and_sorts_first(self, tmp_path):
        write_json(tmp_path / "reports/done/metrics.json", {"events_processed": 1})
        write_json(
            tmp_path / "reports/now/live.json",
            {
                "running": True,
                "ts_utc_ms": 5,
                "metrics": {"events_processed": 2},
                "equity_curve": [[1, "100"], [2, "99"]],
                "active_market": {"slug": "x"},
                "open_orders": [{"intent_id": "a"}],
            },
        )
        write_json(tmp_path / "reports/now/metrics.json", {"events_processed": 1})
        first = state_for(tmp_path)["sessions"][0]
        assert first["name"] == "now" and first["running"] is True
        assert first["metrics"]["events_processed"] == 2
        assert first["equity_curve"][-1] == [2, "99"]
        assert first["open_orders"] == [{"intent_id": "a"}]

    def test_a_replay_curve_is_downsampled_but_keeps_its_last_point(self, tmp_path):
        write_json(tmp_path / "reports/r/metrics.json", {"events_processed": 1})
        timeline = [{"ts_utc_ms": i, "equity": str(i)} for i in range(5000)]
        write_json(tmp_path / "reports/r/timeline.json", timeline)
        curve = state_for(tmp_path)["sessions"][0]["equity_curve"]
        assert len(curve) == 400 and curve[-1] == [4999, "4999"]

    def test_shadow_log_keeps_full_counts_but_few_records(self, tmp_path):
        write_json(tmp_path / "reports/s/metrics.json", {"events_processed": 1})
        lines = [
            json.dumps({"kind": "hypothetical_order", "ts_utc_ms": i, "body": {"price": "0.5"}})
            for i in range(SHADOW_RECENT_RECORDS + 20)
        ]
        lines.append('{"kind": "hypothetical_fi')  # The writer can be mid-line.
        (tmp_path / "reports/s/shadow_records.jsonl").write_text("\n".join(lines), encoding="utf-8")
        shadow = state_for(tmp_path)["sessions"][0]["shadow"]
        assert shadow["counts"] == {"hypothetical_order": SHADOW_RECENT_RECORDS + 20}
        assert len(shadow["recent"]) == SHADOW_RECENT_RECORDS
        assert shadow["recent"][0]["ts_utc_ms"] == SHADOW_RECENT_RECORDS + 19

    def test_building_state_writes_nothing(self, tmp_path):
        write_json(tmp_path / "reports/r/metrics.json", {"events_processed": 1})
        write_json(tmp_path / "data/raw/health.json", {"ts_utc_ms": 1})
        before = sorted(p for p in tmp_path.rglob("*"))
        state_for(tmp_path)
        assert sorted(p for p in tmp_path.rglob("*")) == before


@pytest.fixture
def server(tmp_path):
    write_json(tmp_path / "reports/scan_real/scan.json", {"summary": {"evaluations": 9}})
    instance = make_server(tmp_path, parse_settings({}), 0)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance
    instance.shutdown()
    instance.server_close()
    thread.join(timeout=5)


def request(server, method, path, host=None):
    port = server.server_address[1]
    connection = http.client.HTTPConnection(BIND_HOST, port, timeout=5)
    headers = {"Host": host or f"127.0.0.1:{port}"}
    connection.request(method, path, headers=headers)
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response, body


class TestServer:
    def test_binds_to_this_computer_only(self, server):
        assert server.server_address[0] == "127.0.0.1"

    def test_serves_the_page_with_a_strict_policy(self, server):
        response, body = request(server, "GET", "/")
        assert response.status == 200 and b"Bone Reaper" in body
        policy = response.getheader("Content-Security-Policy")
        assert "default-src 'none'" in policy and "script-src 'self'" in policy
        assert "form-action 'none'" in policy
        assert response.getheader("X-Content-Type-Options") == "nosniff"

    @pytest.mark.parametrize("path", ["/app.js", "/app.css", "/theme.js", "/favicon.svg"])
    def test_serves_each_asset(self, server, path):
        response, body = request(server, "GET", path)
        assert response.status == 200 and body

    def test_state_is_json_from_the_project_folder(self, server):
        response, body = request(server, "GET", "/api/state")
        assert response.status == 200
        assert response.getheader("Content-Type").startswith("application/json")
        assert json.loads(body)["scan"]["doc"]["summary"]["evaluations"] == 9

    def test_localhost_is_accepted(self, server):
        port = server.server_address[1]
        response, _ = request(server, "GET", "/api/state", host=f"localhost:{port}")
        assert response.status == 200

    @pytest.mark.parametrize(
        "host", ["evil.example", "evil.example:8765", "127.0.0.1.evil.example"]
    )
    def test_other_host_names_are_refused(self, server, host):
        response, body = request(server, "GET", "/api/state", host=host)
        assert response.status == 403 and b"evaluations" not in body

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_nothing_can_be_written_or_started(self, server, method):
        for path in ("/", "/api/state"):
            response, _ = request(server, method, path)
            assert response.status == 501

    @pytest.mark.parametrize(
        "path", ["/../pyproject.toml", "/static/app.js", "/reports/scan_real/scan.json", "/api"]
    )
    def test_only_listed_paths_exist(self, server, path):
        response, _ = request(server, "GET", path)
        assert response.status == 404

    def test_head_sends_headers_only(self, server):
        response, body = request(server, "HEAD", "/api/state")
        assert response.status == 200 and body == b""

    def test_a_failure_while_building_state_is_a_500(self, server, monkeypatch):
        def explode(*_args):
            raise RuntimeError("boom")

        monkeypatch.setattr("bone_reaper.dashboard.server.build_state", explode)
        response, body = request(server, "GET", "/api/state")
        assert response.status == 500 and b"boom" not in body


class TestPage:
    def test_the_page_only_ever_reads(self):
        script = (STATIC / "app.js").read_text("utf-8")
        assert script.count("fetch(") == 1 and 'fetch("/api/state"' in script
        assert "method:" not in script and "XMLHttpRequest" not in script
        assert "sendBeacon" not in script and "WebSocket" not in script

    def test_the_page_says_results_are_simulated(self):
        script = (STATIC / "app.js").read_text("utf-8")
        assert "All profit and loss here is simulated" in script

    def test_no_emoji_icons_or_inline_scripts(self):
        page = (STATIC / "index.html").read_text("utf-8")
        assert "<script>" not in page and " onclick=" not in page

    def test_port_in_use_is_a_clear_error(self, server, capsys):
        from bone_reaper.cli import main

        assert main(["dashboard", "--port", str(server.server_address[1])]) == 1
        assert "--port" in capsys.readouterr().err
