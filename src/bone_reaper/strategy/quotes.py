"""Passive dual-outcome quote generation, sizing and refresh rules (Architecture 7.2)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from ..risk.exposure import Exposure
from ..types import ONE, ZERO, BookState, InventorySnapshot, MarketSpec, OrderRecord, Outcome
from .fair_value import FairValue
from .fees import fee_usd

SHARE_LOT = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class QuoteParams:
    size_shares: Decimal
    margin: Decimal
    pair_buffer: Decimal
    min_price: Decimal
    max_price: Decimal
    max_order_notional: Decimal
    max_unpaired_notional: Decimal
    max_total_gross_notional: Decimal
    stop_seconds_before_close: int


@dataclass(frozen=True, slots=True)
class TargetQuote:
    outcome: Outcome
    price: Decimal
    size: Decimal


@dataclass(frozen=True, slots=True)
class QuotePlan:
    quotes: tuple[TargetQuote, ...]
    skipped: tuple[tuple[str, str], ...]


def round_down_tick(price: Decimal, tick: Decimal) -> Decimal:
    return (price / tick).to_integral_value(rounding=ROUND_DOWN) * tick


def round_down_lot(size: Decimal) -> Decimal:
    return (size / SHARE_LOT).to_integral_value(rounding=ROUND_DOWN) * SHARE_LOT


def _expected_hedge_cost(book: BookState, spec: MarketSpec) -> Decimal | None:
    """All-in cost per share of taking the opposite outcome at its best ask."""
    if book.best_ask is None:
        return None
    return book.best_ask + fee_usd(ONE, book.best_ask, spec.fee)


def target_price(
    own: BookState,
    other: BookState,
    fair: Decimal,
    spec: MarketSpec,
    params: QuoteParams,
) -> tuple[Decimal | None, str | None]:
    if own.best_bid is None or own.best_ask is None:
        return None, "own_book_one_sided"
    hedge_cost = _expected_hedge_cost(other, spec)
    if hedge_cost is None:
        return None, "no_counterpart_liquidity"
    # The live tick comes from the book: the venue narrows it near price extremes.
    tick = own.tick_size
    proposed = min(
        own.best_bid + tick,
        fair - params.margin,
        ONE - hedge_cost - params.pair_buffer,
    )
    price = round_down_tick(proposed, tick)
    if price < params.min_price:
        return None, "below_min_viable_price"
    if price > params.max_price:
        return None, "above_max_quote_price"
    if price >= own.best_ask:
        return None, "would_cross_post_only"
    return price, None


def size_quote(
    outcome: Outcome,
    price: Decimal,
    spec: MarketSpec,
    params: QuoteParams,
    inventory: InventorySnapshot,
    exposure: Exposure,
    cash_budget: Decimal,
) -> tuple[Decimal, str | None]:
    """Largest size that keeps worst-case limits even if the other side fills concurrently."""
    own_net = inventory.yes_net if outcome is Outcome.YES else inventory.no_net
    other_net = inventory.no_net if outcome is Outcome.YES else inventory.yes_net
    # Passive counterpart first: a side that is short of the other may grow to close the gap.
    imbalance = other_net - own_net
    desired = max(params.size_shares, imbalance) if imbalance > 0 else params.size_shares
    if imbalance < 0:
        # This side already leads. Stop adding to it until the other side catches up.
        return ZERO, "inventory_skew_side_already_long"

    limit = desired
    limit = min(limit, params.max_order_notional / price)
    limit = min(limit, cash_budget / price)
    gross_room = params.max_total_gross_notional - exposure.gross_notional
    limit = min(limit, gross_room / price)
    unpaired_room = params.max_unpaired_notional - exposure.worst_unpaired_notional
    if imbalance <= 0:
        limit = min(limit, unpaired_room / price)
    size = round_down_lot(limit)
    if size < spec.min_order_shares:
        return ZERO, "size_below_minimum_after_limits"
    return size, None


def build_quote_plan(
    spec: MarketSpec,
    yes_book: BookState,
    no_book: BookState,
    fair: FairValue,
    params: QuoteParams,
    inventory: InventorySnapshot,
    exposure: Exposure,
    cash_available: Decimal,
    now_utc_ms: int,
) -> QuotePlan:
    if now_utc_ms >= spec.end_utc_ms - params.stop_seconds_before_close * 1000:
        return QuotePlan((), (("market", "quote_cutoff"),))
    skipped: list[tuple[str, str]] = []
    quotes: list[TargetQuote] = []
    # Each side's budget assumes the other side's order is also funded.
    budget_left = cash_available
    for outcome, own, other, side_fair in (
        (Outcome.YES, yes_book, no_book, fair.yes),
        (Outcome.NO, no_book, yes_book, fair.no),
    ):
        price, reason = target_price(own, other, side_fair, spec, params)
        if price is None:
            skipped.append((outcome.value, reason or "no_price"))
            continue
        size, reason = size_quote(outcome, price, spec, params, inventory, exposure, budget_left)
        if size <= 0:
            skipped.append((outcome.value, reason or "no_size"))
            continue
        budget_left -= price * size
        quotes.append(TargetQuote(outcome, price, size))
    if len(quotes) == 2 and quotes[0].price + quotes[1].price > ONE - params.pair_buffer:
        return QuotePlan((), (("pair", "combined_cost_unsafe"), *skipped))
    return QuotePlan(tuple(quotes), tuple(skipped))


@dataclass(frozen=True, slots=True)
class RefreshRules:
    min_ticks: int
    min_age_ms: int


def needs_refresh(
    order: OrderRecord,
    target: TargetQuote | None,
    tick: Decimal,
    rules: RefreshRules,
    now_utc_ms: int,
) -> bool:
    """Only a material drift replaces a quote. Trivial tick noise does not cause a cancel storm."""
    if target is None:
        return True
    age = now_utc_ms - order.created_utc_ms
    if age < rules.min_age_ms:
        return False
    return abs(target.price - order.intent.price) >= tick * rules.min_ticks
