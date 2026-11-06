"""Freshness and independent-snapshot checks over immutable :class:`BookState` values."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..types import BookState, BookValidity, PriceLevel


def book_age_ms(book: BookState, now_utc_ms: int) -> int | None:
    if book.last_update_utc_ms is None:
        return None
    return now_utc_ms - book.last_update_utc_ms


def book_problems(book: BookState, now_utc_ms: int, max_age_ms: int) -> list[str]:
    """Reasons this book may not be used for a decision. Empty means usable."""
    problems: list[str] = []
    if book.validity is BookValidity.UNINITIALIZED:
        problems.append("book_uninitialized")
    elif book.validity is BookValidity.SYNCING:
        problems.append("book_syncing")
    elif book.validity is BookValidity.INVALID:
        problems.append(f"book_invalid:{book.invalid_reason}")
    age = book_age_ms(book, now_utc_ms)
    if age is None:
        problems.append("book_no_updates")
    elif age > max_age_ms:
        problems.append("book_stale")
    elif age < 0:
        problems.append("book_from_future")
    return problems


@dataclass(frozen=True, slots=True)
class SnapshotComparison:
    matches: bool
    bid_level_diffs: int
    ask_level_diffs: int
    max_size_diff: Decimal
    top_of_book_equal: bool


def _diff(a: tuple[PriceLevel, ...], b: tuple[PriceLevel, ...]) -> tuple[int, Decimal]:
    left = {level.price: level.size for level in a}
    right = {level.price: level.size for level in b}
    diffs = 0
    largest = Decimal(0)
    for price in set(left) | set(right):
        delta = abs(left.get(price, Decimal(0)) - right.get(price, Decimal(0)))
        if delta > 0:
            diffs += 1
            largest = max(largest, delta)
    return diffs, largest


def compare_snapshots(
    rebuilt: BookState, independent: BookState, tolerance_levels: int = 0
) -> SnapshotComparison:
    """Compare a rebuilt book with an independent REST sample (FR-04)."""
    bid_diffs, bid_max = _diff(rebuilt.bids, independent.bids)
    ask_diffs, ask_max = _diff(rebuilt.asks, independent.asks)
    top_equal = (
        rebuilt.best_bid == independent.best_bid and rebuilt.best_ask == independent.best_ask
    )
    return SnapshotComparison(
        matches=bid_diffs + ask_diffs <= tolerance_levels,
        bid_level_diffs=bid_diffs,
        ask_level_diffs=ask_diffs,
        max_size_diff=max(bid_max, ask_max),
        top_of_book_equal=top_equal,
    )
