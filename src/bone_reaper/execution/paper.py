"""Deterministic simulated exchange and the paper execution adapter.

The exchange sees exchange-time books; the strategy only ever sees bot-visible books. Requests and
notices travel with simulated latency. Every simulated fill is flagged ``simulated=True``.
"""

from __future__ import annotations

import heapq
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from ..market.book import BookManager
from ..research.latency import LatencyModel
from ..research.maker_queue import QueuePosition, QueueScenario
from ..research.taker_fill import DepthOverlay, match_taker
from ..strategy.fees import buy_fill_economics, sell_fill_economics
from ..types import (
    ONE,
    ZERO,
    FillRecord,
    FillStatus,
    LiquidityRole,
    MarketSpec,
    OrderState,
    OrderType,
    Outcome,
    QuoteIntent,
    Side,
)
from .ports import (
    AccountSnapshot,
    CancelResult,
    CancelStatus,
    RemoteOrder,
    SubmitResult,
    SubmitStatus,
)

PRIORITY_EXCHANGE = 0
PRIORITY_NOTICE = 1
PRIORITY_VISIBLE = 2
PRIORITY_TIMER = 3


class EventScheduler:
    """Deterministic priority queue: (time, priority, insertion order)."""

    def __init__(self) -> None:
        self._heap: list[tuple[int, int, int, Callable[..., None], tuple[Any, ...]]] = []
        self._counter = 0
        self.now = 0

    def at(self, when: int, priority: int, fn: Callable[..., None], *args: Any) -> None:
        self._counter += 1
        heapq.heappush(self._heap, (when, priority, self._counter, fn, args))

    def __len__(self) -> int:
        return len(self._heap)

    def run_until(self, until: int | None, on_time: Callable[[int], None]) -> None:
        while self._heap and (until is None or self._heap[0][0] <= until):
            when, _priority, _count, fn, args = heapq.heappop(self._heap)
            self.now = max(self.now, when)
            on_time(self.now)
            fn(*args)
        if until is not None and until > self.now:
            # Time passes even when nothing is due; anything sent next must carry the real time.
            self.now = until
            on_time(until)


class ExchangeListener(Protocol):
    def on_ack(self, intent_id: str, exchange_order_id: str | None) -> Any: ...
    def on_reject(self, intent_id: str, reason: str) -> Any: ...
    def on_fill(self, fill: FillRecord) -> Any: ...
    def on_cancel_confirmed(self, intent_id: str) -> Any: ...
    def on_cancel_rejected(self, intent_id: str, reason: str) -> Any: ...
    def on_expired(self, intent_id: str) -> Any: ...


@dataclass(slots=True)
class RestingOrder:
    intent: QuoteIntent
    order_id: str
    queue: QueuePosition
    matched: Decimal = ZERO


@dataclass(slots=True)
class ExchangeTruth:
    """The simulated venue's own record, kept independently of the bot's ledger."""

    cash: Decimal
    positions: dict[str, Decimal] = field(default_factory=dict)
    trades: list[FillRecord] = field(default_factory=list)
    redeemable: dict[str, Decimal] = field(default_factory=dict)


class SimExchange:
    def __init__(
        self,
        scheduler: EventScheduler,
        latency: LatencyModel,
        scenario: QueueScenario,
        starting_cash: Decimal,
    ) -> None:
        self.scheduler = scheduler
        self.latency = latency
        self.scenario = scenario
        self.books = BookManager()
        self.specs: dict[str, MarketSpec] = {}
        self.asset_market: dict[str, str] = {}
        self.overlay = DepthOverlay()
        self.resting: dict[str, RestingOrder] = {}
        self.truth = ExchangeTruth(cash=starting_cash)
        self.listener: ExchangeListener | None = None
        self.on_sim_fill: Callable[[FillRecord], None] | None = None
        self._order_counter = 0
        self._trade_counter = 0
        self._seen_trades: set[tuple[str, str, str]] = set()
        self._last_notice: dict[str, int] = {}
        self.stats: dict[str, int] = {
            "arrivals": 0,
            "post_only_rejects": 0,
            "fok_kills": 0,
            "cancels_too_late": 0,
            "trade_events": 0,
            "trade_duplicates": 0,
        }

    def register(self, spec: MarketSpec) -> None:
        self.specs[spec.market_id] = spec
        for asset in (spec.yes_asset_id, spec.no_asset_id):
            self.books.register(spec.market_id, asset, spec.tick_size)
            self.asset_market[asset] = spec.market_id

    def _notify(self, when: int, fn: Callable[..., Any], *args: Any) -> None:
        """Notices for one order stay in order: jitter must not deliver an expiry before an ACK."""
        subject = str(args[0].intent_id if isinstance(args[0], FillRecord) else args[0])
        at = max(when + self.latency.ack(), self._last_notice.get(subject, 0))
        self._last_notice[subject] = at
        self.scheduler.at(at, PRIORITY_NOTICE, fn, *args)

    def _listener(self) -> ExchangeListener:
        if self.listener is None:
            raise RuntimeError("exchange has no listener")
        return self.listener

    # --- order arrival ------------------------------------------------------------------------

    def send_order(self, intent: QuoteIntent, now: int) -> None:
        self.scheduler.at(now + self.latency.submit(), PRIORITY_EXCHANGE, self._arrive, intent)

    def _arrive(self, intent: QuoteIntent) -> None:
        now = self.scheduler.now
        self.stats["arrivals"] += 1
        spec = self.specs.get(intent.market_id)
        listener = self._listener()
        if spec is None or now >= spec.end_utc_ms:
            self._notify(now, listener.on_reject, intent.intent_id, "market_closed")
            return
        book = self.books.books[intent.asset_id].state()
        if intent.price % book.tick_size != 0 or intent.size < spec.min_order_shares:
            self._notify(now, listener.on_reject, intent.intent_id, "invalid_order_params")
            return
        crosses = (
            book.best_ask is not None and intent.price >= book.best_ask
            if intent.side is Side.BUY
            else book.best_bid is not None and intent.price <= book.best_bid
        )
        self._order_counter += 1
        order_id = f"sim-order-{self._order_counter}"
        if intent.post_only and crosses:
            self.stats["post_only_rejects"] += 1
            self._notify(now, listener.on_reject, intent.intent_id, "post_only_would_cross")
            return
        matched = ZERO
        if crosses:
            matches = match_taker(
                book, intent.side, intent.price, intent.size, intent.order_type, self.overlay
            )
            if not matches and intent.order_type is OrderType.FOK:
                self.stats["fok_kills"] += 1
                self._notify(now, listener.on_reject, intent.intent_id, "fok_not_filled")
                return
            self._notify(now, listener.on_ack, intent.intent_id, order_id)
            for match in matches:
                self._emit_fill(
                    intent, order_id, match.price, match.gross_shares, LiquidityRole.TAKER, spec
                )
                matched += match.gross_shares
        elif intent.order_type is not OrderType.GTC:
            self.stats["fok_kills"] += 1
            self._notify(now, listener.on_reject, intent.intent_id, "fok_not_marketable")
            return
        else:
            self._notify(now, listener.on_ack, intent.intent_id, order_id)
        remaining = intent.size - matched
        if remaining <= 0:
            return
        if intent.order_type is not OrderType.GTC:
            self._notify(now, listener.on_expired, intent.intent_id)
            return
        levels = book.bids if intent.side is Side.BUY else book.asks
        visible = next((lv.size for lv in levels if lv.price == intent.price), ZERO)
        self.resting[intent.intent_id] = RestingOrder(
            intent,
            order_id,
            QueuePosition.join(intent.price, remaining, visible, self.scenario),
            matched,
        )

    # --- cancellation -------------------------------------------------------------------------

    def send_cancel(self, intent_id: str, now: int) -> None:
        self.scheduler.at(now + self.latency.cancel(), PRIORITY_EXCHANGE, self._cancel, intent_id)

    def _cancel(self, intent_id: str) -> None:
        now = self.scheduler.now
        listener = self._listener()
        if self.resting.pop(intent_id, None) is not None:
            self._notify(now, listener.on_cancel_confirmed, intent_id)
        else:
            # Already filled or expired. The fill notice may race this rejection.
            self.stats["cancels_too_late"] += 1
            self._notify(now, listener.on_cancel_rejected, intent_id, "order_not_open")

    def expire_market(self, market_id: str) -> None:
        now = self.scheduler.now
        for intent_id in [k for k, o in self.resting.items() if o.intent.market_id == market_id]:
            del self.resting[intent_id]
            self._notify(now, self._listener().on_expired, intent_id)

    # --- market data --------------------------------------------------------------------------

    def on_market_event(self, payload: dict[str, Any], recv_utc_ms: int) -> None:
        kind = payload.get("event_type")
        if kind == "book":
            asset = str(payload.get("asset_id"))
            self.overlay.venue_snapshot(asset)
            self.books.apply(payload, recv_utc_ms)
            self._refresh_levels(asset)
        elif kind == "price_change":
            self.books.apply(payload, recv_utc_ms)
            for change in payload.get("price_changes", ()):
                asset = str(change.get("asset_id"))
                side = str(change["side"])
                price = Decimal(change["price"])
                self.overlay.venue_update(asset, side, price)
                self._level_changed(asset, side, price, Decimal(change["size"]))
        elif kind == "tick_size_change":
            self.books.apply(payload, recv_utc_ms)
        elif kind == "last_trade_price":
            self._on_trade(payload)

    def _refresh_levels(self, asset: str) -> None:
        book = self.books.books.get(asset)
        if book is None:
            return
        for order in self.resting.values():
            if order.intent.asset_id != asset:
                continue
            levels = book.bids if order.intent.side is Side.BUY else book.asks
            order.queue.on_level_size(levels.get(order.intent.price, ZERO))

    def _level_changed(self, asset: str, side: str, price: Decimal, size: Decimal) -> None:
        for order in self.resting.values():
            if (
                order.intent.asset_id == asset
                and order.intent.side.value == side
                and order.intent.price == price
            ):
                order.queue.on_level_size(size)

    def _on_trade(self, payload: dict[str, Any]) -> None:
        asset = str(payload.get("asset_id"))
        market_id = self.asset_market.get(asset)
        if market_id is None or payload.get("size") is None:
            return
        spec = self.specs[market_id]
        price = Decimal(payload["price"])
        size = Decimal(payload["size"])
        side = str(payload["side"])
        tx = payload.get("transaction_hash")
        yes_price = price if asset == spec.yes_asset_id else ONE - price
        if tx:
            # The same match can be reported on both outcome tokens. Count it once.
            key = (str(tx), str(size), str(yes_price))
            if key in self._seen_trades:
                self.stats["trade_duplicates"] += 1
                return
            self._seen_trades.add(key)
        self.stats["trade_events"] += 1
        for intent_id, order in list(self.resting.items()):
            if order.intent.market_id != market_id:
                continue
            taker_side, trade_price = self._view_on(order.intent.asset_id, asset, side, price)
            self._apply_trade_to_order(intent_id, order, taker_side, trade_price, size, spec)

    @staticmethod
    def _view_on(
        order_asset: str, trade_asset: str, side: str, price: Decimal
    ) -> tuple[str, Decimal]:
        """A BUY of one outcome at p is the same liquidity as a SELL of the other at 1-p."""
        if order_asset == trade_asset:
            return side, price
        return ("SELL" if side == "BUY" else "BUY"), ONE - price

    def _apply_trade_to_order(
        self,
        intent_id: str,
        order: RestingOrder,
        taker_side: str,
        trade_price: Decimal,
        size: Decimal,
        spec: MarketSpec,
    ) -> None:
        intent = order.intent
        if intent.side is Side.BUY:
            if taker_side != "SELL" or trade_price > intent.price:
                return
            through = trade_price < intent.price
        else:
            if taker_side != "BUY" or trade_price < intent.price:
                return
            through = trade_price > intent.price
        fill = order.queue.on_trade_through() if through else order.queue.on_trade_at_price(size)
        if fill > 0:
            order.matched += fill
            self._emit_fill(intent, order.order_id, intent.price, fill, LiquidityRole.MAKER, spec)
        if order.queue.remaining <= 0:
            self.resting.pop(intent_id, None)

    # --- fills --------------------------------------------------------------------------------

    def _emit_fill(
        self,
        intent: QuoteIntent,
        order_id: str,
        price: Decimal,
        gross: Decimal,
        role: LiquidityRole,
        spec: MarketSpec,
    ) -> None:
        now = self.scheduler.now
        self._trade_counter += 1
        positions = self.truth.positions
        if intent.side is Side.BUY:
            net, fee, cash = buy_fill_economics(gross, price, spec.fee, role)
            self.truth.cash -= cash
            positions[intent.asset_id] = positions.get(intent.asset_id, ZERO) + net
            cash_delta = -cash
        else:
            fee, cash = sell_fill_economics(gross, price, spec.fee, role)
            net = gross
            self.truth.cash += cash
            positions[intent.asset_id] = positions.get(intent.asset_id, ZERO) - gross
            cash_delta = cash
        fill = FillRecord(
            trade_id=f"sim-trade-{self._trade_counter}",
            leg_key=order_id,
            intent_id=intent.intent_id,
            exchange_order_id=order_id,
            market_id=intent.market_id,
            asset_id=intent.asset_id,
            outcome=intent.outcome,
            side=intent.side,
            role=role,
            price=price,
            gross_shares=gross,
            net_shares=net,
            fee_cash=fee.fee_cash,
            fee_shares=fee.fee_shares,
            cash_delta=cash_delta,
            status=FillStatus.MATCHED,
            simulated=True,
            ts_utc_ms=now,
        )
        self.truth.trades.append(fill)
        if self.on_sim_fill is not None:
            self.on_sim_fill(fill)
        self._notify(now, self._listener().on_fill, fill)

    # --- settlement truth ----------------------------------------------------------------------

    def settle_truth(self, spec: MarketSpec, winning: Outcome) -> None:
        """Shares leave the positions at settlement; cash arrives only at redemption."""
        winner = self.truth.positions.pop(spec.asset_id(winning), ZERO)
        self.truth.positions.pop(spec.asset_id(winning.other), None)
        self.truth.redeemable[spec.market_id] = winner

    def redeem_truth(self, spec: MarketSpec) -> None:
        self.truth.cash += self.truth.redeemable.pop(spec.market_id, ZERO)

    def snapshot(self) -> AccountSnapshot:
        return AccountSnapshot(
            open_orders=tuple(
                RemoteOrder(
                    exchange_order_id=o.order_id,
                    intent_id=o.intent.intent_id,
                    state=OrderState.PARTIALLY_FILLED if o.matched > 0 else OrderState.OPEN,
                    matched_gross=o.matched,
                )
                for o in self.resting.values()
            ),
            trades=tuple(self.truth.trades),
            cash_balance=self.truth.cash,
            positions=tuple(sorted(self.truth.positions.items())),
        )


class PaperExecution:
    """Execution port for the simulated exchange. No network path exists from this class."""

    is_real = False

    def __init__(self, exchange: SimExchange) -> None:
        self.exchange = exchange
        self.submitted: list[str] = []

    def submit(self, intent: QuoteIntent) -> SubmitResult:
        self.submitted.append(intent.intent_id)
        self.exchange.send_order(intent, self.exchange.scheduler.now)
        return SubmitResult(SubmitStatus.PENDING)

    def submit_batch(self, intents: Sequence[QuoteIntent]) -> list[SubmitResult]:
        return [self.submit(intent) for intent in intents]

    def cancel(self, intent_id: str, exchange_order_id: str | None) -> CancelResult:
        self.exchange.send_cancel(intent_id, self.exchange.scheduler.now)
        return CancelResult(CancelStatus.PENDING)

    def account_snapshot(self) -> AccountSnapshot:
        return self.exchange.snapshot()
