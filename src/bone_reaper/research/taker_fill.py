"""Depth-aware taker matching at venue-arrival time.

Only the exchange-time book matters here. A stale ask the bot saw cannot fill if it vanished before
the order arrived. Depth we consumed stays consumed until the venue next updates that level.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from ..types import ZERO, BookState, OrderType, PriceLevel, Side


@dataclass(frozen=True, slots=True)
class TakerMatch:
    price: Decimal
    gross_shares: Decimal


@dataclass(slots=True)
class DepthOverlay:
    """Our own consumption per (asset, side, price), cleared when the venue reports that level."""

    consumed: dict[tuple[str, str, Decimal], Decimal] = field(default_factory=dict)

    def available(self, asset_id: str, side: str, level: PriceLevel) -> Decimal:
        return max(level.size - self.consumed.get((asset_id, side, level.price), ZERO), ZERO)

    def consume(self, asset_id: str, side: str, price: Decimal, shares: Decimal) -> None:
        key = (asset_id, side, price)
        self.consumed[key] = self.consumed.get(key, ZERO) + shares

    def venue_update(self, asset_id: str, side: str, price: Decimal) -> None:
        self.consumed.pop((asset_id, side, price), None)

    def venue_snapshot(self, asset_id: str) -> None:
        for key in [k for k in self.consumed if k[0] == asset_id]:
            del self.consumed[key]


def match_taker(
    book: BookState,
    side: Side,
    limit_price: Decimal,
    size: Decimal,
    order_type: OrderType,
    overlay: DepthOverlay,
) -> list[TakerMatch]:
    """Consume exchange depth up to the limit. FOK returns nothing unless all of it can fill."""
    levels = book.asks if side is Side.BUY else book.bids
    book_side = "SELL" if side is Side.BUY else "BUY"
    matches: list[TakerMatch] = []
    remaining = size
    for level in levels:
        if remaining <= 0:
            break
        crosses = level.price <= limit_price if side is Side.BUY else level.price >= limit_price
        if not crosses:
            break
        available = overlay.available(book.asset_id, book_side, level)
        take = min(available, remaining)
        if take > 0:
            matches.append(TakerMatch(level.price, take))
            remaining -= take
    if order_type is OrderType.FOK and remaining > 0:
        return []
    for match in matches:
        overlay.consume(book.asset_id, book_side, match.price, match.gross_shares)
    return matches
