"""P3: ledger, settlement, OMS lifecycle, reconciliation. Includes the mandated cases 2, 5 and 6."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal as D
from typing import ClassVar

import pytest

from bone_reaper.accounting.ledger import (
    CASH_AVAILABLE,
    CASH_RESERVED,
    VENUE,
    Ledger,
    LedgerError,
    Line,
    transfer,
)
from bone_reaper.accounting.pnl import locked_pair_pnl, market_pnl, realized_total
from bone_reaper.accounting.position import PaperAccount, SettlementError
from bone_reaper.execution.oms import (
    LEGAL_TRANSITIONS,
    FileJournal,
    IllegalTransitionError,
    InMemoryJournal,
    OrderManager,
)
from bone_reaper.execution.ports import (
    AccountSnapshot,
    CancelResult,
    CancelStatus,
    RemoteOrder,
    SubmitResult,
    SubmitStatus,
)
from bone_reaper.execution.reconciler import reconcile
from bone_reaper.execution.settlement import (
    ResolutionUnknownError,
    settle_market,
    winning_outcome_from_resolution,
)
from bone_reaper.strategy.fees import buy_fill_economics
from bone_reaper.time import FakeClock
from bone_reaper.types import (
    TERMINAL_ORDER_STATES,
    FeeCollection,
    FillRecord,
    FillStatus,
    LiquidityRole,
    OrderState,
    OrderType,
    Outcome,
    QuoteIntent,
    RiskDecision,
    Side,
)
from helpers import START, crypto_fee, make_intent, make_spec

S = OrderState
SPEC = make_spec()


def fill_for(
    intent: QuoteIntent,
    gross: str = "10",
    trade: str = "t1",
    role: LiquidityRole = LiquidityRole.MAKER,
    spec=SPEC,
    status: FillStatus = FillStatus.MATCHED,
    price: D | None = None,
) -> FillRecord:
    price = intent.price if price is None else price
    net, fee, cash = buy_fill_economics(D(gross), price, spec.fee, role)
    return FillRecord(
        trade_id=trade,
        leg_key="leg",
        intent_id=intent.intent_id,
        exchange_order_id=None,
        market_id=intent.market_id,
        asset_id=intent.asset_id,
        outcome=intent.outcome,
        side=Side.BUY,
        role=role,
        price=price,
        gross_shares=D(gross),
        net_shares=net,
        fee_cash=fee.fee_cash,
        fee_shares=fee.fee_shares,
        cash_delta=-cash,
        status=status,
        simulated=True,
        ts_utc_ms=START,
    )


class TestLedger:
    def test_unbalanced_batch_refused(self):
        ledger = Ledger()
        with pytest.raises(LedgerError):
            ledger.post("x", [Line(VENUE, "pUSD", debit=D(1))], 1)
        assert not ledger.entries

    def test_batch_balances_per_asset_not_across_assets(self):
        ledger = Ledger()
        with pytest.raises(LedgerError):
            ledger.post(
                "x", [Line(VENUE, "pUSD", debit=D(1)), Line(VENUE, "shares", credit=D(1))], 1
            )

    def test_guarded_account_cannot_go_negative_and_batch_is_atomic(self):
        ledger = Ledger()
        ledger.post("in", transfer("pUSD", CASH_AVAILABLE, VENUE, D(5)), 1)
        with pytest.raises(LedgerError):
            ledger.post("out", transfer("pUSD", VENUE, CASH_AVAILABLE, D(6)), 2)
        assert ledger.balance(CASH_AVAILABLE) == D(5) and not ledger.has_cause("out")

    def test_idempotent_by_cause(self):
        ledger = Ledger()
        assert ledger.post("a", transfer("pUSD", CASH_AVAILABLE, VENUE, D(1)), 1)
        assert not ledger.post("a", transfer("pUSD", CASH_AVAILABLE, VENUE, D(1)), 1)
        assert ledger.balance(CASH_AVAILABLE) == D(1)

    def test_reversal_keeps_history(self):
        ledger = Ledger()
        ledger.post("a", transfer("pUSD", CASH_AVAILABLE, VENUE, D(3)), 1)
        count = len(ledger.entries)
        assert ledger.reverse("a", 2)
        assert not ledger.reverse("a", 3)
        assert ledger.balance(CASH_AVAILABLE) == 0 and len(ledger.entries) == count * 2
        assert ledger.entries[-1].reversal_of == "a"
        with pytest.raises(LedgerError):
            ledger.reverse("missing", 1)
        assert ledger.conservation_errors() == []


class TestPaperAccount:
    def test_buy_fill_consumes_reservation_and_is_applied_once(self):
        account = PaperAccount(D(100))
        intent = make_intent(SPEC, price="0.40", size="10")
        account.reserve_cash(intent.intent_id, D(4), START)
        fill = fill_for(intent)
        assert account.apply_fill(fill)
        assert not account.apply_fill(fill), "duplicate fill changes nothing"
        assert account.cash_reserved == 0 and account.cash_available == D(96)
        assert account.shares_net(intent.asset_id) == D(10)
        assert account.ledger.conservation_errors() == []

    def test_taker_fee_in_shares_changes_net_only(self):
        account = PaperAccount(D(100))
        intent = make_intent(
            SPEC, price="0.50", size="100", post_only=False, order_type=OrderType.FOK
        )
        fill = fill_for(intent, "100", role=LiquidityRole.TAKER)
        account.apply_fill(fill)
        assert account.shares_net(intent.asset_id) == D("96.5")
        assert account.cash_available == D(50)
        assert account.flows[SPEC.market_id].fees_usd_equivalent == D("1.75")

    def test_taker_fee_in_cash_paid_on_top(self):
        spec = make_spec(fee=crypto_fee(FeeCollection.CASH_ON_TOP))
        account = PaperAccount(D(100))
        intent = make_intent(
            spec, price="0.50", size="100", post_only=False, order_type=OrderType.FOK
        )
        account.apply_fill(fill_for(intent, "100", role=LiquidityRole.TAKER, spec=spec))
        assert account.shares_net(intent.asset_id) == D(100)
        assert account.cash_available == D("48.25")

    def test_cash_cannot_go_negative(self):
        account = PaperAccount(D(1))
        intent = make_intent(SPEC, price="0.50", size="100")
        with pytest.raises(LedgerError):
            account.apply_fill(fill_for(intent, "100"))
        assert account.shares_net(intent.asset_id) == 0

    def test_failed_fill_reverses_exactly(self):
        account = PaperAccount(D(100))
        intent = make_intent(SPEC)
        fill = fill_for(intent)
        account.apply_fill(fill)
        assert account.apply_fill(fill_for(intent, status=FillStatus.FAILED))
        assert account.cash_available == D(100) and account.shares_net(intent.asset_id) == 0
        assert account.flows[SPEC.market_id].cash_out == 0

    def test_mandated_case2_yes_only_has_zero_locked_pnl(self):
        """A YES-only maker fill is directional, whatever the other order is doing."""
        account = PaperAccount(D(100))
        account.apply_fill(fill_for(make_intent(SPEC, "y", Outcome.YES, "0.40", "10")))
        assert locked_pair_pnl(account, SPEC) == 0
        inventory = account.inventory(SPEC)
        assert inventory.paired == 0 and inventory.unpaired_yes == D(10)
        assert inventory.locked_payout == 0

    def test_complete_pair_locks_only_after_both_legs(self):
        account = PaperAccount(D(100))
        account.apply_fill(fill_for(make_intent(SPEC, "y", Outcome.YES, "0.40", "10"), trade="a"))
        account.apply_fill(fill_for(make_intent(SPEC, "n", Outcome.NO, "0.50", "10"), trade="b"))
        assert locked_pair_pnl(account, SPEC) == D(1)
        assert account.inventory(SPEC).locked_payout == D(10)

    def test_settle_one_pair_pays_exactly_one_unit(self):
        account = PaperAccount(D(100))
        account.apply_fill(fill_for(make_intent(SPEC, "y", Outcome.YES, "0.40", "1"), "1", "a"))
        account.apply_fill(fill_for(make_intent(SPEC, "n", Outcome.NO, "0.50", "1"), "1", "b"))
        payout = account.settle(SPEC, Outcome.YES, START)
        assert payout == D(1)
        assert account.cash_redeemable == D(1), "redeemable is not spendable"
        assert account.cash_available == D("99.10")
        assert account.redeem(SPEC, START + 1) == D(1)
        assert account.redeem(SPEC, START + 2) == 0, "cannot redeem twice"
        assert account.cash_available == D("100.10")
        assert realized_total(account) == D("0.10")

    def test_incomplete_share_on_losing_side_pays_nothing(self):
        account = PaperAccount(D(100))
        account.apply_fill(fill_for(make_intent(SPEC, "y", Outcome.YES, "0.40", "10")))
        assert account.settle(SPEC, Outcome.NO, START) == 0
        assert realized_total(account) == D(-4)
        assert account.shares_net(SPEC.yes_asset_id) == 0

    def test_settle_is_idempotent(self):
        account = PaperAccount(D(100))
        account.apply_fill(fill_for(make_intent(SPEC, "y", Outcome.YES, "0.40", "10")))
        assert account.settle(SPEC, Outcome.YES, START) == D(10)
        assert account.settle(SPEC, Outcome.YES, START) == 0

    def test_merge_then_settle_cannot_double_count(self):
        account = PaperAccount(D(100))
        account.apply_fill(fill_for(make_intent(SPEC, "y", Outcome.YES, "0.40", "10"), trade="a"))
        account.apply_fill(fill_for(make_intent(SPEC, "n", Outcome.NO, "0.50", "6"), "6", "b"))
        account.merge(SPEC, D(6), START)
        assert account.cash_available == D(100) - D(4) - D(3) + D(6)
        assert account.settle(SPEC, Outcome.YES, START) == D(4), "only unmerged YES remains"
        account.redeem(SPEC, START)
        assert account.cash_available == D(103)
        assert realized_total(account) == D(3)
        assert account.ledger.conservation_errors() == []

    def test_merge_limits(self):
        account = PaperAccount(D(100))
        account.apply_fill(fill_for(make_intent(SPEC, "y", Outcome.YES, "0.40", "10")))
        with pytest.raises(SettlementError):
            account.merge(SPEC, D(1), START)
        account.settle(SPEC, Outcome.YES, START)
        with pytest.raises(SettlementError):
            account.merge(SPEC, D(1), START)

    def test_redeem_requires_settlement(self):
        with pytest.raises(SettlementError):
            PaperAccount(D(1)).redeem(SPEC, START)

    def test_settlement_refuses_shares_reserved_by_open_orders(self):
        account = PaperAccount(D(100))
        account.apply_fill(fill_for(make_intent(SPEC, "y", Outcome.YES, "0.40", "10")))
        account.reserve_shares("sell", SPEC.yes_asset_id, D(5), START)
        with pytest.raises(SettlementError):
            account.settle(SPEC, Outcome.YES, START)
        account.release_reservation("sell", START)
        assert account.settle(SPEC, Outcome.YES, START) == D(10)

    def test_market_pnl_views(self):
        account = PaperAccount(D(100))
        account.apply_fill(fill_for(make_intent(SPEC, "y", Outcome.YES, "0.40", "10")))
        open_view = market_pnl(account, SPEC, D("0.50"), D("0.50"))
        assert open_view.realized is None and open_view.unrealized_mtm == D(1)
        account.settle(SPEC, Outcome.NO, START)
        settled = market_pnl(account, SPEC)
        assert settled.realized == D(-4) and settled.unrealized_mtm is None and settled.settled

    def test_adjustment_is_documented_not_silent(self):
        account = PaperAccount(D(10))
        account.post_adjustment("run1", D("0.5"), START)
        account.post_adjustment("run2", D("-0.2"), START)
        assert account.cash_available == D("10.3")
        assert account.ledger.has_cause("adjust:run1")
        assert account.ledger.conservation_errors() == []


class AllowAll:
    def __init__(self, allowed: bool = True, reasons: tuple[str, ...] = ()) -> None:
        self.allowed = allowed
        self.reasons = reasons
        self.count = 0

    def evaluate(self, intent: QuoteIntent) -> RiskDecision:
        self.count += 1
        return RiskDecision(f"r{self.count}", self.allowed, self.reasons, (), 0)


class SpyPort:
    is_real = False

    def __init__(self, result: SubmitResult | Exception | None = None) -> None:
        self.result = result or SubmitResult(SubmitStatus.ACCEPTED, "ex1")
        self.cancel_result: CancelResult | Exception = CancelResult(CancelStatus.PENDING)
        self.sent: list[str] = []
        self.batches: list[list[str]] = []
        self.cancels: list[str] = []
        self.batch_results: list[SubmitResult] | Exception | None = None
        self.snapshot_value = AccountSnapshot((), (), D(100), ())
        self.on_send = None

    def submit(self, intent: QuoteIntent) -> SubmitResult:
        self.sent.append(intent.intent_id)
        if self.on_send is not None:
            self.on_send(intent)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def submit_batch(self, intents: Sequence[QuoteIntent]) -> list[SubmitResult]:
        self.batches.append([i.intent_id for i in intents])
        if isinstance(self.batch_results, Exception):
            raise self.batch_results
        return list(self.batch_results or [])

    def cancel(self, intent_id: str, exchange_order_id: str | None) -> CancelResult:
        self.cancels.append(intent_id)
        if isinstance(self.cancel_result, Exception):
            raise self.cancel_result
        return self.cancel_result

    def account_snapshot(self) -> AccountSnapshot:
        return self.snapshot_value


def make_oms(port=None, risk=None, journal=None, cash="100"):
    port = port or SpyPort()
    account = PaperAccount(D(cash))
    oms = OrderManager(
        port,
        account,
        risk or AllowAll(),
        journal or InMemoryJournal(),
        FakeClock(START),
        {SPEC.market_id: SPEC.fee},
    )
    return oms, port, account


class TestOrderLifecycle:
    def test_transition_table_matches_architecture(self):
        assert S.UNKNOWN in LEGAL_TRANSITIONS[S.SUBMITTING]
        assert S.CANCELED not in LEGAL_TRANSITIONS[S.SUBMITTING]
        assert S.PARTIALLY_FILLED in LEGAL_TRANSITIONS[S.CANCEL_PENDING]
        for terminal in TERMINAL_ORDER_STATES:
            assert terminal not in LEGAL_TRANSITIONS

    def test_journal_and_reservation_before_send(self):
        journal = InMemoryJournal()
        oms, port, account = make_oms(journal=journal)
        seen = {}

        def check(intent):
            seen["reserved"] = account.cash_reserved
            seen["kinds"] = [r["kind"] for r in journal.records]

        port.on_send = check
        oms.submit(make_intent(SPEC))
        assert seen["reserved"] == D(4), "funds reserved before the network send"
        assert "intent" in seen["kinds"] and "risk_decision" in seen["kinds"]

    def test_happy_path_and_release(self):
        oms, _, account = make_oms()
        record = oms.submit(make_intent(SPEC))
        assert record.state is S.OPEN and record.exchange_order_id == "ex1"
        assert account.cash_reserved == D(4)
        oms.cancel("i1")
        assert oms.orders["i1"].state is S.CANCEL_PENDING
        assert account.cash_reserved == D(4), "a cancel request is not proof"
        oms.on_cancel_confirmed("i1")
        assert oms.orders["i1"].state is S.CANCELED and account.cash_reserved == 0

    def test_risk_rejection_never_reaches_port(self):
        oms, port, account = make_oms(risk=AllowAll(False, ("nope",)))
        record = oms.submit(make_intent(SPEC))
        assert record.state is S.RISK_REJECTED and port.sent == []
        assert account.cash_reserved == 0
        assert oms.decisions[0].reasons == ("nope",)

    def test_definite_rejection_releases_funds(self):
        oms, _, account = make_oms(SpyPort(SubmitResult(SubmitStatus.REJECTED, reason="bad")))
        assert oms.submit(make_intent(SPEC)).state is S.REJECTED
        assert account.cash_reserved == 0

    @pytest.mark.parametrize(
        "result", [TimeoutError("ack lost"), SubmitResult(SubmitStatus.UNKNOWN, reason="timeout")]
    )
    def test_ack_lost_becomes_unknown_never_retried_and_freezes(self, result):
        oms, port, account = make_oms(SpyPort(result))
        record = oms.submit(make_intent(SPEC))
        assert record.state is S.UNKNOWN and port.sent == ["i1"], "exactly one send"
        assert account.cash_reserved == D(4), "funds stay reserved while unknown"
        assert oms.risk_frozen and "unknown_order_state" in oms.freeze_reasons()

    def test_duplicate_intent_id_refused(self):
        oms, port, _ = make_oms()
        oms.submit(make_intent(SPEC))
        with pytest.raises(ValueError):
            oms.submit(make_intent(SPEC))
        assert port.sent == ["i1"]

    def test_fill_before_ack(self):
        oms, _port, account = make_oms(SpyPort(SubmitResult(SubmitStatus.PENDING)))
        intent = make_intent(SPEC)
        oms.submit(intent)
        assert oms.orders["i1"].state is S.SUBMITTING
        assert oms.on_fill(fill_for(intent, "4"))
        assert oms.orders["i1"].state is S.PARTIALLY_FILLED
        oms.on_ack("i1", "ex9")
        record = oms.orders["i1"]
        assert record.state is S.PARTIALLY_FILLED and record.exchange_order_id == "ex9"
        assert account.shares_net(intent.asset_id) == D(4)

    def test_duplicate_user_trade_applied_once(self):
        oms, _, account = make_oms()
        intent = make_intent(SPEC)
        oms.submit(intent)
        assert oms.on_fill(fill_for(intent, "4"))
        assert not oms.on_fill(fill_for(intent, "4"))
        assert oms.orders["i1"].matched_gross == D(4)
        assert account.shares_net(intent.asset_id) == D(4)

    def test_mandated_case5_cancel_ack_after_partial_fill(self):
        """Fill accounted once; the reservation is released only when it is safe."""
        oms, _, account = make_oms()
        intent = make_intent(SPEC, price="0.40", size="10")
        oms.submit(intent)
        oms.cancel("i1")
        assert oms.on_fill(fill_for(intent, "4"))
        assert oms.orders["i1"].state is S.PARTIALLY_FILLED
        assert oms.orders["i1"].pending_cancel
        assert account.cash_reserved == D("2.40"), "the unfilled remainder is still at risk"
        assert not oms.on_fill(fill_for(intent, "4")), "duplicate notice ignored"
        oms.on_cancel_confirmed("i1")
        record = oms.orders["i1"]
        assert record.state is S.CANCELED and record.matched_gross == D(4)
        assert account.cash_reserved == 0
        assert account.cash_available == D(100) - D("1.6")
        assert account.shares_net(intent.asset_id) == D(4)

    def test_full_fill_races_cancel(self):
        oms, _, account = make_oms()
        intent = make_intent(SPEC)
        oms.submit(intent)
        oms.cancel("i1")
        oms.on_fill(fill_for(intent, "10"))
        assert oms.orders["i1"].state is S.FILLED
        oms.on_cancel_rejected("i1", "order_not_open")
        assert oms.orders["i1"].state is S.FILLED and account.cash_reserved == 0

    def test_cancel_rejected_returns_to_open(self):
        oms, _, account = make_oms()
        oms.submit(make_intent(SPEC))
        oms.cancel("i1")
        oms.on_cancel_rejected("i1", "busy")
        assert oms.orders["i1"].state is S.OPEN and account.cash_reserved == D(4)

    def test_fill_after_terminal_is_recorded_and_freezes(self):
        oms, _, account = make_oms()
        intent = make_intent(SPEC)
        oms.submit(intent)
        oms.cancel("i1")
        oms.on_cancel_confirmed("i1")
        assert oms.on_fill(fill_for(intent, "3"))
        assert account.shares_net(intent.asset_id) == D(3), "a real fill is never dropped"
        assert oms.anomalies[0]["kind"] == "fill_after_terminal" and oms.risk_frozen

    def test_ambiguous_cancel_freezes_and_keeps_reservation(self):
        port = SpyPort()
        port.cancel_result = TimeoutError("no reply")
        oms, _, account = make_oms(port)
        oms.submit(make_intent(SPEC))
        oms.cancel("i1")
        assert oms.orders["i1"].state is S.CANCEL_PENDING and oms.risk_frozen
        assert account.cash_reserved == D(4)

    def test_cancel_noop_on_unknown_and_terminal(self):
        oms, port, _ = make_oms(SpyPort(TimeoutError()))
        oms.submit(make_intent(SPEC))
        oms.cancel("i1")
        assert port.cancels == [], "nothing certain to cancel yet"

    def test_orphan_fill_held_and_freezes_rather_than_guessed(self):
        oms, _, account = make_oms()
        stray = fill_for(make_intent(SPEC, "unknown"), trade="x")
        assert not oms.on_fill(stray)
        assert account.shares_net(SPEC.yes_asset_id) == 0
        assert oms.orphan_fills == [stray] and oms.risk_frozen

    def test_illegal_transition_raises(self):
        oms, _, _ = make_oms()
        oms.submit(make_intent(SPEC))
        oms.on_cancel_confirmed("i1") if False else None
        oms.force_state("i1", S.CANCELED, None, "test")
        with pytest.raises(IllegalTransitionError):
            oms.force_state("i1", S.OPEN, None, "test")


class TestBatchIndependence:
    def _intents(self):
        return [
            make_intent(SPEC, "y", Outcome.YES, "0.40"),
            make_intent(SPEC, "n", Outcome.NO, "0.50"),
        ]

    def test_one_side_rejects_other_stays_open(self):
        port = SpyPort()
        port.batch_results = [
            SubmitResult(SubmitStatus.ACCEPTED, "ey"),
            SubmitResult(SubmitStatus.REJECTED, reason="post_only"),
        ]
        oms, _, account = make_oms(port)
        records = oms.submit_batch(self._intents())
        assert [r.state for r in records] == [S.OPEN, S.REJECTED]
        assert account.cash_reserved == D(4)

    def test_risk_rejects_one_leg_only(self):
        class RejectNo(AllowAll):
            def evaluate(self, intent):
                decision = super().evaluate(intent)
                if intent.outcome is Outcome.NO:
                    return RiskDecision(decision.request_id, False, ("limit",), (), 0)
                return decision

        port = SpyPort()
        port.batch_results = [SubmitResult(SubmitStatus.ACCEPTED, "ey")]
        oms, _, _ = make_oms(port, RejectNo())
        records = oms.submit_batch(self._intents())
        assert [r.state for r in records] == [S.OPEN, S.RISK_REJECTED]
        assert port.batches == [["y"]]

    def test_batch_exception_marks_each_leg_unknown_no_resend(self):
        port = SpyPort()
        port.batch_results = TimeoutError("lost")
        oms, _, _ = make_oms(port)
        records = oms.submit_batch(self._intents())
        assert all(r.state is S.UNKNOWN for r in records) and len(port.batches) == 1

    def test_result_count_mismatch_is_unknown(self):
        port = SpyPort()
        port.batch_results = [SubmitResult(SubmitStatus.ACCEPTED, "ey")]
        oms, _, _ = make_oms(port)
        assert all(r.state is S.UNKNOWN for r in oms.submit_batch(self._intents()))


class TestRestart:
    def test_restart_marks_inflight_unknown_and_freezes(self, tmp_path):
        journal = FileJournal(tmp_path / "j.jsonl")
        oms, _, _ = make_oms(SpyPort(SubmitResult(SubmitStatus.PENDING)), journal=journal)
        intent = make_intent(SPEC)
        oms.submit(intent)
        oms.on_ack("i1", "ex1")
        oms.on_fill(fill_for(intent, "4"))
        pending = make_intent(SPEC, "i2", Outcome.NO, "0.50")
        oms.submit(pending)

        new_oms, new_port, new_account = make_oms(SpyPort(), journal=FileJournal(journal.path))
        new_oms.restore()
        assert new_oms.orders["i2"].state is S.UNKNOWN, "never assumed rejected"
        first = new_oms.orders["i1"]
        assert first.state is S.PARTIALLY_FILLED and first.matched_gross == D(4)
        assert new_account.shares_net(intent.asset_id) == D(4)
        assert new_account.cash_reserved == D("2.40") + D(5)
        assert new_oms.risk_frozen and new_port.sent == []

    def test_restart_does_not_reserve_for_rejected_intents(self):
        journal = InMemoryJournal()
        oms, _, _ = make_oms(risk=AllowAll(False, ("no",)), journal=journal)
        oms.submit(make_intent(SPEC, price="0.90", size="1000"))
        new_oms, _, new_account = make_oms(journal=journal, cash="1")
        new_oms.restore()
        assert new_oms.orders["i1"].state is S.RISK_REJECTED
        assert new_account.cash_reserved == 0

    def test_replayed_fill_after_restart_is_deduplicated(self):
        journal = InMemoryJournal()
        oms, _, _ = make_oms(journal=journal)
        intent = make_intent(SPEC)
        oms.submit(intent)
        oms.on_fill(fill_for(intent, "4"))
        new_oms, _, account = make_oms(journal=journal)
        new_oms.restore()
        assert not new_oms.on_fill(fill_for(intent, "4"))
        assert account.shares_net(intent.asset_id) == D(4)


class TestReconciliation:
    def test_ack_lost_but_order_live_is_recovered(self):
        oms, port, _ = make_oms(SpyPort(TimeoutError("ack lost")))
        oms.submit(make_intent(SPEC))
        port.snapshot_value = AccountSnapshot(
            (RemoteOrder("ex7", "i1", S.OPEN, D(0)),), (), D(100), ()
        )
        report = reconcile(oms, port, "r1")
        assert oms.orders["i1"].state is S.OPEN
        assert oms.orders["i1"].exchange_order_id == "ex7"
        assert report.clean and not oms.risk_frozen
        assert port.sent == ["i1"], "reconciliation never re-sends"

    def test_unknown_absent_with_no_trades_is_rejected_on_evidence(self):
        oms, port, account = make_oms(SpyPort(TimeoutError()))
        oms.submit(make_intent(SPEC))
        report = reconcile(oms, port, "r1")
        assert oms.orders["i1"].state is S.REJECTED and report.clean
        assert account.cash_reserved == 0

    def test_unknown_with_fill_is_filled_exactly_once(self):
        oms, port, account = make_oms(SpyPort(TimeoutError()))
        intent = make_intent(SPEC, price="0.40", size="10")
        oms.submit(intent)
        trade = fill_for(intent, "10")
        port.snapshot_value = AccountSnapshot((), (trade,), D(96), ((intent.asset_id, D(10)),))
        first = reconcile(oms, port, "r1")
        second = reconcile(oms, port, "r2")
        assert oms.orders["i1"].state is S.FILLED
        assert first.fills_merged == 1 and second.duplicate_fills_ignored == 1
        assert account.shares_net(intent.asset_id) == D(10)
        assert first.clean and second.clean

    def test_fill_before_ack_merges_through_linked_exchange_id(self):
        oms, port, account = make_oms(SpyPort(SubmitResult(SubmitStatus.PENDING)))
        intent = make_intent(SPEC)
        oms.submit(intent)
        fill = fill_for(intent, "4")
        orphan = FillRecord(
            **{
                **{f: getattr(fill, f) for f in fill.__dataclass_fields__},
                "intent_id": None,
                "exchange_order_id": "ex5",
            }
        )
        assert not oms.on_fill(orphan)
        port.snapshot_value = AccountSnapshot(
            (RemoteOrder("ex5", "i1", S.PARTIALLY_FILLED, D(4)),),
            (),
            D(100) - D("1.6"),
            ((intent.asset_id, D(4)),),
        )
        report = reconcile(oms, port, "r1")
        assert report.clean and oms.orphan_fills == []
        assert account.shares_net(intent.asset_id) == D(4)

    def test_unknown_cancel_resolved_from_venue(self):
        port = SpyPort()
        port.cancel_result = TimeoutError()
        oms, _, account = make_oms(port)
        oms.submit(make_intent(SPEC))
        oms.cancel("i1")
        oms.clock.advance_ms(10_000)
        report = reconcile(oms, port, "r1")
        assert oms.orders["i1"].state is S.CANCELED and report.clean
        assert account.cash_reserved == 0

    def test_cancel_not_effective_returns_order_to_open(self):
        port = SpyPort()
        port.cancel_result = TimeoutError()
        oms, _, _ = make_oms(port)
        oms.submit(make_intent(SPEC))
        oms.cancel("i1")
        oms.clock.advance_ms(10_000)
        port.snapshot_value = AccountSnapshot(
            (RemoteOrder("ex1", "i1", S.OPEN, D(0)),), (), D(100), ()
        )
        reconcile(oms, port, "r1")
        assert oms.orders["i1"].state is S.OPEN

    def test_young_inflight_requests_are_left_alone(self):
        oms, port, _ = make_oms(SpyPort(SubmitResult(SubmitStatus.PENDING)))
        oms.submit(make_intent(SPEC))
        report = reconcile(oms, port, "r1")
        assert oms.orders["i1"].state is S.SUBMITTING and report.inflight_left_alone == ["i1"]

    def test_stale_submitting_past_grace_is_resolved(self):
        oms, port, _ = make_oms(SpyPort(SubmitResult(SubmitStatus.PENDING)))
        oms.submit(make_intent(SPEC))
        oms.clock.advance_ms(60_000)
        reconcile(oms, port, "r1")
        assert oms.orders["i1"].state is S.REJECTED

    @pytest.mark.parametrize(
        "snapshot",
        [
            AccountSnapshot((), (), D(99), ()),
            AccountSnapshot((), (), D(100), (("asset", D(5)),)),
            AccountSnapshot((RemoteOrder("ghost", None, S.OPEN, D(0)),), (), D(100), ()),
        ],
    )
    def test_unexplained_differences_keep_risk_frozen(self, snapshot):
        oms, port, _ = make_oms()
        port.snapshot_value = snapshot
        report = reconcile(oms, port, "r1", snapshot=snapshot)
        assert not report.clean and oms.risk_frozen

    def test_small_difference_within_budget_is_clean(self):
        oms, port, _ = make_oms()
        snapshot = AccountSnapshot((), (), D("100.005"), ())
        assert reconcile(oms, port, "r1", D("0.01"), snapshot).clean

    def test_run_is_journaled(self):
        journal = InMemoryJournal()
        oms, port, _ = make_oms(journal=journal)
        reconcile(oms, port, "r1")
        assert journal.records[-1]["kind"] == "reconciliation"
        assert journal.records[-1]["run_id"] == "r1"


class TestResolution:
    PAYLOAD: ClassVar[dict] = {
        "closed": True,
        "yes_label": "Up",
        "no_label": "Down",
        "yes_price": "1",
        "no_price": "0",
    }

    def test_official_winner_read(self):
        assert winning_outcome_from_resolution(self.PAYLOAD, SPEC) is Outcome.YES
        flipped = self.PAYLOAD | {"yes_price": "0", "no_price": "1"}
        assert winning_outcome_from_resolution(flipped, SPEC) is Outcome.NO

    def test_label_order_is_checked_not_assumed(self):
        inverted = {
            "closed": True,
            "yes_label": "Down",
            "no_label": "Up",
            "yes_price": "1",
            "no_price": "0",
        }
        assert winning_outcome_from_resolution(inverted, SPEC) is Outcome.NO

    @pytest.mark.parametrize(
        "payload",
        [
            {"closed": False},
            {
                "closed": True,
                "yes_label": "Yes",
                "no_label": "No",
                "yes_price": "1",
                "no_price": "0",
            },
            {
                "closed": True,
                "yes_label": "Up",
                "no_label": "Down",
                "yes_price": "0.5",
                "no_price": "0.5",
            },
        ],
    )
    def test_unknown_resolution_refuses(self, payload):
        with pytest.raises(ResolutionUnknownError):
            winning_outcome_from_resolution(payload, SPEC)

    def test_settle_market_separates_redeemable(self):
        account = PaperAccount(D(100))
        account.apply_fill(fill_for(make_intent(SPEC, "y", Outcome.YES, "0.40", "10")))
        result = settle_market(account, SPEC, self.PAYLOAD, START)
        assert result.payout == D(10) and result.redeemable_until_redeemed == D(10)
        assert account.cash_available == D(96)


def test_ledger_reserved_account_named_consistently():
    account = PaperAccount(D(5))
    account.reserve_cash("a", D(2), 1)
    assert account.ledger.balance(CASH_RESERVED) == D(2)


def test_build_journal_and_store_backed_oms_roundtrip(tmp_path, monkeypatch):
    from bone_reaper.execution.oms import build_journal
    from bone_reaper.settings import parse_settings
    from bone_reaper.storage.postgres import StoreError
    from bone_reaper.types import to_jsonable

    assert isinstance(build_journal(parse_settings({})), InMemoryJournal)
    file_settings = parse_settings(
        {"storage": {"ledger": "sqlite", "journal_path": str(tmp_path / "j.jsonl")}}
    )
    assert isinstance(build_journal(file_settings), FileJournal)
    monkeypatch.delenv("BONE_REAPER_POSTGRES_DSN", raising=False)
    with pytest.raises(StoreError):
        build_journal(parse_settings({"storage": {"ledger": "postgresql"}}))

    class StoreJournal(InMemoryJournal):
        def __init__(self) -> None:
            super().__init__()
            self.intents: list[tuple[str, D]] = []
            self.fills: list[tuple[str, int]] = []

        def record_intent(self, intent, reservation_amount, ts_utc_ms) -> None:
            self.intents.append((intent.intent_id, reservation_amount))
            self.append({"kind": "intent", "ts_utc_ms": ts_utc_ms, "intent": to_jsonable(intent)})

        def record_fill(self, fill, entries) -> bool:
            self.fills.append((fill.trade_id, len(entries)))
            self.append({"kind": "fill", "ts_utc_ms": fill.ts_utc_ms, "fill": to_jsonable(fill)})
            return True

    store = StoreJournal()
    oms, _, _ = make_oms(journal=store)
    intent = make_intent(SPEC)
    oms.submit(intent)
    oms.on_fill(fill_for(intent, "4"))
    assert store.intents == [("i1", D(4))]
    assert store.fills == [("t1", 4)]
    rebuilt, _, account = make_oms(journal=store)
    rebuilt.restore()
    assert rebuilt.orders["i1"].matched_gross == D(4)
    assert account.shares_net(intent.asset_id) == D(4)
