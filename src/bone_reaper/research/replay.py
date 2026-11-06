"""Deterministic event-driven simulator shared by replay, paper and shadow.

Causality: each recorded event is applied to the exchange book at its (clamped) source time and to
the bot-visible book at its receive time plus any simulated extra delay. The strategy reads only
visible books, so nothing it decides can depend on data it had not yet received.
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ..accounting.pnl import realized_total
from ..accounting.position import PaperAccount
from ..connectors.polymarket_twap import ReferenceTracker
from ..execution.factory import build_execution
from ..execution.oms import InMemoryJournal, Journal, OrderManager
from ..execution.paper import (
    PRIORITY_EXCHANGE,
    PRIORITY_TIMER,
    PRIORITY_VISIBLE,
    EventScheduler,
    PaperExecution,
    SimExchange,
)
from ..execution.ports import ExecutionPort
from ..execution.reconciler import ReconciliationReport, reconcile
from ..execution.settlement import ResolutionUnknownError, winning_outcome_from_resolution
from ..execution.shadow import ShadowExecution
from ..market.book import BookManager
from ..market.rules import INSTRUMENTS
from ..risk.kill_switch import KillReason, KillSwitch
from ..risk.limits import RiskLimits
from ..risk.pretrade import MarketView, PreTradeRisk
from ..settings import Settings
from ..strategy.fair_value import ProbabilityFn, book_mid
from ..strategy.orchestrator import Orchestrator
from ..time import FakeClock
from ..types import (
    ZERO,
    BookValidity,
    EventEnvelope,
    FeeParams,
    FillRecord,
    IntentPurpose,
    MarketSpec,
    Mode,
    OrderState,
    Side,
    from_jsonable,
)
from .latency import LatencyModel, LatencyProfile, percentiles
from .maker_queue import QueueScenario

EQUITY_SAMPLE_MS = 5000
RECONCILE_AFTER_CLOSE_MS = 6000
MARKOUT_HORIZONS_MS = (1000, 5000, 30000)
BOOK_TOPICS = {"book", "price_change", "tick_size_change", "last_trade_price"}
REPLAY_WINDOW_MS = 30_000
HEARTBEAT_MS = 500


@dataclass(frozen=True, slots=True)
class SimScenario:
    """Everything a result depends on besides code, data and seed. Printed in every report."""

    name: str = "pessimistic"
    queue: QueueScenario = QueueScenario.PESSIMISTIC
    latency_factor: int = 1
    fee_multiplier: Decimal = Decimal(1)
    hedge_floor_seconds: int = 45

    def describe(self) -> dict[str, str]:
        return {
            "name": self.name,
            "queue_model": self.queue.value,
            "latency_factor": str(self.latency_factor),
            "fee_multiplier": str(self.fee_multiplier),
            "hedge_floor_seconds": str(self.hedge_floor_seconds),
            "rebates": "excluded (zero); variable and paid later, never pre-counted",
        }


def _stress_fee(fee: FeeParams, multiplier: Decimal) -> FeeParams:
    return dataclasses.replace(fee, rate=fee.rate * multiplier)


@dataclass(slots=True)
class FillStats:
    count: int = 0
    maker: int = 0
    taker: int = 0
    by_purpose: Counter[str] = field(default_factory=Counter)
    gross_shares: Decimal = ZERO
    notional: Decimal = ZERO
    fees_usd_equivalent: Decimal = ZERO
    hedge_cost: Decimal = ZERO
    markouts: dict[int, list[Decimal]] = field(default_factory=dict)


class SimCore:
    def __init__(
        self,
        settings: Settings,
        scenario: SimScenario | None = None,
        *,
        seed: int | None = None,
        journal: Journal | None = None,
        strategy_id: str = "baseline",
        probability: ProbabilityFn | None = None,
        model_hash: str | None = None,
        shadow_path: Any = None,
    ) -> None:
        self.settings = settings
        self.scenario = scenario or SimScenario(
            name=settings.paper.queue_scenario,
            queue=QueueScenario(settings.paper.queue_scenario),
        )
        self.seed = settings.paper.seed if seed is None else seed
        self.clock = FakeClock()
        self.scheduler = EventScheduler()
        paper = settings.paper
        base = LatencyProfile(
            submit_ms=paper.submit_latency_ms,
            cancel_ms=paper.cancel_latency_ms,
            ack_ms=paper.ack_latency_ms,
            visible_extra_ms=paper.visible_latency_ms,
            jitter_ms=paper.latency_jitter_ms,
        )
        self.latency = LatencyModel(base.scaled(self.scenario.latency_factor), self.seed)
        self.exchange = SimExchange(
            self.scheduler, self.latency, self.scenario.queue, paper.starting_cash_usd
        )
        self.visible = BookManager()
        self.account = PaperAccount(paper.starting_cash_usd)
        self.kill = KillSwitch()
        self.limits = RiskLimits.from_settings(settings)
        self.specs: dict[str, MarketSpec] = {}
        self.reference = ReferenceTracker("btcusd")
        self.journal: Journal = journal or InMemoryJournal()
        self._decision_ids = 0
        self.port = self._build_port(shadow_path)
        self.risk = PreTradeRisk(
            self.limits,
            self.clock,
            self.account,
            self._market_view,
            lambda: list(self.oms.orders.values()),
            self.kill,
            lambda: self.oms.freeze_reasons(),
            id_factory=self._next_decision_id,
        )
        self.fee_lookup: dict[str, FeeParams] = {}
        self.oms = OrderManager(
            self.port, self.account, self.risk, self.journal, self.clock, self.fee_lookup
        )
        self.exchange.listener = self.oms
        self.orchestrator = Orchestrator(
            settings,
            self.oms,
            self.account,
            self.risk,
            self.kill,
            self.limits,
            strategy_id=strategy_id,
            probability=probability,
            model_hash=model_hash,
            hedge_floor_seconds=self.scenario.hedge_floor_seconds,
        )
        if self.exchange.on_sim_fill is None:
            self.exchange.on_sim_fill = self._on_exchange_fill
        else:
            shadow_hook = self.exchange.on_sim_fill

            def both(fill: FillRecord) -> None:
                shadow_hook(fill)
                self._on_exchange_fill(fill)

            self.exchange.on_sim_fill = both
        self.fill_stats = FillStats(markouts={h: [] for h in MARKOUT_HORIZONS_MS})
        self.reconciliations: list[ReconciliationReport] = []
        self.events_processed = 0
        self.invalidations: Counter[str] = Counter()
        self.gaps: list[dict[str, Any]] = []
        self.timeline: list[dict[str, Decimal | int]] = []
        self.unresolved: dict[str, str] = {}
        self.resolved: set[str] = set()
        self.market_close_inventory: dict[str, dict[str, Decimal]] = {}
        self.decision_ticks = 0
        self.visibility_violations = 0
        self._exch_time: dict[str, int] = {}
        self._visible_time = 0
        self._sampler_started = False
        self._heartbeat_started = False
        self._next_heartbeat: int | None = None
        self.clock_offset_ms = 0
        self._last_data_ms = 0
        self._exposed_ms = 0
        self._sampled_ms = 0
        self.scheduler_started = False

    def _build_port(self, shadow_path: Any) -> ExecutionPort:
        settings = self.settings
        if settings.mode is Mode.READ_ONLY:
            # Reuses the same refusal as production; read_only can never simulate orders either.
            return build_execution(settings)
        return build_execution(
            settings,
            paper_factory=lambda: PaperExecution(self.exchange),
            shadow_factory=lambda: ShadowExecution(self.exchange, shadow_path),
        )

    def _next_decision_id(self) -> str:
        self._decision_ids += 1
        return f"risk-{self._decision_ids}"

    def _market_view(self, market_id: str) -> MarketView | None:
        spec = self.specs.get(market_id)
        if spec is None:
            return None
        return self._view_for(spec)

    def _view_for(self, spec: MarketSpec) -> MarketView:
        yes = self.visible.books.get(spec.yes_asset_id)
        no = self.visible.books.get(spec.no_asset_id)
        instrument = INSTRUMENTS.get((spec.symbol, spec.duration_minutes))
        return MarketView(
            spec=spec,
            yes_book=yes.state() if yes else None,
            no_book=no.state() if no else None,
            reference=self.reference.state(spec.start_utc_ms),
            verified_rules_hash=instrument.rules_hash if instrument else None,
        )

    def _active_spec(self, now: int) -> MarketSpec | None:
        current = [s for s in self.specs.values() if s.start_utc_ms <= now < s.end_utc_ms]
        return max(current, key=lambda s: s.start_utc_ms) if current else None

    # --- feeding ------------------------------------------------------------------------------

    def schedule_envelope(self, env: EventEnvelope, *, live: bool = False) -> None:
        recv = env.received_utc_ms
        if not self._sampler_started:
            self._sampler_started = True
            self.scheduler.at(recv, PRIORITY_TIMER, self._sample_equity)
        self._last_data_ms = max(self._last_data_ms, recv)
        if not self._heartbeat_started:
            # Resume where it stopped, so a long silence is checked rather than skipped.
            self._heartbeat_started = True
            self.scheduler.at(self._next_heartbeat or recv, PRIORITY_TIMER, self._heartbeat)
        visible_at = recv + self.latency.profile.visible_extra_ms
        self._visible_time = max(self._visible_time, visible_at)
        applies_to_exchange = (
            env.source in ("poly_market",) and env.topic in BOOK_TOPICS
        ) or env.topic == "resnapshot"
        if env.topic == "clock_probe":
            # Source timestamps are venue time and receive times are local. Without this the local
            # clock error would look like feed latency and overstate how stale the bot is.
            self.clock_offset_ms = int(env.payload()["offset_ms"])
        if env.topic == "market_spec":
            # Metadata is known to both sides at discovery time.
            self.scheduler.at(recv, PRIORITY_EXCHANGE, self._on_spec, env)
        elif applies_to_exchange:
            key = env.market_id or "_"
            source = env.source_ts_ms + self.clock_offset_ms if env.source_ts_ms else recv
            at = recv if live else max(self._exch_time.get(key, 0), min(source, recv))
            self._exch_time[key] = at
            payload = env.payload()
            self.scheduler.at(at, PRIORITY_EXCHANGE, self._on_exchange_event, payload)
            self.scheduler.at(
                self._visible_time, PRIORITY_VISIBLE, self._on_visible_event, env, payload
            )
            return
        self.scheduler.at(self._visible_time, PRIORITY_VISIBLE, self._on_visible_event, env, None)

    def _heartbeat(self) -> None:
        """Decide on quiet feeds too. A silent stream must trip the kill switch, as it does live."""
        now = self.scheduler.now
        self._decide(now)
        self._next_heartbeat = now + HEARTBEAT_MS
        if self._next_heartbeat <= self._last_data_ms:
            self.scheduler.at(self._next_heartbeat, PRIORITY_TIMER, self._heartbeat)
        else:
            self._heartbeat_started = False

    def _set_time(self, now: int) -> None:
        self.clock.set_utc_ms(now)

    def run_until(self, until: int | None) -> None:
        """Advance simulated time. Called with None it drains all remaining work."""
        self.scheduler.run_until(until, self._set_time)

    def _on_spec(self, env: EventEnvelope) -> None:
        spec = from_jsonable(MarketSpec, env.payload()["spec"])
        if self.scenario.fee_multiplier != 1:
            spec = dataclasses.replace(
                spec, fee=_stress_fee(spec.fee, self.scenario.fee_multiplier)
            )
        if spec.market_id in self.specs:
            return
        self.specs[spec.market_id] = spec
        self.fee_lookup[spec.market_id] = spec.fee
        self.exchange.register(spec)
        for asset in (spec.yes_asset_id, spec.no_asset_id):
            self.visible.register(spec.market_id, asset, spec.tick_size)
        self.scheduler.at(spec.end_utc_ms, PRIORITY_TIMER, self._on_market_end, spec)

    def _on_exchange_event(self, payload: dict[str, Any]) -> None:
        self.exchange.on_market_event(payload, self.scheduler.now)

    def _on_visible_event(self, env: EventEnvelope, parsed: dict[str, Any] | None) -> None:
        self.events_processed += 1
        now = self.scheduler.now
        if env.topic == "gap":
            self._on_gap(env)
        elif env.topic == "market_resolution":
            self._on_resolution(env)
        elif env.source == "poly_twap":
            self.reference.on_payload(env.payload(), now)
        elif (env.source == "poly_market" and env.topic in BOOK_TOPICS) or (
            env.topic == "resnapshot"
        ):
            result = self.visible.apply(parsed if parsed is not None else env.payload(), now)
            for _asset, reason in result.invalidated:
                self.invalidations[reason] += 1
            self._decide(now)

    def _on_gap(self, env: EventEnvelope) -> None:
        payload = env.payload()
        self.gaps.append(payload)
        market_id = payload.get("market_id")
        reason = str(payload.get("reason"))
        if market_id and market_id in self.specs:
            spec = self.specs[market_id]
            for asset in (spec.yes_asset_id, spec.no_asset_id):
                self.visible.books[asset].invalidate(reason)

    def _decide(self, now: int) -> None:
        if not self.orchestrator.due(now):
            return  # Building book views is costly; do it only when a decision will be made.
        spec = self._active_spec(now)
        view = self._view_for(spec) if spec else None
        if view is not None:
            for book in (view.yes_book, view.no_book):
                if (
                    book is not None
                    and book.last_update_utc_ms is not None
                    and book.last_update_utc_ms > now
                ):
                    self.visibility_violations += 1
        self.decision_ticks += 1
        self.orchestrator.tick(view, now)

    # --- lifecycle ----------------------------------------------------------------------------

    def _on_market_end(self, spec: MarketSpec) -> None:
        self.exchange.expire_market(spec.market_id)
        inventory = self.account.inventory(spec)
        self.market_close_inventory[spec.market_id] = {
            "paired": inventory.paired,
            "unpaired_yes": inventory.unpaired_yes,
            "unpaired_no": inventory.unpaired_no,
        }
        self.scheduler.at(
            spec.end_utc_ms + RECONCILE_AFTER_CLOSE_MS, PRIORITY_TIMER, self._reconcile, spec.slug
        )

    def _reconcile(self, label: str) -> None:
        report = reconcile(
            self.oms,
            self.port,
            f"recon-{label}",
            self.limits.max_unexplained_reconciliation,
        )
        self.reconciliations.append(report)
        if not report.clean:
            self.kill.engage(KillReason.RECONCILIATION_MISMATCH, self.scheduler.now)

    def _on_resolution(self, env: EventEnvelope) -> None:
        market_id = env.market_id or ""
        spec = self.specs.get(market_id)
        if spec is None or market_id in self.resolved:
            return
        try:
            winning = winning_outcome_from_resolution(env.payload(), spec)
        except ResolutionUnknownError as exc:
            self.unresolved[market_id] = str(exc)
            return
        now = self.scheduler.now
        self.account.settle(spec, winning, now)
        self.exchange.settle_truth(spec, winning)
        self.resolved.add(market_id)
        self.unresolved.pop(market_id, None)
        delay = self.settings.paper.resolution_delay_ms
        self.scheduler.at(now + delay, PRIORITY_TIMER, self._redeem, spec)
        loss = self.risk.daily_loss.realized_loss_today(realized_total(self.account), now)
        if loss >= self.limits.max_daily_realized_loss:
            self.kill.engage(KillReason.LOSS_CAP, now)

    def _redeem(self, spec: MarketSpec) -> None:
        self.account.redeem(spec, self.scheduler.now)
        self.exchange.redeem_truth(spec)

    # --- measurement --------------------------------------------------------------------------

    def _on_exchange_fill(self, fill: FillRecord) -> None:
        stats = self.fill_stats
        stats.count += 1
        if fill.role.value == "maker":
            stats.maker += 1
        else:
            stats.taker += 1
        record = self.oms.orders.get(fill.intent_id or "")
        purpose = record.intent.purpose if record else IntentPurpose.PASSIVE_QUOTE
        stats.by_purpose[purpose.value] += 1
        stats.gross_shares += fill.gross_shares
        stats.notional += fill.gross_shares * fill.price
        stats.fees_usd_equivalent += fill.fee_cash + fill.fee_shares * fill.price
        if purpose in (IntentPurpose.PROFITABLE_HEDGE, IntentPurpose.RISK_REDUCING_HEDGE):
            stats.hedge_cost += fill.gross_shares * fill.price + fill.fee_cash
        for horizon in MARKOUT_HORIZONS_MS:
            self.scheduler.at(
                fill.ts_utc_ms + horizon, PRIORITY_TIMER, self._markout, fill, horizon
            )

    def _markout(self, fill: FillRecord, horizon: int) -> None:
        book = self.exchange.books.books.get(fill.asset_id)
        spec = self.specs.get(fill.market_id)
        if book is None or spec is None or self.scheduler.now >= spec.end_utc_ms:
            return
        mid = book_mid(book.state())
        if mid is None:
            return
        move = mid - fill.price
        self.fill_stats.markouts[horizon].append(move if fill.side is Side.BUY else -move)

    def _sample_equity(self) -> None:
        now = self.scheduler.now
        position_value = ZERO
        exposed = False
        for spec in self.specs.values():
            if self.account.flows[spec.market_id].settled:
                continue
            inventory = self.account.inventory(spec)
            if inventory.yes_net == 0 and inventory.no_net == 0:
                continue
            exposed = exposed or inventory.unpaired_yes > 0 or inventory.unpaired_no > 0
            for asset, quantity in (
                (spec.yes_asset_id, inventory.yes_net),
                (spec.no_asset_id, inventory.no_net),
            ):
                book = self.exchange.books.books.get(asset)
                mid = book_mid(book.state()) if book else None
                if mid is not None:
                    position_value += quantity * mid
        equity = self.account.total_cash + self.account.cash_redeemable + position_value
        self.timeline.append(
            {
                "ts_utc_ms": now,
                "equity": equity,
                "cash_available": self.account.cash_available,
                "cash_reserved": self.account.cash_reserved,
                "cash_redeemable": self.account.cash_redeemable,
            }
        )
        self._sampled_ms += EQUITY_SAMPLE_MS
        if exposed:
            self._exposed_ms += EQUITY_SAMPLE_MS
        if now + EQUITY_SAMPLE_MS <= self._last_data_ms:
            self.scheduler.at(now + EQUITY_SAMPLE_MS, PRIORITY_TIMER, self._sample_equity)
        else:
            self._sampler_started = False

    def finish(self) -> None:
        """Run to the end of data. Nothing is force-settled: unresolved markets stay reported."""
        self.run_until(None)
        for spec in self.specs.values():
            if spec.market_id in self.resolved:
                continue
            inventory = self.account.inventory(spec)
            if inventory.yes_net or inventory.no_net:
                self.unresolved.setdefault(spec.market_id, "no_official_resolution_in_data")

    def order_state_counts(self) -> dict[str, int]:
        return dict(Counter(o.state.value for o in self.oms.orders.values()))

    def order_purpose_counts(self) -> dict[str, int]:
        return dict(Counter(o.intent.purpose.value for o in self.oms.orders.values()))

    def latency_report(self) -> dict[str, dict[str, int | None]]:
        return {name: percentiles(values) for name, values in self.latency.samples.items()}

    def book_validity_counts(self) -> dict[str, int]:
        return dict(Counter(b.validity.value for b in self.visible.books.values()))

    def all_books_ready(self) -> bool:
        return all(b.validity is BookValidity.READY for b in self.visible.books.values())

    def rejected_orders(self) -> int:
        return sum(1 for o in self.oms.orders.values() if o.state is OrderState.RISK_REJECTED)


def replay_events(
    events: Iterable[EventEnvelope],
    settings: Settings,
    scenario: SimScenario | None = None,
    *,
    seed: int | None = None,
    market_filter: Callable[[str], bool] | None = None,
    strategy_id: str = "baseline",
    probability_factory: Callable[[SimCore], ProbabilityFn] | None = None,
    model_hash: str | None = None,
) -> SimCore:
    core = SimCore(
        settings,
        scenario,
        seed=seed,
        strategy_id=strategy_id,
        model_hash=model_hash,
    )
    if probability_factory is not None:
        core.orchestrator.probability = probability_factory(core)
    for env in events:
        if market_filter is not None and env.market_id and not market_filter(env.market_id):
            continue
        core.schedule_envelope(env)
        # Keep the queue small. The trailing window exceeds any real source-to-receive lag, so
        # exchange events are still scheduled before simulated time reaches them.
        core.run_until(env.received_utc_ms - REPLAY_WINDOW_MS)
    core.finish()
    return core
