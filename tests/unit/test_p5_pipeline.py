"""P5: research pipeline, model integration through quotes, and the audit decision rules."""

from __future__ import annotations

import json
from decimal import Decimal as D
from pathlib import Path

import pytest
from polymarket import Event

from bone_reaper.research.experiments import load_all
from bone_reaper.research.model_research import (
    MIN_MARKETS_FOR_ECONOMIC_DECISION,
    HistoricalMarket,
    _parse_market,
    audit_decision,
    build_rows,
    forecast_verdict,
    market_probability_lookup,
    reference_probability,
    run_forecast_research,
    run_model_audit,
)
from bone_reaper.research.probability import (
    PricePoint,
    assert_no_leakage,
    baseline_probability,
    chronological_market_split,
    fit_logistic,
    save_model,
)
from bone_reaper.research.replay import SimCore, replay_events
from bone_reaper.settings import load_settings, parse_settings
from bone_reaper.strategy.fair_value import model_fair
from helpers import book_state, make_spec

ROOT = Path(__file__).parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "replay"
PAPER = ROOT / "config" / "paper.yaml"
BASE = 1_700_000_100_000
WINDOW = 300_000


def synthetic_history(markets=200):
    """A random walk with official-style labels from its own closes, in strict time order."""
    import random

    rng = random.Random(11)
    closes = []
    price = 75_000.0
    for minute in range(-40, markets * 5 + 5):
        price += rng.gauss(0, 25)
        closes.append(PricePoint(BASE + minute * 60_000 - 1, price))
    lookup = {p.ts_ms: p.price for p in closes}
    items = []
    for i in range(markets):
        start = BASE + i * WINDOW
        up = lookup[start + WINDOW - 1] >= lookup[start - 1]
        items.append(
            HistoricalMarket(f"m{i:04d}", start, start + WINDOW, "75000", "75100", up, "1")
        )
    return items, closes


class TestParsing:
    def test_real_closed_event_parses(self, gamma_closed):
        parsed = _parse_market(Event.model_validate(gamma_closed))
        assert parsed is not None and parsed.end_ms - parsed.start_ms == WINDOW
        assert parsed.price_to_beat is not None and isinstance(parsed.up_won, bool)
        assert parsed.yes_asset_id == json.loads(gamma_closed["markets"][0]["clobTokenIds"])[0]

    def test_unfinished_market_is_skipped(self, gamma_upcoming):
        assert _parse_market(Event.model_validate(gamma_upcoming)) is None


class TestBuildRows:
    def test_every_row_is_causal_and_labelled_as_proxy(self):
        markets, closes = synthetic_history(30)
        rows, skipped = build_rows(markets, closes)
        assert skipped == 0 and len(rows) == 30 * 4
        for row in rows:
            assert row.as_of_ms <= row.decision_ms < row.market_end_ms
            assert row.source.startswith("binance_spot_1m_proxy")
        assert_no_leakage(chronological_market_split(rows), rows)

    def test_changing_the_future_cannot_change_past_features(self):
        """The core leak test: rewrite everything after a decision and compare the row."""
        markets, closes = synthetic_history(10)
        rows, _ = build_rows(markets, closes)
        target = rows[17]
        changed = [
            p if p.ts_ms <= target.decision_ms else PricePoint(p.ts_ms, p.price * 2) for p in closes
        ]
        new_rows, _ = build_rows(markets, changed)
        same = next(
            r
            for r in new_rows
            if r.market_slug == target.market_slug and r.decision_ms == target.decision_ms
        )
        assert same.vector() == target.vector() and same.as_of_ms == target.as_of_ms

    def test_market_without_history_is_counted_not_guessed(self):
        markets, _ = synthetic_history(3)
        rows, skipped = build_rows(markets, [])
        assert rows == [] and skipped == 12

    def test_market_benchmark_lookup_is_causal(self):
        markets, closes = synthetic_history(3)
        rows, _ = build_rows(markets, closes)
        row = rows[0]
        lookup = market_probability_lookup(
            {
                row.market_slug: [
                    PricePoint(row.decision_ms - 1000, 0.6),
                    PricePoint(row.decision_ms + 1000, 0.9),
                ]
            }
        )
        assert lookup(row) == 0.6
        assert market_probability_lookup({})(row) is None


class TestForecastResearch:
    def test_report_has_partitions_scores_and_verdict(self, tmp_path):
        markets, closes = synthetic_history(200)
        prices = {
            m.slug: [PricePoint(m.start_ms + s * 1000, 0.5) for s in (60, 120, 180, 240)]
            for m in markets[-40:]
        }
        report = run_forecast_research(markets, closes, prices, tmp_path)
        assert report["split"]["train"] == 120 and report["embargo_markets"] == 2
        assert report["rows_skipped_missing_data"] == 0
        for partition in ("train", "validation", "test"):
            assert report["evaluation"][partition]["baseline"]["brier"] < 0.25
        assert "profitable" in report["caveat"]
        assert report["benchmark_vs_market_on_test"]["rows"] > 0
        assert (tmp_path / "reliability_baseline.svg").read_text().startswith("<svg")
        assert (tmp_path / "logistic.json").exists()
        assert json.loads((tmp_path / "forecast_report.json").read_text())["model_hash"]

    def _sections(self, baseline, logistic):
        section = {
            "baseline": {"brier": baseline, "log_loss": baseline},
            "logistic": {"brier": logistic, "log_loss": logistic},
        }
        return {"validation": section, "test": section}

    def test_verdict_prefers_baseline_unless_beaten_out_of_sample(self):
        none = {"rows": 0, "market_price": {}, "logistic": {}}
        worse = forecast_verdict(self._sections(0.20, 0.21), none, 500)
        assert worse["preferred_forecast_model"] == "baseline"
        better = forecast_verdict(self._sections(0.21, 0.20), none, 500)
        assert better["preferred_forecast_model"] == "logistic"

    def test_verdict_refuses_on_too_few_test_markets(self):
        none = {"rows": 0, "market_price": {}, "logistic": {}}
        thin = forecast_verdict(self._sections(0.21, 0.20), none, 10)
        assert thin["preferred_forecast_model"] == "baseline" and not thin["enough_test_markets"]


class TestAuditDecision:
    def _rows(self, midpoint, model):
        return [
            {"variant": "midpoint_baseline", "scenario": "pessimistic", "realized_pnl": midpoint},
            {"variant": "midpoint_baseline", "scenario": "central", "realized_pnl": midpoint},
            {"variant": "m", "scenario": "pessimistic", "realized_pnl": model},
            {"variant": "m", "scenario": "central", "realized_pnl": model},
        ]

    def test_thin_data_is_inconclusive_even_if_model_looks_great(self):
        decision = audit_decision(self._rows("0", "100"), 5)
        assert decision["status"] == "INCONCLUSIVE"
        assert decision["model_enabled_by_default"] is False

    def test_worse_model_is_no_go(self):
        decision = audit_decision(self._rows("1", "0"), MIN_MARKETS_FOR_ECONOMIC_DECISION)
        assert decision["status"] == "NO-GO"

    def test_better_model_is_only_a_candidate_and_stays_disabled(self):
        decision = audit_decision(self._rows("0", "1"), MIN_MARKETS_FOR_ECONOMIC_DECISION)
        assert decision["status"] == "CANDIDATE"
        assert decision["model_enabled_by_default"] is False

    def test_must_win_in_every_scenario(self):
        rows = self._rows("0", "1")
        rows[3]["realized_pnl"] = "-1"
        decision = audit_decision(rows, MIN_MARKETS_FOR_ECONOMIC_DECISION)
        assert decision["status"] == "NO-GO"


class TestModelIntegration:
    @pytest.fixture
    def model_path(self, tmp_path):
        markets, closes = synthetic_history(60)
        rows, _ = build_rows(markets, closes)
        path = tmp_path / "logistic.json"
        save_model(fit_logistic(rows, iterations=30), path)
        return path

    def _settings(self, on, path):
        strategies = {
            "passive_quotes": True,
            "scan_pair": False,
            "probability_model": on,
        }
        if on:
            strategies["model_path"] = str(path)
        return parse_settings({"mode": "paper", "strategies": strategies})

    def test_quotes_are_unchanged_without_the_flag(self, model_path):
        """A model attached to a core with the flag off must change nothing at all."""
        events = load_all(FIXTURE)
        plain = replay_events(events, self._settings(False, model_path), seed=7)

        def factory(core):
            return reference_probability(core, baseline_probability)

        with_model = replay_events(
            events, self._settings(False, model_path), seed=7, probability_factory=factory
        )
        assert set(plain.orchestrator.fair_sources) == {"book_midpoint"}
        assert set(with_model.orchestrator.fair_sources) == {"book_midpoint"}
        prices = lambda c: [(o.intent.price, o.intent.size) for o in c.oms.orders.values()]  # noqa: E731
        assert prices(plain) == prices(with_model)

    def test_flag_on_uses_the_model_and_records_it(self, model_path):
        events = load_all(FIXTURE)
        settings = self._settings(True, model_path)

        def factory(core):
            return reference_probability(core, baseline_probability)

        core = replay_events(events, settings, seed=7, probability_factory=factory)
        assert core.orchestrator.fair_sources.get("model", 0) > 0

    def test_missing_twap_falls_back_and_cannot_create_a_bad_quote(self, model_path):
        events = [e for e in load_all(FIXTURE) if e.source != "poly_twap"]
        settings = self._settings(False, model_path)
        core = SimCore(settings, seed=7)
        probability = reference_probability(core, baseline_probability)
        for env in events:
            core.schedule_envelope(env)
        core.run_until(next(iter(events)).received_utc_ms + 20_000)
        assert probability() is None
        spec = make_spec()
        yes = book_state(spec.yes_asset_id, (("0.49", "1"),), (("0.51", "1"),))
        no = book_state(spec.no_asset_id, (("0.49", "1"),), (("0.51", "1"),))
        assert model_fair(yes, no, probability, True, "h").source == "book_midpoint"

    def test_risk_gate_requires_fresh_reference_when_model_is_on(self, model_path):
        events = [e for e in load_all(FIXTURE) if e.source != "poly_twap"]
        settings = self._settings(True, model_path)
        core = replay_events(events, settings, seed=7)
        reasons = core.risk.rejections_by_reason
        assert reasons.get("reference_unavailable", 0) > 0, "no TWAP means no new quotes"
        assert core.exchange.stats["arrivals"] == 0

    def test_audit_runs_and_stays_inconclusive_on_three_markets(self, tmp_path, model_path):
        report = run_model_audit(FIXTURE, load_settings(PAPER), model_path, tmp_path, 7)
        assert report["simulated"] is True
        assert report["decision"]["status"] == "INCONCLUSIVE"
        variants = {r["variant"] for r in report["rows"]}
        assert {
            "midpoint_baseline",
            "random_walk_model",
            "logistic_model",
            "logistic_model_delayed_twap_5s",
        } == variants
        assert all(r["ledger_errors"] == 0 for r in report["rows"])
        assert (tmp_path / "model_audit_manifest.json").exists()
        assert D(report["rows"][0]["realized_pnl"]) is not None
