"""Pre-trade gate. Each order is approved before it leaves the OMS; any uncertainty is a rejection.

Cancels use a separate path and never come through here. Risk-reducing orders do come through here
but skip the checks that only make sense for new risk, and they carry their own loss budget.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from ..accounting.pnl import realized_total
from ..accounting.position import PaperAccount
from ..market.validation import book_problems
from ..strategy.fees import fee_usd
from ..time import Clock
from ..types import (
    TERMINAL_ORDER_STATES,
    ZERO,
    BookState,
    IntentPurpose,
    MarketSpec,
    OrderRecord,
    QuoteIntent,
    ReferenceState,
    RiskDecision,
    Side,
)
from .exposure import market_exposure
from .kill_switch import KillReason, KillSwitch
from .limits import DailyLossTracker, RiskLimits

RISK_REDUCING = frozenset({IntentPurpose.RISK_REDUCING_HEDGE, IntentPurpose.UNWIND})
# Near expiry stops new risk but must not stop an authorized hedge of existing risk.
REDUCTION_IGNORES = frozenset({KillReason.NEAR_EXPIRY, KillReason.LOSS_CAP})


@dataclass(frozen=True, slots=True)
class MarketView:
    spec: MarketSpec
    yes_book: BookState | None
    no_book: BookState | None
    reference: ReferenceState | None = None
    verified_rules_hash: str | None = None


class PreTradeRisk:
    def __init__(
        self,
        limits: RiskLimits,
        clock: Clock,
        account: PaperAccount,
        market_view: Callable[[str], MarketView | None],
        open_orders: Callable[[], Sequence[OrderRecord]],
        kill: KillSwitch,
        frozen_reasons: Callable[[], list[str]],
        eligibility_reasons: Callable[[], list[str]] = list,
        clock_skew_ms: Callable[[], int | None] = lambda: None,
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self.limits = limits
        self.clock = clock
        self.account = account
        self.market_view = market_view
        self.open_orders = open_orders
        self.kill = kill
        self.frozen_reasons = frozen_reasons
        self.eligibility_reasons = eligibility_reasons
        self.clock_skew_ms = clock_skew_ms
        self.id_factory = id_factory
        self.daily_loss = DailyLossTracker()
        self.hedge_loss_used = ZERO
        self.rejections_by_reason: dict[str, int] = {}

    def _market_reasons(self, intent: QuoteIntent, view: MarketView, now: int) -> list[str]:
        spec = view.spec
        reasons: list[str] = []
        if intent.asset_id not in (spec.yes_asset_id, spec.no_asset_id):
            reasons.append("asset_not_in_market")
        elif spec.outcome_of(intent.asset_id) is not intent.outcome:
            reasons.append("outcome_asset_mismatch")
        if not spec.accepting_orders:
            reasons.append("market_not_accepting_orders")
        if now < spec.start_utc_ms:
            reasons.append("market_not_started")
        if now >= spec.end_utc_ms:
            reasons.append("market_expired")
        if view.verified_rules_hash is None or view.verified_rules_hash != spec.rules_hash:
            reasons.append("rules_hash_unverified")
        own_book = view.yes_book if intent.asset_id == spec.yes_asset_id else view.no_book
        live_tick = own_book.tick_size if own_book is not None else spec.tick_size
        if intent.price % live_tick != 0:
            reasons.append("price_off_tick")
        if intent.size < spec.min_order_shares:
            reasons.append("size_below_minimum")
        for label, book in (("yes", view.yes_book), ("no", view.no_book)):
            if book is None:
                reasons.append(f"{label}_book_missing")
            else:
                reasons.extend(
                    f"{label}_{p}" for p in book_problems(book, now, self.limits.max_book_age_ms)
                )
        if self.limits.require_reference:
            reasons.extend(self._reference_reasons(view.reference, now))
        return reasons

    def _reference_reasons(self, reference: ReferenceState | None, now: int) -> list[str]:
        if reference is None or reference.twap is None or reference.received_utc_ms is None:
            return ["reference_unavailable"]
        if now - reference.received_utc_ms > self.limits.max_reference_age_ms:
            return ["reference_stale"]
        return []

    def _new_risk_reasons(self, intent: QuoteIntent, view: MarketView, now: int) -> list[str]:
        reasons: list[str] = []
        limits = self.limits
        spec = view.spec
        if intent.notional > limits.max_order_notional:
            reasons.append("order_notional_limit")
        if now >= spec.end_utc_ms - limits.stop_seconds_before_close * 1000:
            reasons.append("quote_cutoff")
        live = [o for o in self.open_orders() if o.state not in TERMINAL_ORDER_STATES]
        if len(live) >= limits.max_open_orders:
            reasons.append("open_order_limit")
        exposure = market_exposure(self.account, spec, live, extra=intent)
        if exposure.worst_unpaired_notional > limits.max_unpaired_notional:
            reasons.append("worst_case_unpaired_limit")
        other_cost = self._other_market_cost(spec.market_id, live)
        if exposure.gross_notional + other_cost > limits.max_total_gross_notional:
            reasons.append("total_gross_limit")
        loss = self.daily_loss.realized_loss_today(realized_total(self.account), now)
        if loss >= limits.max_daily_realized_loss:
            reasons.append("daily_loss_limit")
        return reasons

    def _other_market_cost(self, market_id: str, live: Sequence[OrderRecord]) -> Decimal:
        total = ZERO
        for mid, flows in self.account.flows.items():
            if mid != market_id and not flows.settled:
                total += flows.yes_cost + flows.no_cost
        for order in live:
            if order.intent.market_id != market_id and order.intent.side is Side.BUY:
                total += order.remaining * order.intent.price
        return total

    def _funding_reasons(self, intent: QuoteIntent, view: MarketView) -> list[str]:
        if intent.side is Side.BUY:
            cost = intent.notional
            if not intent.post_only:
                cost += fee_usd(intent.size, intent.price, view.spec.fee)
            return [] if cost <= self.account.cash_available else ["insufficient_available_cash"]
        free = self.account.shares_free(intent.asset_id)
        return [] if intent.size <= free else ["insufficient_available_shares"]

    def evaluate(self, intent: QuoteIntent) -> RiskDecision:
        now = self.clock.utc_ms()
        reducing = intent.purpose in RISK_REDUCING
        reasons: list[str] = list(self.eligibility_reasons())
        reasons.extend(self.frozen_reasons())
        reasons.extend(
            f"kill:{r}"
            for r in self.kill.blocks_new_risk(REDUCTION_IGNORES if reducing else frozenset())
        )
        skew = self.clock_skew_ms()
        if skew is not None and abs(skew) > self.limits.max_clock_skew_ms:
            reasons.append("clock_skew")
        view = self.market_view(intent.market_id)
        if view is None:
            reasons.append("market_unknown")
        else:
            reasons.extend(self._market_reasons(intent, view, now))
            reasons.extend(self._funding_reasons(intent, view))
            if reducing:
                if not self.limits.allow_negative_edge_hedge:
                    if intent.purpose is IntentPurpose.RISK_REDUCING_HEDGE:
                        reasons.append("risk_reducing_hedge_not_authorized")
            else:
                reasons.extend(self._new_risk_reasons(intent, view, now))
        for reason in reasons:
            self.rejections_by_reason[reason] = self.rejections_by_reason.get(reason, 0) + 1
        return RiskDecision(
            request_id=self.id_factory(),
            allowed=not reasons,
            reasons=tuple(reasons),
            limit_snapshot=(
                ("max_order_notional", str(self.limits.max_order_notional)),
                ("max_unpaired_notional", str(self.limits.max_unpaired_notional)),
                ("max_total_gross_notional", str(self.limits.max_total_gross_notional)),
                ("max_daily_realized_loss", str(self.limits.max_daily_realized_loss)),
                ("max_open_orders", str(self.limits.max_open_orders)),
            ),
            ts_utc_ms=now,
        )
