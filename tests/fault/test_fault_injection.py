"""BR-405 / BR-602: every fault freezes new risk. Asserted through the real orchestrator and OMS."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from bone_reaper.execution.factory import build_execution
from bone_reaper.execution.oms import FileJournal, InMemoryJournal
from bone_reaper.execution.ports import SubmitResult, SubmitStatus
from bone_reaper.research.replay import SimCore
from bone_reaper.risk.kill_switch import KillReason
from bone_reaper.settings import parse_settings
from bone_reaper.types import (
    FillStatus,
    IntentPurpose,
    OrderState,
    OrderType,
    Outcome,
    QuoteIntent,
    Side,
)
from helpers import EnvelopeFactory, book_payload, make_spec, paper_settings, trade_payload

SPEC = make_spec(0)
T0 = SPEC.start_utc_ms + 10_000


def healthy_core(settings=None, journal=None, fault=None) -> tuple[SimCore, EnvelopeFactory]:
    """A running core with fresh books. ``fault`` is injected before the first decision."""
    core = SimCore(settings or paper_settings(), seed=3, journal=journal)
    if fault is not None:
        fault(core)
    factory = EnvelopeFactory()
    core.schedule_envelope(factory.spec(SPEC, T0 - 5000))
    feed_books(core, factory, T0)
    core.run_until(T0)
    # A decision after the books arrive clears the "no data yet" stale state from before them.
    feed_books(core, factory, T0 + 300)
    core.run_until(T0 + 300)
    return core, factory


def feed_books(core: SimCore, factory: EnvelopeFactory, t: int) -> None:
    for asset in (SPEC.yes_asset_id, SPEC.no_asset_id):
        core.schedule_envelope(
            factory.make(
                book_payload(asset, [("0.48", "100")], [("0.52", "100")], t), t, source_ts=t
            )
        )


def advance(core: SimCore, factory: EnvelopeFactory, ms: int, step: int = 300) -> None:
    """Advance time with a live feed so that only the injected fault can stop new risk."""
    start = core.scheduler.now
    for t in range(start + step, start + ms + 1, step):
        feed_books(core, factory, t)
        core.run_until(t)


def new_quotes(core: SimCore) -> int:
    return sum(
        1
        for o in core.oms.orders.values()
        if o.intent.purpose is IntentPurpose.PASSIVE_QUOTE
        and o.state is not OrderState.RISK_REJECTED
    )


def sends(core: SimCore) -> int:
    return len(core.port.submitted)


class TestBaseline:
    def test_healthy_feed_quotes_both_sides(self):
        core, factory = healthy_core()
        advance(core, factory, 1500)
        assert new_quotes(core) == 2
        assert not core.kill.active


class TestEveryFaultFreezesNewRisk:
    def _assert_no_new_risk(self, core, factory, reason=None, ms=3000):
        before = sends(core)
        advance(core, factory, ms)
        assert sends(core) == before, f"sent new orders during fault {reason}"
        if reason is not None:
            assert core.kill.has(reason), core.kill.reasons

    def test_stale_feed(self):
        core, factory = healthy_core()
        advance(core, factory, 1000)
        before = sends(core)
        t = core.scheduler.now
        self._silent_ticks(core, t, (5000, 6500))
        assert core.kill.has(KillReason.STALE_FEED)
        assert sends(core) == before

    @staticmethod
    def _silent_ticks(core, t, deltas):
        for delta in deltas:
            core.run_until(t + delta)
            core._decide(t + delta)

    def test_brief_silence_pauses_but_does_not_kill(self):
        """Trades cause brief book syncs. Those must not turn into a cancel storm."""
        core, factory = healthy_core()
        advance(core, factory, 1000)
        t = core.scheduler.now
        self._silent_ticks(core, t, (1100,))
        assert not core.kill.has(KillReason.STALE_FEED)

    def test_stale_feed_cancels_resting_quotes(self):
        core, factory = healthy_core()
        advance(core, factory, 1000)
        assert any(o.state is OrderState.OPEN for o in core.oms.orders.values())
        t = core.scheduler.now
        self._silent_ticks(core, t, (2000, 3500))
        assert core.kill.has(KillReason.STALE_FEED)
        core.run_until(t + 4500)
        assert not any(o.state is OrderState.OPEN for o in core.oms.orders.values())

    def test_silent_feed_trips_kill_during_replay_without_any_events(self):
        """The heartbeat must catch silence between recorded events."""
        core, factory = healthy_core()
        advance(core, factory, 1000)
        late = core.scheduler.now + 20_000
        feed_books(core, factory, late)
        core.run_until(late - 1)
        assert core.kill.has(KillReason.STALE_FEED)

    def test_unknown_order(self):
        def ack_lost(core):
            core.port.submit = lambda intent: SubmitResult(SubmitStatus.UNKNOWN, reason="ack_lost")

        core, factory = healthy_core(fault=ack_lost)
        advance(core, factory, 600)
        assert core.oms.unknown_orders, "the injected ack loss put orders in UNKNOWN"
        self._assert_no_new_risk(core, factory, KillReason.UNKNOWN_ORDER)

    def test_loss_cap_latches_and_survives_recovery(self):
        core, factory = healthy_core()
        core.kill.engage(KillReason.LOSS_CAP, T0)
        self._assert_no_new_risk(core, factory, KillReason.LOSS_CAP)
        assert core.kill.latched
        core.kill.clear(KillReason.LOSS_CAP, core.scheduler.now)
        assert core.kill.has(KillReason.LOSS_CAP), "only an operator can clear a latched stop"

    def test_reconciliation_mismatch_latches(self):
        core, factory = healthy_core()
        core.exchange.truth.cash += D(5)  # The venue disagrees with our ledger.
        core._reconcile("injected")
        assert not core.reconciliations[-1].clean
        self._assert_no_new_risk(core, factory, KillReason.RECONCILIATION_MISMATCH)
        assert core.oms.risk_frozen

    def test_clock_skew(self):
        def skewed(core):
            core.risk.clock_skew_ms = lambda: 60_000

        core, factory = healthy_core(fault=skewed)
        advance(core, factory, 1500)
        assert core.oms.decisions, "the strategy tried"
        assert all("clock_skew" in d.reasons for d in core.oms.decisions)
        assert sends(core) == 0

    def test_near_expiry(self):
        core, factory = healthy_core()
        t = SPEC.end_utc_ms - 10_000
        feed_books(core, factory, t)
        core.run_until(t)
        before = sends(core)
        advance(core, factory, 2000)
        assert core.kill.has(KillReason.NEAR_EXPIRY) and sends(core) == before

    def test_storage_failure_stops_everything(self):
        core, factory = healthy_core()
        core.kill.engage(KillReason.STORAGE_FAILURE, T0)
        self._assert_no_new_risk(core, factory, KillReason.STORAGE_FAILURE)

    def test_websocket_gap_invalidates_until_snapshot(self):
        core, factory = healthy_core()
        for asset in (SPEC.yes_asset_id, SPEC.no_asset_id):
            core.visible.books[asset].invalidate("stream_disconnected")
        before = sends(core)
        t = core.scheduler.now + 300
        core.run_until(t)
        core._decide(t)
        assert sends(core) == before
        advance(core, factory, 1500)
        book = core.visible.books[SPEC.yes_asset_id]
        assert book.validity.value == "ready", "a fresh snapshot is the recovery path"
        assert not core.kill.active

    def test_recorded_gap_event_invalidates_market_books(self):
        core, factory = healthy_core()
        t = core.scheduler.now + 10
        gap = {
            "event_type": "gap",
            "reason": "stream_error",
            "market_id": SPEC.market_id,
            "ts_utc_ms": t,
        }
        core.schedule_envelope(factory.make(gap, t, source="control", topic="gap", source_ts=t))
        core.run_until(t)
        assert core.visible.books[SPEC.yes_asset_id].invalid_reason == "stream_error"
        assert core.gaps

    def test_crossed_snapshot_invalidates_and_blocks(self):
        core, factory = healthy_core()
        before = sends(core)
        t = core.scheduler.now + 300
        core.schedule_envelope(
            factory.make(
                book_payload(SPEC.yes_asset_id, [("0.60", "1")], [("0.40", "1")], t),
                t,
                source_ts=t,
            )
        )
        core.run_until(t)
        assert core.visible.books[SPEC.yes_asset_id].invalid_reason == "crossed_book"
        assert core.invalidations["crossed_book"] >= 1
        assert sends(core) == before

    def test_operator_kill(self):
        core, factory = healthy_core()
        core.kill.engage(KillReason.OPERATOR, T0)
        self._assert_no_new_risk(core, factory, KillReason.OPERATOR)
        core.kill.operator_reset(core.scheduler.now, "ops")
        advance(core, factory, 1500)
        assert sends(core) > 0, "explicit operator reset restores trading"


class TestOrderFaults:
    def _intent(self, intent_id="x"):
        return QuoteIntent(
            intent_id=intent_id,
            market_id=SPEC.market_id,
            asset_id=SPEC.yes_asset_id,
            outcome=Outcome.YES,
            side=Side.BUY,
            price=D("0.30"),
            size=D(10),
            post_only=True,
            order_type=OrderType.GTC,
            expiry_cutoff_ms=SPEC.end_utc_ms,
            strategy_id="t",
        )

    def test_unknown_ack_blocks_next_submit_until_reconciled(self):
        core, _ = healthy_core(paper_settings(passive_quotes=False))
        real_submit = core.port.submit
        core.port.submit = lambda intent: SubmitResult(SubmitStatus.UNKNOWN, reason="timeout")
        record = core.oms.submit(self._intent("a"))
        assert record.state is OrderState.UNKNOWN
        core.port.submit = real_submit
        blocked = core.oms.submit(self._intent("b"))
        assert blocked.state is OrderState.RISK_REJECTED
        assert "unknown_order_state" in core.oms.decisions[-1].reasons
        core._reconcile("after-unknown")
        assert core.reconciliations[-1].clean
        assert core.oms.orders["a"].state is OrderState.REJECTED
        allowed = core.oms.submit(self._intent("c"))
        assert allowed.state is not OrderState.RISK_REJECTED

    def test_rate_limit_on_send_is_ambiguous_not_retried(self):
        from polymarket import RateLimitError

        core, _ = healthy_core(paper_settings(passive_quotes=False))
        calls = []

        def limited(intent):
            calls.append(intent.intent_id)
            raise RateLimitError("429")

        core.port.submit = limited
        assert core.oms.submit(self._intent("a")).state is OrderState.UNKNOWN
        assert calls == ["a"]

    def test_journal_failure_means_no_send(self, tmp_path):
        """Database or disk down: if the intent cannot be made durable, it is never sent."""
        journal = InMemoryJournal()
        core, _ = healthy_core(paper_settings(passive_quotes=False), journal=journal)

        def broken(record):
            raise OSError(28, "No space left on device")

        journal.append = broken
        before = sends(core)
        with pytest.raises(OSError):
            core.oms.submit(self._intent("a"))
        assert sends(core) == before
        assert core.account.cash_reserved == 0

    def test_fill_status_failed_reverses_and_requires_reconcile(self):
        core, factory = healthy_core(paper_settings(passive_quotes=False))
        intent = self._intent("a")
        core.oms.submit(intent)
        t = core.scheduler.now + 1000
        feed_books(core, factory, t)
        core.run_until(t)
        core.schedule_envelope(
            factory.make(
                trade_payload(SPEC.yes_asset_id, "SELL", "0.25", "1", t + 1, "0xf"),
                t + 1,
                source_ts=t + 1,
            )
        )
        core.run_until(t + 1000)
        fill = core.exchange.truth.trades[0]
        from dataclasses import replace

        core.oms.on_fill(replace(fill, status=FillStatus.FAILED))
        assert core.account.shares_net(SPEC.yes_asset_id) == 0
        assert core.oms.risk_frozen

    def test_journal_is_durable_for_restart(self, tmp_path):
        journal = FileJournal(tmp_path / "journal.jsonl")
        core, _ = healthy_core(paper_settings(passive_quotes=False), journal=journal)
        core.oms.submit(self._intent("a"))
        rebuilt, _ = healthy_core(
            paper_settings(passive_quotes=False), journal=FileJournal(journal.path)
        )
        rebuilt.oms.restore()
        assert rebuilt.oms.orders["a"].state is OrderState.UNKNOWN
        assert rebuilt.oms.risk_frozen
        before = sends(rebuilt)
        blocked = rebuilt.oms.submit(self._intent("b"))
        assert blocked.state is OrderState.RISK_REJECTED and sends(rebuilt) == before


class TestRiskScenarios:
    def test_one_side_fill_violent_move_records_real_loss(self):
        """BR-404 acceptance: YES fills, price collapses, market resolves against us."""
        factory = EnvelopeFactory()
        settings = paper_settings(
            allow_negative_edge_risk_hedge=True, max_hedge_loss_usd="1", refresh_min_age_ms=100_000
        )
        core = SimCore(settings, seed=3)
        core.schedule_envelope(factory.spec(SPEC, T0 - 5000))
        feed_books(core, factory, T0)
        core.run_until(T0 + 1000)
        feed_books(core, factory, T0 + 1000)
        core.run_until(T0 + 2000)
        resting = [o for o in core.oms.orders.values() if o.state is OrderState.OPEN]
        assert len(resting) == 2
        t = T0 + 2000
        crash = factory.make(
            trade_payload(SPEC.yes_asset_id, "SELL", "0.10", "500", t, "0xcrash"), t, source_ts=t
        )
        core.schedule_envelope(crash)
        for asset, ask in ((SPEC.yes_asset_id, "0.12"), (SPEC.no_asset_id, "0.92")):
            core.schedule_envelope(
                factory.make(
                    book_payload(asset, [("0.08", "100")], [(ask, "100")], t), t + 1, source_ts=t
                )
            )
        core.run_until(t + 2000)
        inventory = core.account.inventory(SPEC)
        assert inventory.unpaired_yes > 0 and inventory.paired == 0
        assert core.orchestrator.alerts.records or core.risk.hedge_loss_used <= D(1)
        assert core.risk.hedge_loss_used <= D(1), "the hedge budget is never exceeded"
        core.schedule_envelope(factory.resolution(SPEC, SPEC.end_utc_ms + 30_000, up_wins=False))
        core.run_until(SPEC.end_utc_ms + 40_000)
        flows = core.account.flows[SPEC.market_id]
        assert flows.settled and flows.realized_pnl < 0, "the actual loss is on the books"
        assert core.account.ledger.conservation_errors() == []

    def test_opening_quotes_cannot_breach_limits_when_both_fill(self):
        factory = EnvelopeFactory()
        core = SimCore(paper_settings(), seed=3)
        core.schedule_envelope(factory.spec(SPEC, T0 - 5000))
        feed_books(core, factory, T0)
        core.run_until(T0)
        advance(core, factory, 1000)
        for order in core.oms.open_orders:
            assert order.intent.notional <= core.limits.max_order_notional
        assert core.account.cash_reserved <= core.limits.max_total_gross_notional


class TestModeSafety:
    @pytest.mark.parametrize("mode", ["paper", "shadow"])
    def test_simulated_modes_use_simulated_adapters_only(self, mode):
        core = SimCore(parse_settings({"mode": mode}), seed=1)
        assert core.port.is_real is False
        assert type(core.port).__name__ in {"PaperExecution", "ShadowExecution"}

    def test_factory_never_returns_real_for_simulated_modes(self):
        class Sim:
            is_real = False

        port = build_execution(parse_settings({"mode": "paper"}), paper_factory=Sim)
        assert port.is_real is False

    def test_shadow_marks_everything_hypothetical(self, tmp_path):
        path = tmp_path / "shadow.jsonl"
        core = SimCore(
            parse_settings({"mode": "shadow", "strategies": {"passive_quotes": True}}),
            seed=1,
            shadow_path=path,
        )
        factory = EnvelopeFactory()
        core.schedule_envelope(factory.spec(SPEC, T0 - 5000))
        feed_books(core, factory, T0)
        core.run_until(T0)
        advance(core, factory, 1000)
        records = core.port.records
        assert records and all(r["hypothetical"] for r in records)
        assert all("never happened" in r["uncertainty"] for r in records)
        assert path.read_text().count("hypothetical_order") >= 1


class TestStorageFaults:
    def test_journal_failure_during_a_decision_latches_the_kill_switch(self):
        """Disk full or database down mid-session: no send, no crash, new risk stopped."""
        journal = InMemoryJournal()
        core, factory = healthy_core(journal=journal, settings=paper_settings(passive_quotes=False))
        settings = paper_settings(passive_quotes=True)
        core.orchestrator.settings = settings

        def broken(record):
            raise OSError(28, "No space left on device")

        journal.append = broken
        before = sends(core)
        advance(core, factory, 1500)
        assert sends(core) == before
        assert core.kill.has(KillReason.STORAGE_FAILURE) and core.kill.latched
        assert any(a["kind"] == "storage_failure" for a in core.orchestrator.alerts.records)

    def test_postgres_failure_also_latches(self):
        from bone_reaper.storage.postgres import StoreError

        journal = InMemoryJournal()
        core, factory = healthy_core(journal=journal, settings=paper_settings(passive_quotes=False))
        core.orchestrator.settings = paper_settings(passive_quotes=True)

        def broken(record):
            raise StoreError("transaction failed: OperationalError")

        journal.append = broken
        advance(core, factory, 1500)
        assert core.kill.has(KillReason.STORAGE_FAILURE)


class TestMatchingEngineRestart:
    """docs.polymarket.com/trading/matching-engine: new work is rejected during a restart, then
    two minutes of post-only follow while cancels stay available."""

    def test_rejections_during_restart_are_definite_and_release_funds(self):
        restarting = SubmitResult(SubmitStatus.REJECTED, reason="restarting")

        def engine_down(core):
            core.port.submit = lambda intent: restarting
            core.port.submit_batch = lambda intents: [restarting for _ in intents]

        core, factory = healthy_core(fault=engine_down)
        advance(core, factory, 1500)
        states = {o.state for o in core.oms.orders.values()}
        assert states == {OrderState.REJECTED}, "a definite rejection is not an unknown order"
        assert core.account.cash_reserved == 0
        assert not core.oms.risk_frozen

    def test_quoting_resumes_after_restart_with_no_blind_resend(self):
        restarting = SubmitResult(SubmitStatus.REJECTED, reason="restarting")
        calls = []

        def engine_down(core):
            core.real_submit_batch = core.port.submit_batch
            core.port.submit_batch = lambda intents: (
                calls.extend(intents) or [restarting for _ in intents]
            )

        core, factory = healthy_core(fault=engine_down)
        advance(core, factory, 600)
        rejected = len(core.oms.orders)
        assert rejected and len({i.intent_id for i in calls}) == len(calls), "ids are never reused"
        core.port.submit_batch = core.real_submit_batch
        advance(core, factory, 1000)
        assert any(o.state is OrderState.OPEN for o in core.oms.orders.values())

    def test_post_only_window_keeps_quotes_and_rejects_takers_safely(self):
        """The maker path is unaffected; a taker hedge rejected in the window leaves no state."""
        core, _ = healthy_core(paper_settings(passive_quotes=False))

        def post_only_window(intent):
            if not intent.post_only:
                return SubmitResult(SubmitStatus.REJECTED, reason="restarting_post_only")
            return SubmitResult(SubmitStatus.ACCEPTED, "ex-1")

        core.port.submit = post_only_window
        taker = QuoteIntent(
            intent_id="taker",
            market_id=SPEC.market_id,
            asset_id=SPEC.yes_asset_id,
            outcome=Outcome.YES,
            side=Side.BUY,
            price=D("0.52"),
            size=D(5),
            post_only=False,
            order_type=OrderType.FOK,
            expiry_cutoff_ms=SPEC.end_utc_ms,
            strategy_id="t",
            purpose=IntentPurpose.PROFITABLE_HEDGE,
        )
        assert core.oms.submit(taker).state is OrderState.REJECTED
        assert core.account.cash_reserved == 0
        maker = QuoteIntent(
            intent_id="maker",
            market_id=SPEC.market_id,
            asset_id=SPEC.yes_asset_id,
            outcome=Outcome.YES,
            side=Side.BUY,
            price=D("0.40"),
            size=D(5),
            post_only=True,
            order_type=OrderType.GTC,
            expiry_cutoff_ms=SPEC.end_utc_ms,
            strategy_id="t",
        )
        assert core.oms.submit(maker).state is OrderState.OPEN

    def test_orders_lost_in_a_restart_are_found_by_reconciliation(self):
        """If the engine comes back without our resting orders, venue truth wins, with evidence."""
        core, factory = healthy_core()
        advance(core, factory, 600)
        open_orders = [o for o in core.oms.orders.values() if o.state is OrderState.OPEN]
        assert open_orders
        core.exchange.resting.clear()
        core.scheduler.run_until(core.scheduler.now + 6000, core._set_time)
        core._reconcile("after-engine-restart")
        report = core.reconciliations[-1]
        assert report.clean and report.orders_updated
        for order in open_orders:
            assert core.oms.orders[order.intent.intent_id].state is OrderState.CANCELED
        assert core.account.cash_reserved == 0


def test_shutdown_cancels_an_order_that_was_still_in_flight():
    """Regression: an order acknowledged after the cancel pass stayed open at shutdown."""
    from bone_reaper.live_sim import shutdown_orders

    core, _ = healthy_core()
    in_flight = [o for o in core.oms.orders.values() if o.state is OrderState.SUBMITTING]
    assert in_flight, "the setup decision just sent quotes that have not been acknowledged"
    shutdown_orders(core, core.scheduler.now)
    assert core.oms.open_orders == []
    assert core.account.cash_reserved == 0
