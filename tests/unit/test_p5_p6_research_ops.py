"""P5 and P6: probability research, alerts, the Postgres store, and report outputs."""

from __future__ import annotations

import json
import math
import multiprocessing
import random
from decimal import Decimal as D

import pytest

from bone_reaper.research.experiments import coverage_grade, strategy_variants
from bone_reaper.research.probability import (
    CausalityError,
    FeatureRow,
    LogisticModel,
    PricePoint,
    assert_no_leakage,
    baseline_probability,
    brier_score,
    build_features,
    chronological_market_split,
    evaluate,
    expected_calibration_error,
    fit_logistic,
    load_model,
    log_loss,
    normal_cdf,
    realized_sigma_per_sqrt_second,
    reliability_svg,
    reliability_table,
    save_model,
)
from bone_reaper.settings import parse_settings
from bone_reaper.telemetry.alerts import HealthInputs, Severity, evaluate_alerts, prometheus_text
from bone_reaper.types import FillRecord, FillStatus, LedgerEntry, LiquidityRole, Outcome, Side
from helpers import make_intent, make_spec


def history(n=30, start=0, step=10_000, drift=1.0):
    return [PricePoint(start + i * step, 75_000 + i * drift) for i in range(n)]


def make_row(slug, end, z, label, decision=None, as_of=None) -> FeatureRow:
    decision = end - 60_000 if decision is None else decision
    return FeatureRow(
        market_slug=slug,
        market_end_ms=end,
        decision_ms=decision,
        as_of_ms=decision if as_of is None else as_of,
        seconds_left=60.0,
        z_score=z,
        distance_bps=z * 5,
        momentum_bps=0.0,
        tau_fraction=0.2,
        label_up=label,
        source="test",
    )


class TestFeatures:
    def test_future_observation_is_refused(self):
        with pytest.raises(CausalityError):
            build_features(
                market_slug="m",
                market_end_ms=1_000_000,
                window_seconds=300,
                decision_ms=100_000,
                strike=75_000,
                history=history(30, 0, 10_000),
                label_up=1,
                source="t",
            )

    def test_decision_at_or_after_close_refused(self):
        with pytest.raises(CausalityError):
            build_features(
                market_slug="m",
                market_end_ms=100,
                window_seconds=300,
                decision_ms=100,
                strike=1,
                history=[],
                label_up=None,
                source="t",
            )

    def test_row_is_causal_and_directional(self):
        points = history(30)
        row = build_features(
            market_slug="m",
            market_end_ms=points[-1].ts_ms + 60_000,
            window_seconds=300,
            decision_ms=points[-1].ts_ms,
            strike=75_000,
            history=points,
            label_up=1,
            source="t",
        )
        assert row.as_of_ms <= row.decision_ms < row.market_end_ms
        assert row.distance_bps > 0 and row.z_score > 0
        assert baseline_probability(row) > 0.5
        assert row.tau_fraction == 0.2

    def test_missing_data_returns_none_not_a_guess(self):
        assert realized_sigma_per_sqrt_second(history(2)) is None
        row = build_features(
            market_slug="m",
            market_end_ms=500_000,
            window_seconds=300,
            decision_ms=100_000,
            strike=75_000,
            history=history(2),
            label_up=1,
            source="t",
        )
        assert row is None

    def test_normal_cdf_and_sigma(self):
        assert normal_cdf(0) == pytest.approx(0.5)
        assert normal_cdf(1.96) == pytest.approx(0.975, abs=1e-3)
        flat = [PricePoint(i * 1000, 100.0) for i in range(5)]
        assert realized_sigma_per_sqrt_second(flat) == pytest.approx(1e-6)


class TestSplits:
    def rows(self, count=30):
        return [make_row(f"m{i:03d}", 1_000_000 * (i + 1), 0.1, i % 2) for i in range(count)]

    def test_split_is_chronological_by_market_with_embargo(self):
        rows = self.rows()
        split = chronological_market_split(rows, embargo_markets=2)
        assert_no_leakage(split, rows)
        assert len(split.train) == 18
        assert len(split.validation) == 4 and len(split.test) == 4
        all_used = set(split.train) | set(split.validation) | set(split.test)
        assert len(all_used) == 26, "embargoed markets are dropped from every partition"

    def test_many_rows_per_market_never_cross_partitions(self):
        rows = [
            make_row(f"m{i:02d}", 1_000_000 * (i + 1), 0.1, 1, decision=1_000_000 * (i + 1) - d)
            for i in range(20)
            for d in (60_000, 120_000, 180_000)
        ]
        split = chronological_market_split(rows)
        assert_no_leakage(split, rows)

    def test_leak_detection_catches_each_failure(self):
        rows = self.rows(6)
        from bone_reaper.research.probability import MarketSplit

        with pytest.raises(CausalityError):
            assert_no_leakage(MarketSplit(("m000",), ("m000",), ()), rows)
        with pytest.raises(CausalityError):
            assert_no_leakage(MarketSplit(("m003",), ("m001",), ()), rows)
        future = [make_row("m000", 1_000_000, 0.1, 1, decision=100, as_of=500)]
        with pytest.raises(CausalityError):
            assert_no_leakage(MarketSplit(("m000",), (), ()), future)


class TestModels:
    def synthetic(self, seed=5, count=600):
        rng = random.Random(seed)
        rows = []
        for i in range(count):
            z = rng.gauss(0, 1.5)
            rows.append(
                make_row(f"m{i:04d}", 1_000_000 * (i + 1), z, int(rng.random() < normal_cdf(z)))
            )
        return rows

    def test_fit_is_deterministic_and_improves_on_a_coin_flip(self):
        rows = self.synthetic()
        first = fit_logistic(rows)
        assert first == fit_logistic(rows)
        predictions = [(first.predict(r), r.label_up) for r in rows]
        assert brier_score(predictions) < 0.25
        assert log_loss(predictions) < math.log(2)

    def test_baseline_is_calibrated_on_its_own_generating_process(self):
        rows = self.synthetic(count=2000)
        pairs = [(baseline_probability(r), r.label_up) for r in rows]
        assert expected_calibration_error(pairs) < 0.05

    def test_model_roundtrip_and_hash(self, tmp_path):
        model = fit_logistic(self.synthetic(count=100), iterations=20)
        path = tmp_path / "m.json"
        digest = save_model(model, path)
        loaded = load_model(path)
        assert loaded == model and loaded.model_hash == digest
        assert isinstance(LogisticModel.from_json(path.read_text()), LogisticModel)

    def test_fit_requires_labels(self):
        with pytest.raises(ValueError):
            fit_logistic([make_row("m", 1_000_000, 0.1, None)])

    def test_metrics(self):
        assert brier_score([(1.0, 1), (0.0, 0)]) == 0
        assert brier_score([(0.5, 1), (0.5, 0)]) == 0.25
        assert log_loss([(0.5, 1)]) == pytest.approx(math.log(2))
        assert log_loss([(0.0, 1)]) < 20, "clipped, so never infinite"
        table = reliability_table([(0.05, 0), (0.95, 1), (0.99, 1)])
        assert [row["count"] for row in table] == [1, 2]
        assert table[1]["observed_up_rate"] == 1

    def test_evaluate_reports_and_handles_empty(self):
        rows = self.synthetic(count=50)
        report = evaluate(rows, baseline_probability, "baseline")
        assert report["rows"] == 50 and report["markets"] == 50 and "ece" in report
        assert evaluate([], baseline_probability, "baseline")["rows"] == 0

    def test_reliability_plot_is_valid_svg_and_escapes(self):
        svg = reliability_svg(reliability_table([(0.2, 0), (0.8, 1)]), "a<b&c")
        assert svg.startswith("<svg") and "a&lt;b&amp;c" in svg and "<circle" in svg


class TestAlerts:
    def healthy(self, **changes):
        fields = {"now_utc_ms": 1, "book_age_ms": 10}
        fields.update(changes)
        return HealthInputs(**fields)

    def test_healthy_state_has_no_alerts(self):
        assert evaluate_alerts(self.healthy()) == []

    @pytest.mark.parametrize(
        ("changes", "code"),
        [
            ({"rollover_failures": 1}, "rollover_failure"),
            ({"book_age_ms": 5000}, "stale_feed"),
            ({"book_age_ms": None}, "stale_feed"),
            ({"reference_required": True}, "stale_reference"),
            ({"oracle_mismatch": True}, "oracle_mismatch"),
            ({"user_stream_silence_ms": 9999}, "account_stream_silence"),
            ({"unknown_orders": 1}, "unknown_orders"),
            ({"unpaired_notional": D(21)}, "oversized_unpaired"),
            ({"realized_loss_today": D(10)}, "daily_loss_stop"),
            ({"ledger_errors": 1}, "ledger_discrepancy"),
            ({"reconciliation_diff": D("0.02")}, "ledger_discrepancy"),
            ({"disk_write_failed": True}, "disk_write_error"),
            ({"geoblock_changed": True}, "geoblock_change"),
            ({"clock_offset_ms": 5000}, "clock_skew"),
            ({"queue_depth": 1000}, "queue_backlog"),
            ({"rate_limited": True}, "rate_limited"),
            ({"data_gaps": 2}, "data_gaps"),
            ({"kill_switch_active": True}, "kill_switch_active"),
        ],
    )
    def test_each_failure_raises_its_alert_deterministically(self, changes, code):
        first = evaluate_alerts(self.healthy(**changes))
        assert code in {a.code for a in first}
        assert first == evaluate_alerts(self.healthy(**changes))

    def test_money_critical_alerts_are_critical(self):
        alerts = evaluate_alerts(self.healthy(unknown_orders=1, ledger_errors=1))
        assert all(a.severity is Severity.CRITICAL for a in alerts)

    def test_prometheus_export(self):
        text = prometheus_text(self.healthy(unknown_orders=2, kill_switch_active=True))
        assert "bone_reaper_orders_unknown 2" in text
        assert "bone_reaper_kill_switch_active 1" in text
        assert "bone_reaper_reference_age_ms NaN" in text


class TestStrategyVariants:
    def test_variants_keep_their_stated_policies(self):
        variants = strategy_variants(parse_settings({"mode": "paper"}))
        assert not variants["scanner_only"].strategies.passive_quotes
        assert variants["passive_maker"].strategies.passive_quotes
        assert not variants["passive_maker"].strategies.allow_negative_edge_risk_hedge
        hedged = variants["passive_constrained_hedge"].strategies
        assert hedged.allow_negative_edge_risk_hedge and hedged.max_hedge_loss_usd == D(2)

    def test_too_few_markets_cannot_grade_as_conclusive(self):
        metrics = {"markets_unresolved": [], "data": {"market_gaps": 0}}
        grade = coverage_grade([make_spec(i) for i in range(5)], metrics, 1000)
        assert grade.startswith("B-")
        assert coverage_grade([], metrics, 0).startswith("F")
        gappy = {"markets_unresolved": [], "data": {"market_gaps": 3}}
        assert coverage_grade([make_spec(0)], gappy, 10).startswith("C")


@pytest.fixture(scope="module")
def postgres_dsn(tmp_path_factory):
    pgserver = pytest.importorskip("pgserver")
    server = pgserver.get_server(tmp_path_factory.mktemp("pg"))
    yield server.get_uri()
    server.cleanup()


def _second_process_claims(dsn, wallet, queue):
    from bone_reaper.storage.postgres import PostgresStore

    store = PostgresStore(dsn, wallet)
    queue.put(store.try_acquire_writer())
    store.close()


def _fill(trade="t1") -> FillRecord:
    return FillRecord(
        trade_id=trade,
        leg_key="leg",
        intent_id=None,
        exchange_order_id=None,
        market_id="m0",
        asset_id="9" * 77,
        outcome=Outcome.YES,
        side=Side.BUY,
        role=LiquidityRole.MAKER,
        price=D("0.40"),
        gross_shares=D(10),
        net_shares=D(10),
        fee_cash=D(0),
        fee_shares=D(0),
        cash_delta=D(-4),
        status=FillStatus.MATCHED,
        simulated=True,
        ts_utc_ms=1,
    )


def _entries(cause="fill:t1#leg"):
    return [
        LedgerEntry(f"{cause}/0", cause, "cash:available", "pUSD", D(0), D(4), 1),
        LedgerEntry(f"{cause}/1", cause, "external:venue", "pUSD", D(4), D(0), 1),
    ]


class TestPostgresStore:
    def store(self, dsn, wallet):
        from bone_reaper.storage.postgres import PostgresStore

        store = PostgresStore(dsn, wallet)
        store.migrate()
        store.require_writer()
        return store

    def test_two_processes_cannot_both_own_a_wallet(self, postgres_dsn):
        from bone_reaper.storage.postgres import PostgresStore, WalletLockedError

        first = self.store(postgres_dsn, "wallet-lock")
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        process = context.Process(
            target=_second_process_claims, args=(postgres_dsn, "wallet-lock", queue)
        )
        process.start()
        claimed = queue.get(timeout=60)
        process.join(30)
        assert claimed is False, "a second process must not get the writer lock"
        second = PostgresStore(postgres_dsn, "wallet-lock")
        with pytest.raises(WalletLockedError):
            second.require_writer()
        other_wallet = PostgresStore(postgres_dsn, "wallet-other")
        assert other_wallet.try_acquire_writer(), "the lock is per wallet"
        other_wallet.close()
        first.close()
        assert second.try_acquire_writer(), "a dead owner releases automatically"
        second.close()

    def test_writes_require_ownership(self, postgres_dsn):
        from bone_reaper.storage.postgres import PostgresStore, WalletLockedError

        owner = self.store(postgres_dsn, "wallet-w")
        intruder = PostgresStore(postgres_dsn, "wallet-w")
        with pytest.raises(WalletLockedError):
            intruder.append({"kind": "x", "ts_utc_ms": 1})
        owner.close()
        intruder.close()

    def test_intent_and_reservation_commit_atomically(self, postgres_dsn):
        from bone_reaper.storage.postgres import StoreError

        store = self.store(postgres_dsn, "wallet-a")
        spec = make_spec()
        before = {t: store.count(t) for t in ("order_intents", "cash_reservations", "journal")}
        with pytest.raises(StoreError):
            store.record_intent(make_intent(spec, "fail"), D(4), 1, fail_after=True)
        assert {t: store.count(t) for t in before} == before, "nothing partial is left behind"
        store.record_intent(make_intent(spec, "ok"), D(4), 1)
        assert store.count("order_intents") == before["order_intents"] + 1
        assert store.count("cash_reservations") == before["cash_reservations"] + 1
        store.close()

    def test_duplicate_intent_id_refused_by_unique_key(self, postgres_dsn):
        from bone_reaper.storage.postgres import StoreError

        store = self.store(postgres_dsn, "wallet-dup")
        intent = make_intent(make_spec(), "same-intent")
        store.record_intent(intent, D(4), 1)
        with pytest.raises(StoreError):
            store.record_intent(intent, D(4), 1)
        store.close()

    def test_fill_and_ledger_commit_exactly_once(self, postgres_dsn):
        store = self.store(postgres_dsn, "wallet-f")
        entries_before = store.count("ledger_entries")
        assert store.record_fill(_fill("once"), _entries("fill:once#leg"))
        assert not store.record_fill(_fill("once"), _entries("fill:once#leg"))
        assert store.count("ledger_entries") == entries_before + 2
        store.close()

    def test_precision_is_exact_and_asset_ids_are_text(self, postgres_dsn):
        store = self.store(postgres_dsn, "wallet-p")
        assert store.record_fill(_fill("prec"), _entries("fill:prec#leg"))
        with store._conn.cursor() as cur:
            cur.execute("SELECT asset_id, cash_delta FROM fills WHERE trade_id = 'prec'")
            asset, cash = cur.fetchone()
        assert asset == "9" * 77 and cash == D("-4")
        store.close()

    def test_acts_as_oms_journal_and_survives_restart(self, postgres_dsn):
        from bone_reaper.storage.postgres import PostgresStore

        store = self.store(postgres_dsn, "wallet-j")
        store.append({"kind": "state", "ts_utc_ms": 5, "intent_id": "a"})
        records = store.read_all()
        assert records[-1]["intent_id"] == "a"
        store.close()
        reopened = PostgresStore(postgres_dsn, "wallet-j")
        assert reopened.read_all()[-1]["intent_id"] == "a"
        reopened.close()

    def test_database_down_raises_store_error_not_silence(self):
        from bone_reaper.storage.postgres import PostgresStore, StoreError

        with pytest.raises(StoreError):
            PostgresStore("postgresql://nobody@127.0.0.1:1/none?connect_timeout=1", "w")


def test_coverage_report_on_recorded_fixture(tmp_path):
    from bone_reaper.storage.raw_events import RawEventWriter
    from bone_reaper.telemetry.health import coverage_report
    from helpers import START, EnvelopeFactory, book_payload, change, price_change_payload

    factory = EnvelopeFactory()
    spec = make_spec(0)
    y = spec.yes_asset_id
    writer = RawEventWriter(tmp_path, run_id="r")
    for env in (
        factory.spec(spec, START - 1000),
        factory.make(book_payload(y, [("0.40", "10")], [("0.42", "5")], START), START - 500),
        factory.make(
            price_change_payload(spec.end_utc_ms, change(y, "BUY", "0.39", "4", "0.40", "0.42")),
            spec.end_utc_ms,
            asset_id="",
        ),
        factory.make(
            book_payload(y, [("0.40", "10"), ("0.39", "4")], [("0.42", "5")], spec.end_utc_ms),
            spec.end_utc_ms + 1,
        ),
        factory.resolution(spec, spec.end_utc_ms + 5000, True),
    ):
        writer.append(env)
    writer.flush()
    report = coverage_report(tmp_path)
    assert report["full_lifespans"] == 1 and report["checksum_problems"] == []
    assert report["markets"][0]["resolved"] and report["markets"][0]["price_to_beat"] == "75000"
    assert report["stream_snapshot_validation"]["exact_depth_match"] == 1
    assert json.dumps(report)


def test_health_command_surfaces_alerts(tmp_path):
    from bone_reaper.settings import Settings
    from bone_reaper.telemetry.health import health_report

    (tmp_path / "health.json").write_text(
        json.dumps(
            {
                "ts_utc_ms": 5,
                "gaps": 1,
                "clock_offset_ms": 9000,
                "rollover_failures": ["x"],
                "books": {"a": {"age_ms": 50}},
            }
        )
    )
    codes = {a["code"] for a in health_report(Settings(), tmp_path)["alerts"]}
    assert {"clock_skew", "rollover_failure", "data_gaps"} <= codes
    assert "stale_feed" not in codes
