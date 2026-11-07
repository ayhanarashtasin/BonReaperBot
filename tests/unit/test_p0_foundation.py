"""P0: configuration, domain models, clocks, geoblock, adapter gating, redaction."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

from bone_reaper import logging as bone_logging
from bone_reaper.cli import main
from bone_reaper.connectors.geoblock import GeoStatus, check_geoblock
from bone_reaper.execution.factory import NoExecutionInReadOnlyError, build_execution
from bone_reaper.execution.real import LIVE_DISABLED, LiveDisabledError, build_real_execution
from bone_reaper.settings import ConfigError, Settings, load_settings, parse_settings
from bone_reaper.time import FakeClock, ReorderDetector, SkewMonitor, stamp
from bone_reaper.types import (
    BinanceReference,
    BookState,
    BookValidity,
    CurrencyAmount,
    EventEnvelope,
    FeeBreakdown,
    FillRecord,
    FillStatus,
    InventorySnapshot,
    LiquidityRole,
    MarketSpec,
    Mode,
    Outcome,
    PriceLevel,
    QuoteIntent,
    ReferenceSource,
    ReferenceState,
    ReplayManifest,
    RiskDecision,
    Side,
    from_jsonable,
    to_jsonable,
)
from helpers import make_intent, make_spec

ROOT = Path(__file__).parents[2]


class TestConfig:
    def test_committed_configs_load(self):
        assert load_settings(ROOT / "config/read_only.yaml").mode is Mode.READ_ONLY
        assert load_settings(ROOT / "config/paper.yaml").mode is Mode.PAPER
        assert load_settings(ROOT / "config/shadow.yaml").mode is Mode.SHADOW
        assert load_settings(ROOT / "config/live.example.yaml").mode is Mode.LIVE

    def test_default_is_read_only(self):
        assert Settings().mode is Mode.READ_ONLY

    @pytest.mark.parametrize(
        "raw",
        [
            {"unknown_key": 1},
            {"risk": {"max_order_notional_usd": 0.5}},  # binary float refused
            {"risk": {"max_order_notional_usd": "-1"}},
            {"risk": {"max_order_notional_usd": "500"}},  # exceeds gross limit
            {"mode": "read_only", "strategies": {"passive_quotes": True}},
            {"mode": "read_only", "live_enabled": True},
            {"mode": "paper", "live_operator_approval": True},
            {"mode": "live"},  # live needs both flags
            {"mode": "live", "live_enabled": True},
            {"mode": "paper", "strategies": {"allow_negative_edge_risk_hedge": True}},
            {"mode": "paper", "strategies": {"probability_model": True}},
            {"strategies": {"min_net_roi": "1.5"}},
            {"execution": {"retry_unknown_order_submit": True}},
        ],
    )
    def test_invalid_configs_rejected(self, raw):
        with pytest.raises(ConfigError):
            parse_settings(raw)

    def test_invalid_config_exits_nonzero(self, tmp_path, capsys):
        bad = tmp_path / "bad.yaml"
        bad.write_text("mode: read_only\nlive_enabled: true\n")
        assert main(["doctor", "--config", str(bad), "--offline"]) == 2

    def test_hash_is_stable_and_sensitive(self):
        first = parse_settings({"mode": "paper"})
        same = parse_settings({"mode": "paper"})
        other = parse_settings({"mode": "paper", "risk": {"max_open_orders": 2}})
        assert first.config_hash() == same.config_hash()
        assert first.config_hash() != other.config_hash()
        assert len(first.config_hash()) == 64


def _fill(**changes) -> FillRecord:
    fields = {
        "trade_id": "t",
        "leg_key": "l",
        "intent_id": "i",
        "exchange_order_id": "o",
        "market_id": "m",
        "asset_id": "9" * 70,
        "outcome": Outcome.YES,
        "side": Side.BUY,
        "role": LiquidityRole.TAKER,
        "price": D("0.46"),
        "gross_shares": D("10"),
        "net_shares": D("9.8"),
        "fee_cash": D(0),
        "fee_shares": D("0.2"),
        "cash_delta": D("-4.6"),
        "status": FillStatus.MATCHED,
        "simulated": True,
        "ts_utc_ms": 1,
    }
    fields.update(changes)
    return FillRecord(**fields)


class TestDomainModels:
    @pytest.mark.parametrize(
        "value",
        [
            make_spec(),
            make_intent(make_spec()),
            _fill(),
            FeeBreakdown(D("0.1"), D(0), D("0.1")),
            BookState(
                "m", "a", (PriceLevel(D("0.4"), D("1")),), (), D("0.01"), 1, 2, BookValidity.READY
            ),
            InventorySnapshot(
                "m", D(1), D(0), D(1), D(1), D(2), D(2), D(1), D(0), D(1), D(1), None
            ),
            RiskDecision("r", False, ("reason",), (("k", "v"),), 5),
            ReferenceState("btcusd", D("75000.123"), 60, ReferenceSource.CHAINLINK_TWAP, "f", 1, 2),
            ReplayManifest(
                "run", (), (("markets", "3"),), "abc", "hash", None, 1, 2, ("fee",), 7, "0.17.0"
            ),
        ],
    )
    def test_roundtrip_through_json(self, value):
        encoded = json.dumps(to_jsonable(value))
        assert from_jsonable(type(value), json.loads(encoded)) == value

    def test_float_is_refused_in_serialisation(self):
        with pytest.raises(TypeError):
            to_jsonable({"price": 0.5})

    def test_large_asset_ids_stay_strings(self):
        spec = make_spec(yes_asset_id="1" * 77, no_asset_id="2" * 77)
        rebuilt = from_jsonable(MarketSpec, json.loads(json.dumps(to_jsonable(spec))))
        assert rebuilt.yes_asset_id == "1" * 77

    def test_market_spec_invariants(self):
        with pytest.raises(ValueError):
            make_spec(end_utc_ms=make_spec().start_utc_ms)
        with pytest.raises(ValueError):
            make_spec(yes_asset_id="same", no_asset_id="same")
        with pytest.raises(ValueError):
            make_spec(tick_size=D(0))

    def test_paired_invariant(self):
        with pytest.raises(ValueError):
            InventorySnapshot("m", D(0), D(0), D(5), D(5), D(3), D(3), D(5), D(2), D(0), D(0))

    def test_intent_invariants(self):
        spec = make_spec()
        with pytest.raises(ValueError):
            make_intent(spec, price="1.00")
        with pytest.raises(ValueError):
            make_intent(spec, size="0")

    def test_currency_amount_does_not_mix_assets(self):
        with pytest.raises(ValueError):
            CurrencyAmount("pUSD", D(1)) + CurrencyAmount("USDC", D(1))

    def test_binance_cannot_be_labelled_chainlink(self):
        with pytest.raises(ValueError):
            BinanceReference("BTCUSDT", ReferenceSource.CHAINLINK_TWAP, D(1), 1, 2)
        with pytest.raises(ValueError):
            ReferenceState("btcusd", D(1), 60, ReferenceSource.BINANCE_SPOT, "f", 1, 2)

    def test_envelope_payload(self):
        env = EventEnvelope("id", 1, "s", "t", None, None, None, 1, 2, b'{"a":1}')
        assert env.payload() == {"a": 1}


class TestClock:
    def test_missing_source_timestamp_is_flagged_estimated(self):
        clock = FakeClock(1000, 5)
        arrival = stamp(clock, None)
        assert arrival.source_ts_ms is None and arrival.source_ts_estimated
        assert arrival.received_utc_ms == 1000 and arrival.received_at_ns == 5

    def test_present_source_timestamp_not_estimated(self):
        assert not stamp(FakeClock(1000), 900).source_ts_estimated

    def test_monotonic_unaffected_by_wall_clock_step(self):
        clock = FakeClock(1000, 0)
        clock.advance_ms(10)
        before = clock.monotonic_ns()
        clock.set_utc_ms(0)
        assert clock.monotonic_ns() == before and clock.utc_ms() == 0

    def test_skew_detection(self):
        monitor = SkewMonitor(max_future_ms=500, max_lag_ms=5000)
        assert not monitor.observe(1000, 1100).unreasonable
        assert monitor.observe(2000, 1000).reason == "source_timestamp_in_future"
        assert monitor.observe(0, 6000).reason == "source_timestamp_too_old"
        assert monitor.unreasonable_count == 2

    def test_reorder_detection(self):
        detector = ReorderDetector()
        assert not detector.observe("a", 100)
        assert detector.observe("a", 90)
        assert not detector.observe("b", 90)
        assert not detector.observe("a", None)
        assert detector.reordered == 1


class TestGeoblock:
    def test_allowed(self):
        result = check_geoblock(
            lambda: (200, {"blocked": False, "ip": "1.2.3.4", "country": "XX", "region": "Y"})
        )
        assert result.status is GeoStatus.ALLOWED and result.may_open_real_positions

    def test_ip_is_not_retained(self):
        result = check_geoblock(lambda: (200, {"blocked": False, "ip": "1.2.3.4"}))
        assert "1.2.3.4" not in repr(result)

    @pytest.mark.parametrize(
        ("fetch", "expected"),
        [
            (lambda: (200, {"blocked": True, "country": "XX"}), GeoStatus.BLOCKED),
            (lambda: (500, {}), GeoStatus.UNKNOWN),
            (lambda: (200, {"country": "XX"}), GeoStatus.UNKNOWN),
            (lambda: (200, {"blocked": "false"}), GeoStatus.UNKNOWN),
            (lambda: (200, "not json object"), GeoStatus.UNKNOWN),
        ],
    )
    def test_fails_closed(self, fetch, expected):
        result = check_geoblock(fetch)
        assert result.status is expected and not result.may_open_real_positions

    def test_exception_is_unknown(self):
        def boom():
            raise OSError("down")

        assert check_geoblock(boom).status is GeoStatus.UNKNOWN


class TestAdapterGate:
    def test_read_only_constructs_no_adapter(self):
        with pytest.raises(NoExecutionInReadOnlyError):
            build_execution(Settings())

    def test_paper_refuses_a_real_adapter(self):
        class Real:
            is_real = True

        settings = parse_settings({"mode": "paper"})
        with pytest.raises(RuntimeError):
            build_execution(settings, paper_factory=Real)

    @pytest.mark.parametrize("status", ["allowed", "blocked", "unknown"])
    def test_malicious_live_config_never_sends(self, status):
        """Even mode=live with both flags and an allowed geoblock stays LIVE_DISABLED."""
        settings = load_settings(ROOT / "config/live.example.yaml")
        geo = check_geoblock(
            {
                "allowed": lambda: (200, {"blocked": False}),
                "blocked": lambda: (200, {"blocked": True}),
                "unknown": lambda: (500, {}),
            }[status]
        )
        with pytest.raises(LiveDisabledError) as info:
            build_execution(settings, geo)
        assert LIVE_DISABLED in str(info.value)
        if status != "allowed":
            assert f"geoblock_{status}" in info.value.reasons

    def test_live_gate_without_geoblock(self):
        with pytest.raises(LiveDisabledError) as info:
            build_real_execution(Settings(), None)
        assert "geoblock_not_checked" in info.value.reasons

    def test_no_module_imports_order_placing_client(self):
        """Only the TWAP facade's spec and public client may be imported from the SDK."""
        offenders = []
        for path in (ROOT / "src/bone_reaper").rglob("*.py"):
            text = path.read_text("utf-8")
            if "AsyncSecureClient(" in text or "SecureClient(" in text or "create_order" in text:
                offenders.append(str(path))
        assert offenders == []

    def test_live_cli_refuses(self, capsys):
        assert main(["live"]) == 1
        assert LIVE_DISABLED in capsys.readouterr().err


class TestCli:
    def test_help_works_offline_in_a_fresh_process(self):
        done = subprocess.run(
            [sys.executable, "-m", "bone_reaper", "--help"], capture_output=True, text=True
        )
        assert done.returncode == 0 and "read_only" in done.stdout

    def test_doctor_prints_mode_hash_and_actions(self, capsys, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert main(["doctor", "--offline"]) == 0
        out = capsys.readouterr().out
        assert '"mode": "read_only"' in out
        assert "config_hash" in out and "order submission" in out
        assert '"live_disabled": true' in out

    def test_reconcile_requires_dry_run(self, capsys):
        assert main(["reconcile"]) == 2
        assert main(["reconcile", "--dry-run"]) == 0

    def test_start_command_wires_dashboard_and_bot(self, capsys, tmp_path, monkeypatch):
        import bone_reaper.dashboard.server as dash_server
        import bone_reaper.live_sim as live_sim

        events: list[str] = []

        class FakeServer:
            server_address = ("127.0.0.1", 8765)

            def serve_forever(self) -> None:
                events.append("served")

            def shutdown(self) -> None:
                events.append("shutdown")

            def server_close(self) -> None:
                events.append("closed")

        async def fake_run(settings, out_dir, seconds):
            events.append(f"bot:{settings.mode.value}")
            return {"mode": settings.mode.value, "output": str(out_dir)}

        def fake_asyncio_run(coro):
            coro.close()
            events.append("bot:paper")
            return {"mode": "paper", "output": str(tmp_path)}

        monkeypatch.setattr(dash_server, "make_server", lambda *a, **kw: FakeServer())
        monkeypatch.setattr(live_sim, "run_live_sim", fake_run)
        monkeypatch.setattr("bone_reaper.cli.asyncio.run", fake_asyncio_run)
        assert main(["start", "bone", "--no-browser", "--output", str(tmp_path)]) == 0
        out = capsys.readouterr().out
        assert "http://127.0.0.1:8765" in out and "bot:paper" in events
        assert "shutdown" in events and "closed" in events
        assert main(["start", "--config", str(ROOT / "config/live.example.yaml")]) == 1


class TestLogRedaction:
    def test_secrets_are_redacted(self):
        secret = "ab" * 32
        assert bone_logging.redact({"private_key": "x", "note": f"key={secret}"}) == {
            "private_key": bone_logging.REDACTED,
            "note": f"key={bone_logging.REDACTED}",
        }
        words = " ".join(["abandon"] * 12)
        assert bone_logging.REDACTED in bone_logging.redact(words)

    def test_formatter_emits_json_fields(self):
        record = logging.LogRecord("x", logging.INFO, "f", 1, "hello", (), None)
        record.fields = {"market_id": "m", "api_secret": "s"}
        doc = json.loads(bone_logging.JsonFormatter().format(record))
        assert doc["market_id"] == "m" and doc["api_secret"] == bone_logging.REDACTED


def test_quote_intent_roundtrip_keeps_decimal_exactly():
    intent = make_intent(make_spec(), price="0.33", size="12.34")
    rebuilt = from_jsonable(QuoteIntent, json.loads(json.dumps(to_jsonable(intent))))
    assert rebuilt.price == D("0.33") and rebuilt.size == D("12.34")
