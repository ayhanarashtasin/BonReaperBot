"""End-to-end over the committed synthetic fixture and the real CLI. Offline: the autouse network
guard in conftest fails any attempt to reach a non-local host, which also proves zero sends."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from bone_reaper.app import Recorder
from bone_reaper.cli import main
from bone_reaper.live_sim import attach_core, run_live_sim
from bone_reaper.market.rules import get_instrument
from bone_reaper.research.experiments import (
    chronological_partitions,
    load_all,
    market_specs,
    run_compare,
)
from bone_reaper.research.metrics import compute_metrics
from bone_reaper.research.replay import SimCore, replay_events
from bone_reaper.research.scan_runner import run_scan
from bone_reaper.settings import load_settings, parse_settings
from bone_reaper.storage.raw_events import load_manifest, read_events
from bone_reaper.telemetry.health import coverage_report
from bone_reaper.time import SystemClock
from bone_reaper.types import canonical_json
from helpers import book_payload, change, price_change_payload, trade_payload

ROOT = Path(__file__).parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "replay"
READ_ONLY = ROOT / "config" / "read_only.yaml"
PAPER = ROOT / "config" / "paper.yaml"


@pytest.fixture(scope="module")
def events():
    return load_all(FIXTURE)


@pytest.fixture(scope="module")
def paper():
    return load_settings(PAPER)


class TestG1Recording:
    def test_three_sequential_markets_full_lifespans(self):
        report = coverage_report(FIXTURE)
        assert report["checksum_problems"] == []
        assert len(report["markets"]) == 3 and report["full_lifespans"] == 3
        assert all(m["resolved"] for m in report["markets"])
        assert report["twap_points"] > 800

    def test_discontinuity_is_disclosed_not_hidden(self):
        report = coverage_report(FIXTURE)
        assert report["gaps_total"] == 1 and report["markets"][1]["market_gaps"] == 1

    def test_rebuilt_books_match_independent_samples(self):
        """The fixture has no missed events, so depth must match every REST and stream sample."""
        report = coverage_report(FIXTURE)
        rest = report["rest_validation"]
        assert rest["samples"] > 20 and rest["top_of_book_match"] == rest["samples"]
        stream = report["stream_snapshot_validation"]
        # A book message follows a trade, so it often differs at the top on purpose. Only the
        # REST samples above are a strict comparison; this just proves the check is running.
        assert stream["checks"] > 100 and stream["top_of_book_match"] > stream["checks"] / 2

    def test_no_stale_asset_reuse_across_rollover(self, events):
        specs = market_specs(events)
        assets = [a for s in specs for a in (s.yes_asset_id, s.no_asset_id)]
        assert len(assets) == len(set(assets)) == 6


class TestG2ScannerCli:
    def test_scan_exports_records_and_summary(self, tmp_path, capsys):
        report_path = tmp_path / "scan.json"
        code = main(
            [
                "scan",
                "--input",
                str(FIXTURE),
                "--config",
                str(READ_ONLY),
                "--report",
                str(report_path),
            ]
        )
        assert code == 0
        doc = json.loads(report_path.read_text())
        assert doc["execution"].startswith("none")
        assert doc["summary"]["evaluations"] > 1000
        assert doc["sdk_version"] and len(doc["config_hash"]) == 64
        assert doc["known_gaps"], "gaps reach the report"
        records = report_path.with_suffix(".records.jsonl").read_text().splitlines()
        first = json.loads(records[0])
        assert "reasons" in first and "fee_version" in first

    def test_scan_is_reproducible(self, tmp_path):
        settings = load_settings(READ_ONLY)
        one = run_scan(FIXTURE, settings, tmp_path / "a.json")
        two = run_scan(FIXTURE, settings, tmp_path / "b.json")
        assert one["records_sha256"] == two["records_sha256"]

    def test_candidates_carry_adverse_cases_and_funding(self, tmp_path):
        settings = load_settings(READ_ONLY)
        run_scan(FIXTURE, settings, tmp_path / "a.json")
        rows = [json.loads(x) for x in (tmp_path / "a.records.jsonl").read_text().splitlines()]
        complete = [
            r
            for r in rows
            if r["yes_leg"]["gross_shares"] != "0" and r["no_leg"]["gross_shares"] != "0"
        ]
        assert complete and all(len(r["adverse_cases"]) >= 1 for r in complete)
        for row in complete:
            for case in row["adverse_cases"]:
                assert float(case["funding_required"]) > 0

    def test_scanner_has_no_order_route(self):
        import ast

        source = (ROOT / "src/bone_reaper/strategy/pair_scanner.py").read_text("utf-8")
        modules = {
            node.module or ""
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom)
        }
        assert not any("execution" in m or "polymarket" in m for m in modules)
        assert "bone_reaper.execution" not in (
            ROOT / "src/bone_reaper/research/scan_runner.py"
        ).read_text("utf-8")


class TestG3Replay:
    def test_cli_replay_writes_metrics_and_manifest(self, tmp_path, capsys):
        out = tmp_path / "replay"
        assert (
            main(["replay", "--input", str(FIXTURE), "--config", str(PAPER), "--output", str(out)])
            == 0
        )
        metrics = json.loads((out / "metrics.json").read_text())
        manifest = json.loads((out / "manifest.json").read_text())
        assert metrics["simulated"] is True
        assert metrics["ledger"]["conservation_errors"] == []
        assert metrics["safety"]["visibility_violations"] == 0
        assert metrics["reconciliation"]["runs"] == 3
        assert metrics["reconciliation"]["clean"] == metrics["reconciliation"]["runs"]
        assert metrics["markets_unresolved"] == []
        assert manifest["config_hash"] and manifest["inputs"] and manifest["assumptions"]
        assert manifest["sdk_version"] and manifest["code_commit"]
        assert any(key == "grade" for key, _ in manifest["coverage"])
        assert "Not real earnings" in capsys.readouterr().out

    def test_byte_identical_outputs_for_same_seed(self, tmp_path):
        outputs = []
        for name in ("a", "b"):
            out = tmp_path / name
            main(
                [
                    "replay",
                    "--input",
                    str(FIXTURE),
                    "--config",
                    str(PAPER),
                    "--output",
                    str(out),
                    "--seed",
                    "9",
                ]
            )
            outputs.append((out / "metrics.json").read_bytes())
        assert outputs[0] == outputs[1]

    def test_all_fills_are_unique_and_simulated(self, events, paper):
        core = replay_events(events, paper, seed=7)
        identities = [f.identity for f in core.exchange.truth.trades]
        assert len(identities) == len(set(identities))
        assert all(f.simulated for f in core.exchange.truth.trades)

    def test_ledger_agrees_with_independent_exchange_truth(self, events, paper):
        core = replay_events(events, paper, seed=7)
        assert core.account.total_cash == core.exchange.truth.cash
        assert core.account.cash_redeemable == 0, "all payouts redeemed"

    def test_realized_pnl_reconciles_to_cash(self, events, paper):
        core = replay_events(events, paper, seed=7)
        metrics = compute_metrics(core)
        start = paper.paper.starting_cash_usd
        assert core.account.cash_available - start == metrics["pnl"]["realized_settled"]

    def test_latency_stress_changes_nothing_about_accounting(self, events, paper):
        from bone_reaper.research.experiments import SCENARIOS

        for name in ("pessimistic_2x_latency", "optimistic", "central_higher_fees"):
            core = replay_events(events, paper, SCENARIOS[name], seed=7)
            assert core.account.ledger.conservation_errors() == []
            assert core.account.total_cash == core.exchange.truth.cash

    def test_causality_later_data_cannot_change_earlier_decisions(self, events, paper):
        """Truncating the data must not change any order decided before the cut."""
        cut = market_specs(events)[1].start_utc_ms
        full = replay_events(events, paper, seed=7)
        partial = replay_events([e for e in events if e.received_utc_ms < cut], paper, seed=7)
        early = cut - 60_000

        def decided(core):
            return {
                i: (o.intent.price, o.intent.size)
                for i, o in core.oms.orders.items()
                if o.created_utc_ms < early
            }

        assert decided(full) and decided(full) == decided(partial)

    def test_unresolved_positions_stay_reported_never_force_settled(self, events, paper):
        without = [e for e in events if e.topic != "market_resolution"]
        core = replay_events(without, paper, seed=7)
        metrics = compute_metrics(core)
        assert metrics["pnl"]["realized_settled"] == 0
        if metrics["markets_traded"]:
            assert metrics["markets_unresolved"]
            assert metrics["pnl"]["unresolved_cost_at_risk"] > 0


class TestG4Compare:
    def test_compare_report_separates_partitions_and_assumptions(self, tmp_path, paper, capsys):
        summary = run_compare(FIXTURE, paper, tmp_path, 7)
        assert summary["rows"] > 10
        report = json.loads((tmp_path / "compare.json").read_text())
        names = {r["strategy"] for r in report["rows"]}
        assert names == {
            "scanner_only",
            "taker_pair_research",
            "passive_maker",
            "passive_constrained_hedge",
            "passive_maker_no_loss_stop",
            "passive_hedge_no_loss_stop",
        }
        scenarios = {r["scenario"] for r in report["rows"]}
        assert {"pessimistic", "central", "pessimistic_2x_latency"} <= scenarios
        assert all(r["ledger_errors"] == 0 for r in report["rows"])
        assert all(r["unclean_reconciliations"] == 0 for r in report["rows"])
        text = (tmp_path / "compare.md").read_text()
        assert "SIMULATED" in text and "untouched_test" in text and "Queue position" in text

    def test_scanner_only_never_trades(self, tmp_path, paper):
        run_compare(FIXTURE, paper, tmp_path, 7)
        report = json.loads((tmp_path / "compare.json").read_text())
        for row in report["rows"]:
            if row["strategy"] == "scanner_only":
                assert row["fills"] == 0 and row["markets_traded"] == 0

    def test_partitions_on_fixture_stay_chronological(self, events):
        parts = chronological_partitions(market_specs(events), embargo_markets=0)
        assert [p.name for p in parts] == ["train", "validation", "untouched_test"]


async def _fake_stream_factory(client, assets):
    """A live-shaped feed for one market: books, updates and a trade that fills a quote."""
    yes, no = assets
    clock = SystemClock()
    condition = _fake_stream_factory.condition
    for _ in range(3):
        now = clock.utc_ms()
        yield book_payload(yes, [("0.48", "100")], [("0.52", "100")], now, condition), 0
        yield book_payload(no, [("0.48", "100")], [("0.52", "100")], now, condition), 0
        await asyncio.sleep(0.05)
    for i in range(30):
        now = clock.utc_ms()
        payload = price_change_payload(
            now,
            change(yes, "BUY", "0.40", str(50 + i), "0.48", "0.52"),
            change(no, "SELL", "0.60", str(50 + i), "0.48", "0.52"),
        ) | {"market": condition}
        yield payload, 0
        if i == 10:
            yield (
                (
                    trade_payload(yes, "SELL", "0.30", "50", now, f"0xlive{i}")
                    | {"market": condition}
                ),
                0,
            )
        await asyncio.sleep(0.05)
    await asyncio.sleep(30)


class FakeLiveTransport:
    client = object()

    def __init__(self, event):
        self.event = event
        self.closed = False

    async def series_id(self, slug):
        return 1

    async def open_events(self, series_id, now, limit):
        return [self.event]

    async def book_payload(self, asset_id):
        raise RuntimeError("no REST in this test")

    async def event_by_slug(self, slug):
        return self.event

    async def close(self):
        self.closed = True


def _live_event(gamma_upcoming):
    """Shift the captured real event's window around now, so that it is the active market."""
    import copy
    from datetime import UTC, datetime

    from polymarket import Event

    raw = copy.deepcopy(gamma_upcoming)
    now = SystemClock().utc_ms()
    # One minute in, whatever the wall clock says, so the near-expiry cutoff can never interfere.
    start = (now - 60_000) // 1000 * 1000
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    raw["startTime"] = datetime.fromtimestamp(start / 1000, UTC).strftime(fmt)
    raw["endDate"] = datetime.fromtimestamp((start + 300_000) / 1000, UTC).strftime(fmt)
    raw["markets"][0]["endDate"] = raw["endDate"]
    return (
        Event.parse_response(raw) if hasattr(Event, "parse_response") else Event.model_validate(raw)
    )


class TestShadowAndPaperLive:
    def _run(self, gamma_upcoming, tmp_path, mode):
        event = _live_event(gamma_upcoming)
        _fake_stream_factory.condition = event.markets[0].condition_id
        settings = parse_settings(
            {"mode": mode, "strategies": {"passive_quotes": True, "refresh_min_age_ms": 60000}}
        )
        core = SimCore(settings, shadow_path=tmp_path / "shadow.jsonl")
        recorder = Recorder(
            settings,
            get_instrument("BTC", 5),
            tmp_path / "raw",
            transport=FakeLiveTransport(event),
            enable_twap=False,
            market_stream=_fake_stream_factory,
        )
        attach_core(recorder, core)
        asyncio.run(recorder.run(1.5))
        return core, recorder

    def test_shadow_records_hypothetical_orders_with_zero_real_sends(
        self, gamma_upcoming, tmp_path, monkeypatch
    ):
        import polymarket

        def forbidden(*args, **kwargs):
            raise AssertionError("no secure client may be constructed in shadow mode")

        monkeypatch.setattr(polymarket.AsyncSecureClient, "__init__", forbidden)
        core, _ = self._run(gamma_upcoming, tmp_path, "shadow")
        assert type(core.port).__name__ == "ShadowExecution" and core.port.is_real is False
        kinds = {r["kind"] for r in core.port.records}
        assert "hypothetical_order" in kinds
        lines = (tmp_path / "shadow.jsonl").read_text().splitlines()
        assert lines and all(json.loads(x)["hypothetical"] is True for x in lines)

    def test_paper_matches_replay_on_the_same_recording(self, gamma_upcoming, tmp_path):
        """Paper and replay run the same decision code: feeding the recording back reproduces it."""
        core, _recorder = self._run(gamma_upcoming, tmp_path, "paper")
        assert type(core.port).__name__ == "PaperExecution"
        assert core.oms.orders, "the live session quoted"
        recorded = list(read_events(tmp_path / "raw"))
        replayed = SimCore(
            parse_settings(
                {
                    "mode": "paper",
                    "strategies": {"passive_quotes": True, "refresh_min_age_ms": 60000},
                }
            )
        )
        for env in recorded:
            replayed.schedule_envelope(env, live=True)
            replayed.run_until(env.received_utc_ms)
        assert set(replayed.oms.orders) == set(core.oms.orders)
        assert [(o.intent.price, o.intent.size) for o in replayed.oms.orders.values()] == [
            (o.intent.price, o.intent.size) for o in core.oms.orders.values()
        ]

    def test_shutdown_is_honest_about_open_state(self, gamma_upcoming, tmp_path, monkeypatch):
        import bone_reaper.live_sim as live

        event = _live_event(gamma_upcoming)
        _fake_stream_factory.condition = event.markets[0].condition_id
        real_recorder = live.Recorder

        def recorder_with_fakes(settings, instrument, out, *, transport):
            return real_recorder(
                settings,
                instrument,
                out,
                transport=FakeLiveTransport(event),
                enable_twap=False,
                market_stream=_fake_stream_factory,
            )

        monkeypatch.setattr(live, "Recorder", recorder_with_fakes)
        monkeypatch.setattr(live, "ReadOnlyTransport", lambda: FakeLiveTransport(event))
        settings = parse_settings({"mode": "paper", "strategies": {"passive_quotes": True}})
        result = asyncio.run(run_live_sim(settings, tmp_path, 1.5))
        assert result["mode"] == "paper" and "clean" in result["shutdown"]
        assert (tmp_path / "metrics.json").exists()
        doc = json.loads((tmp_path / "metrics.json").read_text())
        assert doc["shutdown"]["freeze_reasons"] == [] or not doc["shutdown"]["clean"]

    def test_live_sim_refuses_read_only(self, tmp_path):
        with pytest.raises(ValueError):
            asyncio.run(run_live_sim(parse_settings({}), tmp_path, 0.01))


class TestCliCommands:
    def test_coverage_and_health(self, tmp_path, capsys):
        assert main(["coverage", "--input", str(FIXTURE)]) == 0
        assert '"full_lifespans": 3' in capsys.readouterr().out
        assert main(["health", "--input", str(tmp_path)]) == 0
        assert "no_recorder_data" in capsys.readouterr().out

    def test_health_reads_recorder_state(self, tmp_path, capsys):
        (tmp_path / "health.json").write_text(json.dumps({"run_id": "x", "gaps": 2}))
        main(["health", "--input", str(tmp_path)])
        out = capsys.readouterr().out
        assert '"run_id": "x"' in out and '"status": "recorded_state"' in out

    def test_paper_cli_rejects_wrong_mode(self, capsys):
        assert main(["paper", "--config", str(READ_ONLY)]) == 2
        assert main(["shadow", "--config", str(READ_ONLY)]) == 2

    def test_no_silent_mode_change_flag_exists(self):
        from bone_reaper.cli import build_parser

        text = build_parser().format_help()
        assert "--live" not in text and "--mode" not in text

    def test_fixture_is_not_modified_by_reading(self, tmp_path):
        before = (FIXTURE / "manifest.jsonl").read_bytes()
        copy = tmp_path / "copy"
        shutil.copytree(FIXTURE, copy)
        coverage_report(copy)
        assert (copy / "manifest.jsonl").read_bytes() == before
        assert len(load_manifest(copy)) > 0
        assert canonical_json({"a": 1})


def test_parallel_compare_is_identical_to_serial(tmp_path, paper):
    """Workers change the wall-clock time and nothing else."""
    run_compare(FIXTURE, paper, tmp_path / "serial", 7, workers=1)
    run_compare(FIXTURE, paper, tmp_path / "parallel", 7, workers=2)
    serial = json.loads((tmp_path / "serial" / "compare.json").read_text())
    parallel = json.loads((tmp_path / "parallel" / "compare.json").read_text())
    assert serial["rows"] == parallel["rows"]


def test_streaming_reader_matches_a_full_sort():
    streamed = list(read_events(FIXTURE))
    keys = [(e.received_utc_ms, e.segment_id, e.seq, e.event_id) for e in streamed]
    assert keys == sorted(keys) and len(streamed) > 1000
    only_control = list(read_events(FIXTURE, verify=False, sources=["control"]))
    assert only_control and all(e.source == "control" for e in only_control)


def test_loss_stop_ends_the_capped_run_and_research_variant_is_labelled(paper):
    """The stop works; the research variant lifts only the stop and keeps exposure limits."""
    from bone_reaper.research.experiments import strategy_variants, without_loss_stop

    variants = strategy_variants(paper)
    capped = variants["passive_maker"]
    uncapped = variants["passive_maker_no_loss_stop"]
    assert capped.risk.max_daily_realized_loss_usd == paper.risk.max_daily_realized_loss_usd
    assert uncapped.risk.max_daily_realized_loss_usd > capped.risk.max_daily_realized_loss_usd
    for field in ("max_order_notional_usd", "max_unpaired_notional_usd", "max_open_orders"):
        assert getattr(uncapped.risk, field) == getattr(capped.risk, field)
    assert uncapped.risk.max_total_gross_notional_usd == capped.risk.max_total_gross_notional_usd
    assert without_loss_stop(paper).mode is paper.mode


def test_closed_market_positions_keep_their_last_mark(events, paper):
    """An emptied book must not value a held position at zero and fake a drawdown."""
    core = replay_events(events, paper, seed=7)
    metrics = compute_metrics(core)
    if metrics["fills"]["count"]:
        assert metrics["equity"]["max_drawdown"] < paper.paper.starting_cash_usd / 2


def test_per_market_distribution_is_reported(events, paper):
    metrics = compute_metrics(replay_events(events, paper, seed=7))
    spread = metrics["per_traded_market"]
    assert spread["count"] == metrics["markets_traded"]
    if spread["count"] > 1:
        assert spread["mean_95ci_low"] <= spread["mean"] <= spread["mean_95ci_high"]
