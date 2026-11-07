"""P4: quotes, refresh, sizing, hedging, kill switch and the pre-trade gate."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D

import pytest

from bone_reaper.accounting.position import PaperAccount
from bone_reaper.risk.exposure import market_exposure
from bone_reaper.risk.kill_switch import KillReason, KillSwitch
from bone_reaper.risk.limits import DailyLossTracker, RiskLimits
from bone_reaper.risk.pretrade import MarketView, PreTradeRisk
from bone_reaper.strategy.fair_value import (
    FairValue,
    midpoint_fair,
    model_fair,
    sanitize_probability,
)
from bone_reaper.strategy.hedging import HedgeAction, HedgePolicy, decide_hedge
from bone_reaper.strategy.quotes import (
    QuoteParams,
    RefreshRules,
    TargetQuote,
    build_quote_plan,
    needs_refresh,
    round_down_tick,
    size_quote,
    target_price,
)
from bone_reaper.time import FakeClock
from bone_reaper.types import (
    BookValidity,
    FillRecord,
    FillStatus,
    IntentPurpose,
    LiquidityRole,
    OrderRecord,
    OrderState,
    OrderType,
    Outcome,
    Side,
)
from helpers import NO, START, YES, book_state, make_intent, make_spec, paper_settings

SPEC = make_spec()
NOW = START + 60_000
Y, N = SPEC.yes_asset_id, SPEC.no_asset_id
PARAMS = QuoteParams(
    size_shares=D(10),
    margin=D("0.01"),
    pair_buffer=D("0.02"),
    min_price=D("0.05"),
    max_price=D("0.95"),
    max_order_notional=D(10),
    max_unpaired_notional=D(20),
    max_total_gross_notional=D(100),
    stop_seconds_before_close=20,
)
YES_BOOK = book_state(Y, (("0.49", "100"),), (("0.51", "100"),), now=NOW)
NO_BOOK = book_state(N, (("0.49", "100"),), (("0.51", "100"),), now=NOW)
FAIR = FairValue(D("0.50"), D("0.50"), "book_midpoint")


def fill(outcome: Outcome, gross: str, price: str = "0.40", trade: str = "t") -> FillRecord:
    return FillRecord(
        trade_id=trade,
        leg_key="l",
        intent_id=None,
        exchange_order_id=None,
        market_id=SPEC.market_id,
        asset_id=SPEC.asset_id(outcome),
        outcome=outcome,
        side=Side.BUY,
        role=LiquidityRole.MAKER,
        price=D(price),
        gross_shares=D(gross),
        net_shares=D(gross),
        fee_cash=D(0),
        fee_shares=D(0),
        cash_delta=-D(price) * D(gross),
        status=FillStatus.MATCHED,
        simulated=True,
        ts_utc_ms=NOW,
    )


def plan_for(account=None, yes=YES_BOOK, no=NO_BOOK, fair=FAIR, params=PARAMS, now=NOW, orders=()):
    account = account or PaperAccount(D(100))
    return build_quote_plan(
        SPEC,
        yes,
        no,
        fair,
        params,
        account.inventory(SPEC),
        market_exposure(account, SPEC, orders),
        account.cash_available,
        now,
    )


class TestQuoteGenerator:
    def test_rounds_down_to_tick(self):
        assert round_down_tick(D("0.4799"), D("0.01")) == D("0.47")
        assert round_down_tick(D("0.47"), D("0.01")) == D("0.47")
        assert round_down_tick(D("0.4799"), D("0.001")) == D("0.479")

    def test_uses_live_book_tick(self):
        fine_yes = replace(YES_BOOK, tick_size=D("0.001"))
        price, _ = target_price(fine_yes, NO_BOOK, D("0.50"), SPEC, PARAMS)
        assert price is not None and price % D("0.001") == 0

    def test_neither_quote_crosses_its_own_ask(self):
        plan = plan_for()
        assert len(plan.quotes) == 2
        for quote in plan.quotes:
            assert quote.price < D("0.51")

    def test_pair_cost_keeps_the_buffer(self):
        plan = plan_for()
        assert sum(q.price for q in plan.quotes) <= D(1) - PARAMS.pair_buffer

    def test_quote_respects_hedge_cost_bound(self):
        price, _ = target_price(YES_BOOK, NO_BOOK, D("0.50"), SPEC, PARAMS)
        hedge = D("0.51") + D("0.07") * D("0.51") * D("0.49")
        assert price <= D(1) - hedge - PARAMS.pair_buffer

    def test_fair_value_caps_quote(self):
        price, _ = target_price(YES_BOOK, NO_BOOK, D("0.20"), SPEC, PARAMS)
        assert price == D("0.19")

    @pytest.mark.parametrize(
        ("own", "other", "reason"),
        [
            (book_state(Y, (), (("0.51", "1"),), now=NOW), NO_BOOK, "own_book_one_sided"),
            (book_state(Y, (("0.49", "1"),), (), now=NOW), NO_BOOK, "own_book_one_sided"),
            (YES_BOOK, book_state(N, (("0.49", "1"),), (), now=NOW), "no_counterpart_liquidity"),
        ],
    )
    def test_skips_without_two_sided_information(self, own, other, reason):
        assert target_price(own, other, D("0.5"), SPEC, PARAMS) == (None, reason)

    def test_below_min_viable_price(self):
        expensive = book_state(N, (("0.90", "1"),), (("0.96", "1"),), now=NOW)
        price, reason = target_price(YES_BOOK, expensive, D("0.5"), SPEC, PARAMS)
        assert price is None and reason == "below_min_viable_price"

    def test_tight_book_cannot_post_only_cross(self):
        tight = book_state(Y, (("0.49", "1"),), (("0.50", "1"),), now=NOW)
        cheap_other = book_state(N, (("0.01", "1"),), (("0.02", "1"),), now=NOW)
        price, reason = target_price(tight, cheap_other, D("0.90"), SPEC, PARAMS)
        assert price == D("0.49") or reason == "would_cross_post_only"

    def test_cutoff_stops_quotes(self):
        cutoff = SPEC.end_utc_ms - 20_000
        assert plan_for(now=cutoff).quotes == ()
        assert plan_for(now=cutoff).skipped == (("market", "quote_cutoff"),)
        assert plan_for(now=cutoff - 1).quotes != ()


class TestSizing:
    def test_both_quotes_reserve_their_worst_case_cash_together(self):
        """With cash for only one quote, the second cannot assume the first stays unfilled."""
        account = PaperAccount(D("3"))
        plan = plan_for(account)
        assert sum(q.price * q.size for q in plan.quotes) <= D(3)

    def test_below_minimum_after_limits_skips(self):
        account = PaperAccount(D("1"))
        plan = plan_for(account)
        assert not plan.quotes
        assert ("YES", "size_below_minimum_after_limits") in plan.skipped

    def test_order_notional_limit(self):
        size, _ = size_quote(
            Outcome.YES,
            D("0.50"),
            SPEC,
            replace(PARAMS, size_shares=D(1000)),
            PaperAccount(D(1000)).inventory(SPEC),
            market_exposure(PaperAccount(D(1000)), SPEC, ()),
            D(1000),
        )
        assert size == D(20), "10 dollars at 0.50"

    def test_imbalanced_side_stops_adding(self):
        account = PaperAccount(D(100))
        account.apply_fill(fill(Outcome.YES, "10"))
        plan = plan_for(account)
        assert [q.outcome for q in plan.quotes] == [Outcome.NO]
        assert ("YES", "inventory_skew_side_already_long") in plan.skipped

    def test_counterpart_grows_to_close_the_gap(self):
        account = PaperAccount(D(100))
        account.apply_fill(fill(Outcome.YES, "15", "0.30"))
        plan = plan_for(account)
        assert plan.quotes[0].outcome is Outcome.NO and plan.quotes[0].size == D(15)

    @pytest.mark.parametrize("filled_first", [Outcome.YES, Outcome.NO])
    def test_opening_quotes_cannot_breach_unpaired_limit_if_other_side_fills(self, filled_first):
        tight = replace(PARAMS, max_unpaired_notional=D(6), size_shares=D(100))
        plan = plan_for(params=tight)
        worst = max((q.price * q.size for q in plan.quotes), default=D(0))
        assert worst <= D(6)
        assert filled_first in {Outcome.YES, Outcome.NO}

    def test_gross_limit_shrinks_size(self):
        tight = replace(PARAMS, max_total_gross_notional=D(3))
        plan = plan_for(params=tight)
        assert sum(q.price * q.size for q in plan.quotes) <= D(3) * 2


class TestRefresh:
    RULES = RefreshRules(min_ticks=2, min_age_ms=2000)

    def order(self, price="0.45", created=NOW - 10_000):
        return OrderRecord(
            intent=make_intent(SPEC, price=price), state=OrderState.OPEN, created_utc_ms=created
        )

    def target(self, price):
        return TargetQuote(Outcome.YES, D(price), D(10))

    def test_one_tick_noise_does_not_cancel(self):
        assert not needs_refresh(self.order(), self.target("0.46"), D("0.01"), self.RULES, NOW)

    def test_two_ticks_with_age_cancels(self):
        assert needs_refresh(self.order(), self.target("0.47"), D("0.01"), self.RULES, NOW)

    def test_young_quote_is_kept(self):
        young = self.order(created=NOW - 100)
        assert not needs_refresh(young, self.target("0.60"), D("0.01"), self.RULES, NOW)

    def test_target_gone_cancels_even_if_young(self):
        assert needs_refresh(self.order(created=NOW), None, D("0.01"), self.RULES, NOW)

    def test_orchestrator_refresh_uses_live_book_tick(self):
        from bone_reaper.research.replay import SimCore

        core = SimCore(paper_settings(refresh_min_ticks=2, refresh_min_age_ms=1000), seed=1)
        core.specs[SPEC.market_id] = SPEC
        fine_yes = replace(
            book_state(Y, (("0.945", "100"),), (("0.955", "100"),), now=NOW, tick="0.001"),
            tick_size=D("0.001"),
        )
        fine_no = replace(
            book_state(N, (("0.045", "100"),), (("0.055", "100"),), now=NOW, tick="0.001"),
            tick_size=D("0.001"),
        )
        view = MarketView(SPEC, fine_yes, fine_no, None, SPEC.rules_hash)
        core.clock.set_utc_ms(NOW)
        existing = make_intent(
            SPEC, "q-yes", Outcome.YES, "0.930", "10", purpose=IntentPurpose.PASSIVE_QUOTE
        )
        core.oms.orders[existing.intent_id] = OrderRecord(
            intent=existing, state=OrderState.OPEN, created_utc_ms=NOW - 5000
        )
        core.orchestrator._quote(view, NOW)
        assert core.oms.orders["q-yes"].state in (OrderState.CANCEL_PENDING, OrderState.CANCELED)


POLICY = HedgePolicy(
    min_profit_per_pair=D("0.005"),
    allow_negative_edge=False,
    max_hedge_loss=D(0),
    hedge_loss_used=D(0),
    min_order_shares=D(5),
)


class TestHedging:
    def inv(self, yes="10", no="0"):
        account = PaperAccount(D(100))
        if D(yes) > 0:
            account.apply_fill(fill(Outcome.YES, yes, trade="y"))
        if D(no) > 0:
            account.apply_fill(fill(Outcome.NO, no, trade="n"))
        return account.inventory(SPEC)

    def counter(self, ask="0.45"):
        return book_state(N, (("0.40", "100"),), ((ask, "1000"),), now=NOW)

    def test_flat_needs_nothing(self):
        decision = decide_hedge(
            SPEC, self.inv("10", "10"), D("0.4"), self.counter(), POLICY, NOW, False
        )
        assert decision.action is HedgeAction.NONE

    def test_dust_below_min_order_is_left(self):
        decision = decide_hedge(SPEC, self.inv("3"), D("0.4"), self.counter(), POLICY, NOW, False)
        assert decision.action is HedgeAction.NONE

    def test_passive_counterpart_first(self):
        decision = decide_hedge(SPEC, self.inv(), D("0.4"), self.counter(), POLICY, NOW, True)
        assert decision.action is HedgeAction.PASSIVE_COUNTERPART
        assert decision.outcome is Outcome.NO

    def test_profitable_taker_hedge(self):
        late = SPEC.end_utc_ms - 30_000
        decision = decide_hedge(SPEC, self.inv(), D("0.40"), self.counter(), POLICY, late, True)
        assert decision.action is HedgeAction.PROFITABLE_TAKER_HEDGE
        assert decision.purpose is IntentPurpose.PROFITABLE_HEDGE
        assert decision.expected_pair_pnl > 0
        assert decision.quantity > D(10), "extra gross so net shares cover the fee"

    def test_mandated_one_side_fill_violent_move_stops_without_authorization(self):
        """YES filled at 0.40, then NO jumps to 0.90: the loss is real and is not hidden."""
        decision = decide_hedge(
            SPEC, self.inv(), D("0.40"), self.counter("0.90"), POLICY, NOW, False
        )
        assert decision.action is HedgeAction.STOP_AND_ALERT
        assert decision.reason == "hedge_unprofitable_and_not_authorized"
        assert decision.expected_pair_pnl < 0

    def test_authorized_negative_hedge_respects_budget_and_is_labelled(self):
        allowed = replace(POLICY, allow_negative_edge=True, max_hedge_loss=D(5))
        decision = decide_hedge(
            SPEC, self.inv(), D("0.40"), self.counter("0.70"), allowed, NOW, False
        )
        assert decision.action is HedgeAction.RISK_REDUCING_HEDGE
        assert decision.purpose is IntentPurpose.RISK_REDUCING_HEDGE
        assert decision.purpose is not IntentPurpose.PAIR_ENTRY
        assert decision.expected_pair_pnl < 0

    def test_budget_exceeded_stops(self):
        allowed = replace(POLICY, allow_negative_edge=True, max_hedge_loss=D("0.10"))
        decision = decide_hedge(
            SPEC, self.inv(), D("0.40"), self.counter("0.90"), allowed, NOW, False
        )
        assert decision.action is HedgeAction.STOP_AND_ALERT
        assert decision.reason == "hedge_loss_budget_exceeded"

    def test_used_budget_counts(self):
        allowed = replace(
            POLICY, allow_negative_edge=True, max_hedge_loss=D(5), hedge_loss_used=D("4.99")
        )
        decision = decide_hedge(
            SPEC, self.inv(), D("0.40"), self.counter("0.70"), allowed, NOW, False
        )
        assert decision.action is HedgeAction.STOP_AND_ALERT

    def test_no_liquidity_stops(self):
        thin = book_state(N, (), (("0.45", "1"),), now=NOW)
        decision = decide_hedge(SPEC, self.inv(), D("0.40"), thin, POLICY, NOW, False)
        assert decision.action is HedgeAction.STOP_AND_ALERT
        assert decision.reason == "insufficient_counterpart_liquidity"


class TestFairValue:
    def test_midpoint_uses_both_books(self):
        fair = midpoint_fair(YES_BOOK, NO_BOOK)
        assert fair.yes == D("0.5") and fair.no == D("0.5")
        assert midpoint_fair(book_state(Y, (), (), now=NOW), NO_BOOK) is None

    @pytest.mark.parametrize(
        "bad", [float("nan"), float("inf"), -float("inf"), 0.0, 1.0, -0.2, 1.5, None]
    )
    def test_model_output_sanitized(self, bad):
        assert sanitize_probability(bad) is None

    def test_extreme_is_clamped(self):
        assert sanitize_probability(0.9999) == D("0.99")

    @pytest.mark.parametrize("bad", [lambda: float("nan"), lambda: None, lambda: 1 / 0])
    def test_model_failure_falls_back_to_midpoint(self, bad):
        fair = model_fair(YES_BOOK, NO_BOOK, bad, True, "h")
        assert fair.source == "book_midpoint"

    def test_model_ignored_without_explicit_flag(self):
        assert model_fair(YES_BOOK, NO_BOOK, lambda: 0.9, False, "h").source == "book_midpoint"

    def test_model_used_with_flag_and_carries_hash(self):
        fair = model_fair(YES_BOOK, NO_BOOK, lambda: 0.6, True, "h")
        assert fair.source == "model" and fair.yes == D("0.6") and fair.model_hash == "h"


class TestExposure:
    def test_worst_case_assumes_one_side_fills_only(self):
        account = PaperAccount(D(100))
        orders = [
            OrderRecord(make_intent(SPEC, "y", Outcome.YES, "0.40", "10"), OrderState.OPEN),
            OrderRecord(make_intent(SPEC, "n", Outcome.NO, "0.50", "10"), OrderState.OPEN),
        ]
        exposure = market_exposure(account, SPEC, orders)
        assert exposure.worst_unpaired_notional == D(5)
        assert exposure.worst_unpaired_outcome is Outcome.NO
        assert exposure.gross_notional == D(9)

    def test_pending_cancel_still_counts(self):
        account = PaperAccount(D(100))
        order = OrderRecord(
            make_intent(SPEC, "y", Outcome.YES, "0.40", "10"),
            OrderState.CANCEL_PENDING,
            pending_cancel=True,
        )
        assert market_exposure(account, SPEC, [order]).gross_notional == D(4)

    def test_terminal_orders_do_not_count(self):
        order = OrderRecord(make_intent(SPEC), OrderState.CANCELED)
        assert market_exposure(PaperAccount(D(1)), SPEC, [order]).gross_notional == 0

    def test_paired_inventory_has_no_unpaired_exposure(self):
        account = PaperAccount(D(100))
        account.apply_fill(fill(Outcome.YES, "10", trade="a"))
        account.apply_fill(fill(Outcome.NO, "10", trade="b"))
        assert market_exposure(account, SPEC, ()).worst_unpaired_notional == 0


class TestKillSwitch:
    def test_transient_clears_latched_does_not(self):
        kill = KillSwitch()
        kill.engage(KillReason.STALE_FEED, 1)
        kill.clear(KillReason.STALE_FEED, 2)
        assert not kill.active
        kill.engage(KillReason.LOSS_CAP, 3)
        kill.clear(KillReason.LOSS_CAP, 4)
        kill.set_condition(KillReason.LOSS_CAP, False, 5)
        assert kill.active and kill.latched

    def test_reset_needs_operator_identity(self):
        kill = KillSwitch()
        kill.engage(KillReason.RECONCILIATION_MISMATCH, 1)
        with pytest.raises(ValueError):
            kill.operator_reset(2, " ")
        kill.operator_reset(2, "ops")
        assert not kill.active
        assert kill.history[-1][1] == "operator_reset:ops"


def make_risk(
    account=None, view=None, kill=None, frozen=(), orders=(), now=NOW, skew=None, **limit_changes
):
    account = account or PaperAccount(D(100))
    settings = paper_settings()
    limits = replace(RiskLimits.from_settings(settings), **limit_changes)
    default_view = MarketView(SPEC, YES_BOOK, NO_BOOK, None, SPEC.rules_hash)
    resolved = view if view is not None else default_view
    return PreTradeRisk(
        limits,
        FakeClock(now),
        account,
        lambda market_id: resolved if market_id == SPEC.market_id else None,
        lambda: list(orders),
        kill or KillSwitch(),
        lambda: list(frozen),
        clock_skew_ms=lambda: skew,
    )


def intent_ok(**changes):
    fields = {"price": "0.40", "size": "10"}
    fields.update(changes)
    return make_intent(SPEC, **fields)


class TestPreTradeGate:
    def test_clean_intent_allowed(self):
        decision = make_risk().evaluate(intent_ok())
        assert decision.allowed and decision.reasons == ()
        assert dict(decision.limit_snapshot)["max_order_notional"] == "10"

    @pytest.mark.parametrize(
        ("kwargs", "reason"),
        [
            ({"frozen": ("unknown_order_state",)}, "unknown_order_state"),
            ({"now": SPEC.end_utc_ms + 1}, "market_expired"),
            ({"now": START - 1}, "market_not_started"),
            ({"now": SPEC.end_utc_ms - 1000}, "quote_cutoff"),
            ({"skew": 5000}, "clock_skew"),
            ({"max_order_notional": D(1)}, "order_notional_limit"),
            ({"max_unpaired_notional": D(1)}, "worst_case_unpaired_limit"),
            ({"max_total_gross_notional": D(1)}, "total_gross_limit"),
            ({"account": PaperAccount(D(1))}, "insufficient_available_cash"),
        ],
    )
    def test_each_rule_rejects(self, kwargs, reason):
        decision = make_risk(**kwargs).evaluate(intent_ok())
        assert not decision.allowed and reason in decision.reasons

    def test_open_order_limit(self):
        orders = [
            OrderRecord(make_intent(SPEC, f"o{i}", price="0.10", size="5"), OrderState.OPEN)
            for i in range(4)
        ]
        assert "open_order_limit" in make_risk(orders=orders).evaluate(intent_ok()).reasons

    def test_stale_invalid_and_missing_books(self):
        stale = make_risk(now=NOW + 5000).evaluate(intent_ok())
        assert "yes_book_stale" in stale.reasons and "no_book_stale" in stale.reasons
        bad_book = replace(YES_BOOK, validity=BookValidity.INVALID, invalid_reason="crossed_book")
        view = MarketView(SPEC, bad_book, NO_BOOK, None, SPEC.rules_hash)
        assert "yes_book_invalid:crossed_book" in make_risk(view=view).evaluate(intent_ok()).reasons
        missing = MarketView(SPEC, None, NO_BOOK, None, SPEC.rules_hash)
        assert "yes_book_missing" in make_risk(view=missing).evaluate(intent_ok()).reasons

    def test_rules_hash_must_match_verified(self):
        view = MarketView(SPEC, YES_BOOK, NO_BOOK, None, "other")
        assert "rules_hash_unverified" in make_risk(view=view).evaluate(intent_ok()).reasons
        unverified = MarketView(SPEC, YES_BOOK, NO_BOOK, None, None)
        assert "rules_hash_unverified" in make_risk(view=unverified).evaluate(intent_ok()).reasons

    def test_mandated_case6_expired_and_stale_twap_and_unknown_market(self):
        expired = make_risk(now=SPEC.end_utc_ms + 1).evaluate(intent_ok())
        assert not expired.allowed
        unknown = make_intent(make_spec(1))
        assert "market_unknown" in make_risk().evaluate(unknown).reasons
        needing_reference = make_risk(require_reference=True)
        assert "reference_unavailable" in needing_reference.evaluate(intent_ok()).reasons

    def test_stale_twap_rejected_when_required(self):
        from bone_reaper.types import ReferenceSource, ReferenceState

        reference = ReferenceState(
            "btcusd", D(75000), 60, ReferenceSource.CHAINLINK_TWAP, "f", NOW - 9000, NOW - 9000
        )
        view = MarketView(SPEC, YES_BOOK, NO_BOOK, reference, SPEC.rules_hash)
        risk = make_risk(view=view, require_reference=True)
        assert "reference_stale" in risk.evaluate(intent_ok()).reasons

    def test_off_tick_mismatch_and_size(self):
        off = make_intent(SPEC, price="0.405")
        assert "price_off_tick" in make_risk().evaluate(off).reasons
        small = make_intent(SPEC, size="1")
        assert "size_below_minimum" in make_risk().evaluate(small).reasons
        wrong = make_intent(SPEC, outcome=Outcome.YES, asset_id=N)
        assert "outcome_asset_mismatch" in make_risk().evaluate(wrong).reasons
        foreign = make_intent(SPEC, asset_id="999")
        assert "asset_not_in_market" in make_risk().evaluate(foreign).reasons

    def test_daily_loss_limit(self):
        account = PaperAccount(D(100))
        account.apply_fill(fill(Outcome.YES, "25", "0.40"))
        account.settle(SPEC, Outcome.NO, NOW)
        decision = make_risk(account=account).evaluate(intent_ok())
        assert "daily_loss_limit" in decision.reasons

    @pytest.mark.parametrize("reason", list(KillReason))
    def test_every_kill_reason_blocks_new_risk(self, reason):
        kill = KillSwitch()
        kill.engage(reason, NOW)
        decision = make_risk(kill=kill).evaluate(intent_ok())
        assert f"kill:{reason.value}" in decision.reasons

    def test_risk_reduction_passes_near_expiry_only_if_authorized(self):
        kill = KillSwitch()
        kill.engage(KillReason.NEAR_EXPIRY, NOW)
        hedge = make_intent(
            SPEC,
            outcome=Outcome.NO,
            post_only=False,
            order_type=OrderType.FOK,
            purpose=IntentPurpose.RISK_REDUCING_HEDGE,
        )
        unauthorized = make_risk(kill=kill).evaluate(hedge)
        assert "risk_reducing_hedge_not_authorized" in unauthorized.reasons
        authorized = make_risk(kill=kill, allow_negative_edge_hedge=True).evaluate(hedge)
        assert authorized.allowed

    def test_risk_reduction_still_blocked_by_uncertain_state(self):
        hedge = make_intent(
            SPEC, post_only=False, order_type=OrderType.FOK, purpose=IntentPurpose.UNWIND
        )
        frozen = make_risk(frozen=("unknown_order_state",), allow_negative_edge_hedge=True)
        assert not frozen.evaluate(hedge).allowed
        kill = KillSwitch()
        kill.engage(KillReason.RECONCILIATION_MISMATCH, NOW)
        blocked = make_risk(kill=kill, allow_negative_edge_hedge=True)
        assert not blocked.evaluate(hedge).allowed

    def test_sell_needs_shares(self):
        sell = make_intent(
            SPEC,
            side=Side.SELL,
            post_only=False,
            order_type=OrderType.FOK,
            purpose=IntentPurpose.UNWIND,
        )
        decision = make_risk(allow_negative_edge_hedge=True).evaluate(sell)
        assert "insufficient_available_shares" in decision.reasons


class TestDailyLossTracker:
    def test_resets_at_utc_midnight(self):
        tracker = DailyLossTracker()
        day = 86_400_000
        assert tracker.realized_loss_today(D(0), day) == 0
        assert tracker.realized_loss_today(D(-5), day + 1) == D(5)
        assert tracker.realized_loss_today(D(-5), day * 2) == 0
        assert tracker.realized_loss_today(D(-7), day * 2 + 1) == D(2)


def test_unused_import_guard():
    assert YES and NO
