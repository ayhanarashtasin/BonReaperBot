"""Execution boundary. Only the OMS may hold an :class:`ExecutionPort`."""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from ..types import FillRecord, OrderState, QuoteIntent


class SubmitStatus(enum.StrEnum):
    PENDING = "pending"  # Sent; the ACK or rejection arrives later as an event.
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class CancelStatus(enum.StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SubmitResult:
    status: SubmitStatus
    exchange_order_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class CancelResult:
    status: CancelStatus
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class RemoteOrder:
    exchange_order_id: str
    intent_id: str | None
    state: OrderState
    matched_gross: Decimal


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """What an exchange (real or fake) reports for reconciliation."""

    open_orders: tuple[RemoteOrder, ...]
    trades: tuple[FillRecord, ...]
    cash_balance: Decimal
    positions: tuple[tuple[str, Decimal], ...]


@runtime_checkable
class ExecutionPort(Protocol):
    is_real: bool

    def submit(self, intent: QuoteIntent) -> SubmitResult: ...
    def submit_batch(self, intents: Sequence[QuoteIntent]) -> list[SubmitResult]: ...
    def cancel(self, intent_id: str, exchange_order_id: str | None) -> CancelResult: ...
    def account_snapshot(self) -> AccountSnapshot: ...
