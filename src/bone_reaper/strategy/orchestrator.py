"""Decision loop shared by replay, paper and shadow. It reads visible state and talks only to the
OMS; it has no access to any execution adapter."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ..accounting.position import PaperAccount
from ..execution.oms import OrderManager
from ..market.validation import book_problems
from ..research.scan_runner import ScanAccumulator, thresholds_from
from ..risk.exposure import market_exposure
from ..risk.kill_switch import KillReason, KillSwitch
from ..risk.limits import RiskLimits
from ..risk.pretrade import MarketView, PreTradeRisk
from ..settings import Settings
from ..types import (
    TERMINAL_ORDER_STATES,
    ZERO,
    ArbCandidate,
    BookState,
    IntentPurpose,
    MarketSpec,
    OrderRecord,
    OrderState,
    OrderType,
    Outcome,
    QuoteIntent,
    Side,
)
from .fair_value import ProbabilityFn, model_fair
from .hedging import HedgeAction, HedgePolicy, decide_hedge
from .pair_scanner import evaluate_pair
from .quotes import QuoteParams, RefreshRules, TargetQuote, build_quote_plan, needs_refresh

DECISION_INTERVAL_MS = 250
SCAN_INTERVAL_MS = 1000


@dataclass(slots=True)
class Alerts:
    records: list[dict[str, Any]] = field(default_factory=list)
    _seen: set[tuple[str, str]] = field(default_factory=set)

    def once(self, kind: str, key: str, ts_utc_ms: int, **fields: Any) -> None:
        if (kind, key) not in self._seen:
            self._seen.add((kind, key))
            self.records.append({"kind": kind, "key": key, "ts_utc_ms": ts_utc_ms, **fields})


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        oms: OrderManager,
        account: PaperAccount,
        risk: PreTradeRisk,
        kill: KillSwitch,
        limits: RiskLimits,
        *,
        strategy_id: str = "baseline",
        probability: ProbabilityFn | None = None,
        model_hash: str | None = None,
        hedge_floor_seconds: int = 45,
        on_candidate: Callable[[ArbCandidate], None] | None = None,
    ) -> None:
        self.settings = settings
        self.oms = oms
        self.account = account
        self.risk = risk
        self.kill = kill
        self.limits = limits
        self.strategy_id = strategy_id
        self.probability = probability
        self.model_hash = model_hash
        self.hedge_floor_seconds = hedge_floor_seconds
        self.on_candidate = on_candidate
        self.alerts = Alerts()
        self.scan = ScanAccumulator()
        self.thresholds = thresholds_from(settings)
        self._last_decision = 0
        self._last_scan = 0
        self._unusable_since: int | None = None
        self._counter = 0
        self.fair_sources: dict[str, int] = {}
        s = settings.strategies
        self.params = QuoteParams(
            size_shares=s.quote_size_shares,
            margin=s.quote_margin,
            pair_buffer=s.pair_buffer,
            min_price=s.min_quote_price,
            max_price=s.max_quote_price,
            max_order_notional=limits.max_order_notional,
            max_unpaired_notional=limits.max_unpaired_notional,
            max_total_gross_notional=limits.max_total_gross_notional,
            stop_seconds_before_close=limits.stop_seconds_before_close,
        )
        self.refresh = RefreshRules(s.refresh_min_ticks, s.refresh_min_age_ms)

    def _intent_id(self, market_id: str, label: str) -> str:
        self._counter += 1
        return f"{self.strategy_id}-{market_id}-{label}-{self._counter}"

    def _live_orders(
        self, market_id: str, purpose: IntentPurpose | None = None
    ) -> list[OrderRecord]:
        return [
            o
            for o in self.oms.orders.values()
            if o.intent.market_id == market_id
            and o.state not in TERMINAL_ORDER_STATES
            and (purpose is None or o.intent.purpose is purpose)
        ]

    # --- safety --------------------------------------------------------------------------------

    def update_kill_conditions(self, view: MarketView | None, now: int) -> None:
        kill = self.kill
        kill.set_condition(KillReason.UNKNOWN_ORDER, bool(self.oms.unknown_orders), now)
        unusable = False
        if view is not None:
            unusable = self._books_unusable(view, now)
            cutoff = view.spec.end_utc_ms - self.limits.stop_seconds_before_close * 1000
            kill.set_condition(KillReason.NEAR_EXPIRY, now >= cutoff, now)
        # A brief sync after a trade only pauses new orders. Cancelling on each one would turn
        # ordinary trading activity into a cancel storm, so the kill needs it to persist.
        if not unusable:
            self._unusable_since = None
        elif self._unusable_since is None:
            self._unusable_since = now
        persistent = (
            self._unusable_since is not None
            and now - self._unusable_since > self.limits.max_book_age_ms
        )
        kill.set_condition(KillReason.STALE_FEED, persistent, now)

    def _books_unusable(self, view: MarketView, now: int) -> bool:
        max_age = self.limits.max_book_age_ms
        return any(
            book is None or bool(book_problems(book, now, max_age))
            for book in (view.yes_book, view.no_book)
        )

    def cancel_all(self, market_id: str, reason: str) -> None:
        for order in self._live_orders(market_id):
            if order.intent.purpose is IntentPurpose.PASSIVE_QUOTE:
                self.oms.cancel(order.intent.intent_id, reason)

    # --- main decision ------------------------------------------------------------------------

    def due(self, now: int) -> bool:
        return now - self._last_decision >= DECISION_INTERVAL_MS

    def tick(self, view: MarketView | None, now: int) -> None:
        if not self.due(now):
            return
        self._last_decision = now
        self.update_kill_conditions(view, now)
        if view is None or view.yes_book is None or view.no_book is None:
            return
        spec = view.spec
        s = self.settings.strategies
        if s.scan_pair and now - self._last_scan >= SCAN_INTERVAL_MS:
            self._last_scan = now
            self._scan(spec, view.yes_book, view.no_book, now)
        if self.kill.active:
            self.cancel_all(spec.market_id, f"kill:{','.join(self.kill.reasons)}")
        elif s.passive_quotes and not self._books_unusable(view, now):
            self._quote(view, now)
        if s.passive_quotes or s.allow_taker_taker:
            self._hedge(view, now)

    def _scan(self, spec: MarketSpec, yes_book: BookState, no_book: BookState, now: int) -> None:
        s = self.settings.strategies
        for q in s.scan_sizes:
            candidate = evaluate_pair(spec, yes_book, no_book, q, self.thresholds, now)
            self.scan.add(candidate)
            if self.on_candidate is not None:
                self.on_candidate(candidate)
            if candidate.passed and s.allow_taker_taker and not self.kill.active:
                self._enter_pair(spec, candidate, now)
                break

    def _enter_pair(self, spec: MarketSpec, candidate: ArbCandidate, now: int) -> None:
        """Research-only route, off by default. Two independent FOK requests, never atomic."""
        if self._live_orders(spec.market_id, IntentPurpose.PAIR_ENTRY):
            return
        intents = [
            QuoteIntent(
                intent_id=self._intent_id(spec.market_id, f"pair-{leg.outcome.value}"),
                market_id=spec.market_id,
                asset_id=leg.asset_id,
                outcome=leg.outcome,
                side=Side.BUY,
                price=leg.worst_price,
                size=leg.gross_shares,
                post_only=False,
                order_type=OrderType.FOK,
                expiry_cutoff_ms=spec.end_utc_ms,
                strategy_id=self.strategy_id,
                purpose=IntentPurpose.PAIR_ENTRY,
                created_utc_ms=now,
            )
            for leg in (candidate.yes_leg, candidate.no_leg)
        ]
        self.oms.submit_batch(intents)

    def _quote(self, view: MarketView, now: int) -> None:
        spec = view.spec
        if view.yes_book is None or view.no_book is None:
            return
        fair = model_fair(
            view.yes_book,
            view.no_book,
            self.probability,
            self.settings.strategies.probability_model,
            self.model_hash,
        )
        if fair is None:
            return
        self.fair_sources[fair.source] = self.fair_sources.get(fair.source, 0) + 1
        live = self._live_orders(spec.market_id)
        plan = build_quote_plan(
            spec,
            view.yes_book,
            view.no_book,
            fair,
            self.params,
            self.account.inventory(spec),
            market_exposure(self.account, spec, live),
            self.account.cash_available,
            now,
        )
        targets = {q.outcome: q for q in plan.quotes}
        new_intents: list[QuoteIntent] = []
        for outcome in (Outcome.YES, Outcome.NO):
            existing = [
                o
                for o in live
                if o.intent.outcome is outcome and o.intent.purpose is IntentPurpose.PASSIVE_QUOTE
            ]
            target = targets.get(outcome)
            if existing:
                # Replace only after the old order is confirmed gone: it still reserves funds.
                for order in existing:
                    if order.state in (OrderState.OPEN, OrderState.PARTIALLY_FILLED):
                        if needs_refresh(order, target, spec.tick_size, self.refresh, now):
                            self.oms.cancel(order.intent.intent_id, "refresh")
                continue
            if target is not None:
                new_intents.append(self._quote_intent(spec, target, now))
        if len(new_intents) == 1:
            self.oms.submit(new_intents[0])
        elif new_intents:
            self.oms.submit_batch(new_intents)

    def _quote_intent(self, spec: MarketSpec, target: TargetQuote, now: int) -> QuoteIntent:
        return QuoteIntent(
            intent_id=self._intent_id(spec.market_id, f"q-{target.outcome.value}"),
            market_id=spec.market_id,
            asset_id=spec.asset_id(target.outcome),
            outcome=target.outcome,
            side=Side.BUY,
            price=target.price,
            size=target.size,
            post_only=True,
            order_type=OrderType.GTC,
            expiry_cutoff_ms=spec.end_utc_ms - self.limits.stop_seconds_before_close * 1000,
            strategy_id=self.strategy_id,
            purpose=IntentPurpose.PASSIVE_QUOTE,
            created_utc_ms=now,
        )

    def _hedge(self, view: MarketView, now: int) -> None:
        spec = view.spec
        inventory = self.account.inventory(spec)
        if inventory.unpaired_yes <= 0 and inventory.unpaired_no <= 0:
            return
        hedges_live = [
            o
            for o in self._live_orders(spec.market_id)
            if o.intent.purpose
            in (IntentPurpose.PROFITABLE_HEDGE, IntentPurpose.RISK_REDUCING_HEDGE)
        ]
        if hedges_live or now >= spec.end_utc_ms:
            return
        unpaired_is_yes = inventory.unpaired_yes > 0
        flows = self.account.flows[spec.market_id]
        cost_per_share = ZERO
        if unpaired_is_yes and inventory.yes_net > 0:
            cost_per_share = flows.yes_cost / inventory.yes_net
        elif not unpaired_is_yes and inventory.no_net > 0:
            cost_per_share = flows.no_cost / inventory.no_net
        counter_book = view.no_book if unpaired_is_yes else view.yes_book
        if counter_book is None or book_problems(counter_book, now, self.limits.max_book_age_ms):
            return
        short_side = Outcome.NO if unpaired_is_yes else Outcome.YES
        counterpart_live = any(
            o.intent.outcome is short_side and o.intent.purpose is IntentPurpose.PASSIVE_QUOTE
            for o in self._live_orders(spec.market_id)
        )
        decision = decide_hedge(
            spec,
            inventory,
            cost_per_share,
            counter_book,
            HedgePolicy(
                min_profit_per_pair=Decimal("0.005"),
                allow_negative_edge=self.limits.allow_negative_edge_hedge,
                max_hedge_loss=self.limits.max_hedge_loss,
                hedge_loss_used=self.risk.hedge_loss_used,
                min_order_shares=spec.min_order_shares,
                passive_time_floor_seconds=self.hedge_floor_seconds,
            ),
            now,
            counterpart_live,
        )
        if decision.action in (HedgeAction.NONE, HedgeAction.PASSIVE_COUNTERPART):
            return
        if decision.action is HedgeAction.STOP_AND_ALERT:
            self.alerts.once(
                "unhedged_inventory",
                spec.market_id,
                now,
                reason=decision.reason,
                quantity=str(decision.quantity),
                expected_pair_pnl=str(decision.expected_pair_pnl),
            )
            return
        if decision.limit_price is None or decision.outcome is None or decision.purpose is None:
            return
        if decision.quantity * decision.limit_price > self.limits.max_order_notional:
            self.alerts.once(
                "hedge_exceeds_order_limit", spec.market_id, now, quantity=str(decision.quantity)
            )
            return
        record = self.oms.submit(
            QuoteIntent(
                intent_id=self._intent_id(spec.market_id, f"hedge-{decision.outcome.value}"),
                market_id=spec.market_id,
                asset_id=spec.asset_id(decision.outcome),
                outcome=decision.outcome,
                side=Side.BUY,
                price=decision.limit_price,
                size=decision.quantity,
                post_only=False,
                order_type=OrderType.FOK,
                expiry_cutoff_ms=spec.end_utc_ms,
                strategy_id=self.strategy_id,
                purpose=decision.purpose,
                created_utc_ms=now,
            )
        )
        if record.state is OrderState.RISK_REJECTED:
            self.alerts.once("hedge_rejected_by_risk", spec.market_id, now)
        elif decision.expected_pair_pnl < 0:
            self.risk.hedge_loss_used += -decision.expected_pair_pnl
