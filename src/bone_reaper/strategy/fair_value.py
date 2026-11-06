"""Quote-side fair value. Defaults to the book midpoint; a validated model needs an explicit flag.

Model output is advisory and bounded: any error or missing input falls back to the midpoint, and a
non-finite or out-of-range number can never reach a quote.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from ..types import ONE, BookState

MODEL_MIN = Decimal("0.01")
MODEL_MAX = Decimal("0.99")


@dataclass(frozen=True, slots=True)
class FairValue:
    yes: Decimal
    no: Decimal
    source: str
    model_hash: str | None = None


def book_mid(book: BookState) -> Decimal | None:
    if book.best_bid is None or book.best_ask is None:
        return None
    return (book.best_bid + book.best_ask) / 2


def midpoint_fair(yes_book: BookState, no_book: BookState) -> FairValue | None:
    """Fair value from both books. A single stale book never defines the probability."""
    yes_mid = book_mid(yes_book)
    no_mid = book_mid(no_book)
    if yes_mid is None or no_mid is None:
        return None
    total = yes_mid + no_mid
    if total <= 0:
        return None
    return FairValue(yes=yes_mid / total, no=no_mid / total, source="book_midpoint")


ProbabilityFn = Callable[[], float | None]


def sanitize_probability(value: float | None) -> Decimal | None:
    if value is None or not math.isfinite(value):
        return None
    if not 0.0 < value < 1.0:
        return None
    return min(max(Decimal(str(round(value, 6))), MODEL_MIN), MODEL_MAX)


def model_fair(
    yes_book: BookState,
    no_book: BookState,
    probability: ProbabilityFn | None,
    model_enabled: bool,
    model_hash: str | None,
) -> FairValue | None:
    """The model changes quotes only with the explicit flag, and it falls back on any failure."""
    fallback = midpoint_fair(yes_book, no_book)
    if not model_enabled or probability is None:
        return fallback
    try:
        p_up = sanitize_probability(probability())
    except Exception:
        return fallback
    if p_up is None:
        return fallback
    return FairValue(yes=p_up, no=ONE - p_up, source="model", model_hash=model_hash)
