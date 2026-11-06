"""Risk limits resolved from settings, plus the UTC-day realized-loss tracker."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..settings import Settings
from ..types import ZERO

MS_PER_DAY = 86_400_000


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_order_notional: Decimal
    max_unpaired_notional: Decimal
    max_total_gross_notional: Decimal
    max_daily_realized_loss: Decimal
    max_open_orders: int
    stop_seconds_before_close: int
    max_book_age_ms: int
    max_reference_age_ms: int
    max_clock_skew_ms: int
    max_unexplained_reconciliation: Decimal
    require_reference: bool
    allow_negative_edge_hedge: bool
    max_hedge_loss: Decimal

    @classmethod
    def from_settings(cls, settings: Settings) -> RiskLimits:
        r = settings.risk
        return cls(
            max_order_notional=r.max_order_notional_usd,
            max_unpaired_notional=r.max_unpaired_notional_usd,
            max_total_gross_notional=r.max_total_gross_notional_usd,
            max_daily_realized_loss=r.max_daily_realized_loss_usd,
            max_open_orders=r.max_open_orders,
            stop_seconds_before_close=r.stop_new_quotes_seconds_before_close,
            max_book_age_ms=r.max_book_age_ms,
            max_reference_age_ms=r.max_reference_age_ms,
            max_clock_skew_ms=r.max_clock_skew_ms,
            max_unexplained_reconciliation=r.max_unexplained_reconciliation_usd,
            require_reference=settings.strategies.probability_model,
            allow_negative_edge_hedge=settings.strategies.allow_negative_edge_risk_hedge,
            max_hedge_loss=settings.strategies.max_hedge_loss_usd,
        )


class DailyLossTracker:
    """Realized PnL resets at UTC midnight for the stop. A latched kill stays latched regardless.

    The first observation keeps a zero baseline rather than today's total: losses made before the
    process (re)started must still count, or a restart would silently disarm the stop.
    """

    def __init__(self) -> None:
        self._day: int | None = None
        self._baseline = ZERO

    def realized_loss_today(self, realized_total: Decimal, now_utc_ms: int) -> Decimal:
        day = now_utc_ms // MS_PER_DAY
        if self._day is None:
            self._day = day
        elif self._day != day:
            self._day = day
            self._baseline = realized_total
        today = realized_total - self._baseline
        return -today if today < 0 else ZERO
