"""Deterministic alert rules and a Prometheus-style text export (Architecture section 12)."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from decimal import Decimal


class Severity(enum.StrEnum):
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class HealthInputs:
    """A plain snapshot, so every rule can be tested without any running service."""

    now_utc_ms: int
    book_age_ms: int | None = None
    reference_age_ms: int | None = None
    max_book_age_ms: int = 1000
    max_reference_age_ms: int = 2000
    reference_required: bool = False
    rollover_failures: int = 0
    oracle_mismatch: bool = False
    user_stream_silence_ms: int | None = None
    max_user_stream_silence_ms: int = 5000
    unknown_orders: int = 0
    open_orders: int = 0
    unpaired_notional: Decimal = Decimal(0)
    max_unpaired_notional: Decimal = Decimal(20)
    realized_loss_today: Decimal = Decimal(0)
    max_daily_loss: Decimal = Decimal(10)
    ledger_errors: int = 0
    reconciliation_diff: Decimal = Decimal(0)
    max_reconciliation_diff: Decimal = Decimal("0.01")
    disk_write_failed: bool = False
    geoblock_changed: bool = False
    clock_offset_ms: int | None = None
    max_clock_offset_ms: int = 1000
    queue_depth: int = 0
    max_queue_depth: int = 900
    rate_limited: bool = False
    kill_switch_active: bool = False
    data_gaps: int = 0


@dataclass(frozen=True, slots=True)
class Alert:
    code: str
    severity: Severity
    detail: str


def evaluate_alerts(h: HealthInputs) -> list[Alert]:
    alerts: list[Alert] = []

    def add(code: str, severity: Severity, detail: str) -> None:
        alerts.append(Alert(code, severity, detail))

    if h.rollover_failures:
        add("rollover_failure", Severity.CRITICAL, f"{h.rollover_failures} failures")
    if h.book_age_ms is None or h.book_age_ms > h.max_book_age_ms:
        add("stale_feed", Severity.CRITICAL, f"book age {h.book_age_ms}ms")
    if h.reference_required and (
        h.reference_age_ms is None or h.reference_age_ms > h.max_reference_age_ms
    ):
        add("stale_reference", Severity.CRITICAL, f"reference age {h.reference_age_ms}ms")
    if h.oracle_mismatch:
        add("oracle_mismatch", Severity.CRITICAL, "market oracle differs from the reviewed rules")
    if (
        h.user_stream_silence_ms is not None
        and h.user_stream_silence_ms > h.max_user_stream_silence_ms
    ):
        add("account_stream_silence", Severity.CRITICAL, f"{h.user_stream_silence_ms}ms")
    if h.unknown_orders:
        add("unknown_orders", Severity.CRITICAL, f"{h.unknown_orders} orders in unknown state")
    if h.unpaired_notional > h.max_unpaired_notional:
        add("oversized_unpaired", Severity.CRITICAL, f"{h.unpaired_notional}")
    if h.realized_loss_today >= h.max_daily_loss:
        add("daily_loss_stop", Severity.CRITICAL, f"{h.realized_loss_today}")
    if h.ledger_errors or abs(h.reconciliation_diff) > h.max_reconciliation_diff:
        add(
            "ledger_discrepancy",
            Severity.CRITICAL,
            f"errors={h.ledger_errors} diff={h.reconciliation_diff}",
        )
    if h.disk_write_failed:
        add("disk_write_error", Severity.CRITICAL, "raw or journal write failed")
    if h.geoblock_changed:
        add("geoblock_change", Severity.CRITICAL, "eligibility result changed")
    if h.clock_offset_ms is not None and abs(h.clock_offset_ms) > h.max_clock_offset_ms:
        add("clock_skew", Severity.CRITICAL, f"{h.clock_offset_ms}ms")
    if h.queue_depth > h.max_queue_depth:
        add("queue_backlog", Severity.WARNING, f"depth {h.queue_depth}")
    if h.rate_limited:
        add("rate_limited", Severity.WARNING, "venue returned a rate limit response")
    if h.data_gaps:
        add("data_gaps", Severity.WARNING, f"{h.data_gaps} gaps")
    if h.kill_switch_active:
        add("kill_switch_active", Severity.CRITICAL, "new risk is stopped")
    return alerts


def prometheus_text(h: HealthInputs) -> str:
    def number(value: object) -> str:
        return "NaN" if value is None else str(value)

    rows = {
        "market_book_age_ms": h.book_age_ms,
        "reference_age_ms": h.reference_age_ms,
        "event_queue_depth": h.queue_depth,
        "orders_open": h.open_orders,
        "orders_unknown": h.unknown_orders,
        "unpaired_notional_usd": h.unpaired_notional,
        "reconciliation_diff_usd": h.reconciliation_diff,
        "kill_switch_active": int(h.kill_switch_active),
        "clock_offset_ms": h.clock_offset_ms,
    }
    return "".join(f"bone_reaper_{name} {number(value)}\n" for name, value in rows.items())
