"""Durable order manager: the single writer per account and the only holder of an ExecutionPort.

Rules from Architecture section 9: journal and reserve before any network send; a timed-out submit
is UNKNOWN and is never retried; fills may precede ACKs; a cancel is not real until confirmed.
Unknown state and orphan fills freeze new risk until reconciliation clears them.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from ..accounting.position import PaperAccount
from ..settings import Settings
from ..strategy.fees import fee_usd
from ..time import Clock
from ..types import (
    TERMINAL_ORDER_STATES,
    FeeParams,
    FillRecord,
    FillStatus,
    OrderRecord,
    OrderState,
    QuoteIntent,
    RiskDecision,
    Side,
    from_jsonable,
    to_jsonable,
)
from .ports import CancelStatus, ExecutionPort, SubmitResult, SubmitStatus

S = OrderState
LEGAL_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    S.INTENT: frozenset({S.RISK_REJECTED, S.PREPARED}),
    S.PREPARED: frozenset({S.SUBMITTING}),
    # EXPIRED here: a FAK remainder can be reported dead before any other notice reaches us.
    S.SUBMITTING: frozenset(
        {S.OPEN, S.PARTIALLY_FILLED, S.FILLED, S.REJECTED, S.UNKNOWN, S.EXPIRED}
    ),
    S.OPEN: frozenset({S.PARTIALLY_FILLED, S.FILLED, S.CANCEL_PENDING, S.EXPIRED, S.CANCELED}),
    S.PARTIALLY_FILLED: frozenset({S.FILLED, S.CANCEL_PENDING, S.EXPIRED, S.CANCELED}),
    S.CANCEL_PENDING: frozenset({S.CANCELED, S.PARTIALLY_FILLED, S.FILLED, S.OPEN, S.EXPIRED}),
    S.UNKNOWN: frozenset({S.OPEN, S.PARTIALLY_FILLED, S.FILLED, S.REJECTED, S.CANCELED, S.EXPIRED}),
}


class IllegalTransitionError(RuntimeError):
    pass


class Journal(Protocol):
    def append(self, record: dict[str, Any]) -> None: ...
    def read_all(self) -> list[dict[str, Any]]: ...


class InMemoryJournal:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def append(self, record: dict[str, Any]) -> None:
        self.records.append(json.loads(json.dumps(record)))

    def read_all(self) -> list[dict[str, Any]]:
        return list(self.records)


class FileJournal:
    """Append-only JSONL with fsync, so an intent is durable before any send."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text("utf-8").splitlines() if line]


def build_journal(settings: Settings) -> Journal:
    ledger = settings.storage.ledger
    if ledger == "memory":
        return InMemoryJournal()
    if ledger == "sqlite":
        return FileJournal(Path(settings.storage.journal_path))
    from ..storage.postgres import PostgresStore, StoreError

    dsn = os.environ.get(settings.storage.postgres_dsn_env)
    if not dsn:
        raise StoreError(f"missing environment variable {settings.storage.postgres_dsn_env}")
    store = PostgresStore(dsn, wallet_id=settings.mode.value)
    store.migrate()
    store.require_writer()
    return store


class RiskGate(Protocol):
    def evaluate(self, intent: QuoteIntent) -> RiskDecision: ...


class OrderManager:
    def __init__(
        self,
        port: ExecutionPort,
        account: PaperAccount,
        risk: RiskGate,
        journal: Journal,
        clock: Clock,
        fee_lookup: dict[str, FeeParams] | None = None,
    ) -> None:
        self._port = port
        self.account = account
        self._risk = risk
        self.journal = journal
        self.clock = clock
        self.orders: dict[str, OrderRecord] = {}
        self._by_exchange_id: dict[str, str] = {}
        self._fill_ids: set[str] = set()
        self._fee_lookup = fee_lookup if fee_lookup is not None else {}
        self.orphan_fills: list[FillRecord] = []
        self.anomalies: list[dict[str, Any]] = []
        self.needs_reconciliation = False
        self.decisions: list[RiskDecision] = []

    # --- state --------------------------------------------------------------------------------

    @property
    def unknown_orders(self) -> list[OrderRecord]:
        return [o for o in self.orders.values() if o.state is S.UNKNOWN]

    @property
    def open_orders(self) -> list[OrderRecord]:
        return [o for o in self.orders.values() if o.state not in TERMINAL_ORDER_STATES]

    @property
    def risk_frozen(self) -> bool:
        return bool(self.unknown_orders or self.orphan_fills or self.needs_reconciliation)

    def freeze_reasons(self) -> list[str]:
        reasons = []
        if self.unknown_orders:
            reasons.append("unknown_order_state")
        if self.orphan_fills:
            reasons.append("orphan_fill_unmatched")
        if self.needs_reconciliation:
            reasons.append("reconciliation_required")
        return reasons

    def _journal(self, kind: str, **fields: Any) -> None:
        self.journal.append({"kind": kind, "ts_utc_ms": self.clock.utc_ms(), **to_jsonable(fields)})

    def _transition(self, record: OrderRecord, to: OrderState, **changes: Any) -> OrderRecord:
        if to is not record.state and to not in LEGAL_TRANSITIONS.get(record.state, frozenset()):
            raise IllegalTransitionError(f"{record.intent.intent_id}: {record.state} -> {to}")
        updated = replace(record, state=to, updated_utc_ms=self.clock.utc_ms(), **changes)
        self.orders[record.intent.intent_id] = updated
        self._journal(
            "state",
            intent_id=record.intent.intent_id,
            state=to,
            exchange_order_id=updated.exchange_order_id,
            matched_gross=updated.matched_gross,
            pending_cancel=updated.pending_cancel,
        )
        if to in TERMINAL_ORDER_STATES:
            self.account.release_reservation(record.intent.intent_id, self.clock.utc_ms())
        return updated

    # --- submission ---------------------------------------------------------------------------

    def _reservation_amount(self, intent: QuoteIntent) -> Decimal:
        """Worst-case cash for a BUY: full notional, plus the taker fee if it could take."""
        amount = intent.notional
        fee = self._fee_lookup.get(intent.market_id)
        if not intent.post_only and fee is not None:
            amount += fee_usd(intent.size, intent.price, fee)
        return amount

    def _prepare(self, intent: QuoteIntent) -> OrderRecord | None:
        if intent.intent_id in self.orders:
            raise ValueError(f"duplicate intent id {intent.intent_id}")
        now = self.clock.utc_ms()
        decision = self._risk.evaluate(intent)
        self.decisions.append(decision)
        self._journal("risk_decision", decision=decision)
        record = OrderRecord(intent=intent, state=S.INTENT, created_utc_ms=now, updated_utc_ms=now)
        self.orders[intent.intent_id] = record
        if not decision.allowed:
            # Separate kind: a rejected intent never held funds, so restore must not reserve any.
            self._journal("intent_rejected", intent=intent)
            self._transition(record, S.RISK_REJECTED)
            return None
        intent = replace(intent, risk_id=decision.request_id)
        record = replace(record, intent=intent)
        self.orders[intent.intent_id] = record
        # Journal before reserving and before send: a crash here leaves evidence, not a lost order.
        reservation = self._reservation_amount(intent) if intent.side is Side.BUY else intent.size
        if hasattr(self.journal, "record_intent"):
            self.journal.record_intent(intent, reservation, now)
        else:
            self._journal("intent", intent=intent)
        if intent.side is Side.BUY:
            self.account.reserve_cash(intent.intent_id, reservation, now)
        else:
            self.account.reserve_shares(intent.intent_id, intent.asset_id, intent.size, now)
        record = self._transition(record, S.PREPARED)
        return self._transition(record, S.SUBMITTING)

    def _apply_submit_result(self, intent_id: str, result: SubmitResult) -> OrderRecord:
        record = self.orders[intent_id]
        if result.exchange_order_id:
            self._by_exchange_id[result.exchange_order_id] = intent_id
        if result.status is SubmitStatus.PENDING:
            if result.exchange_order_id:
                record = replace(record, exchange_order_id=result.exchange_order_id)
                self.orders[intent_id] = record
            return record
        if result.status is SubmitStatus.ACCEPTED:
            return self.on_ack(intent_id, result.exchange_order_id)
        if result.status is SubmitStatus.REJECTED:
            return self.on_reject(intent_id, result.reason or "rejected")
        return self._mark_unknown(intent_id, result.reason or "ambiguous_ack")

    def _mark_unknown(self, intent_id: str, reason: str) -> OrderRecord:
        record = self.orders[intent_id]
        self._journal("unknown", intent_id=intent_id, reason=reason)
        if record.state is S.SUBMITTING:
            return self._transition(record, S.UNKNOWN)
        return record

    def submit(self, intent: QuoteIntent) -> OrderRecord:
        prepared = self._prepare(intent)
        if prepared is None:
            return self.orders[intent.intent_id]
        try:
            result = self._port.submit(prepared.intent)
        except Exception as exc:  # A send failure is ambiguous. Never retry blindly.
            return self._mark_unknown(intent.intent_id, f"submit_exception:{type(exc).__name__}")
        return self._apply_submit_result(intent.intent_id, result)

    def submit_batch(self, intents: Sequence[QuoteIntent]) -> list[OrderRecord]:
        """Maker batches are not atomic: every leg is approved and resolved independently."""
        prepared = [p for p in (self._prepare(i) for i in intents) if p is not None]
        if prepared:
            live = [p.intent for p in prepared]
            try:
                results = self._port.submit_batch(live)
            except Exception as exc:
                for leg in live:
                    self._mark_unknown(leg.intent_id, f"batch_exception:{type(exc).__name__}")
            else:
                if len(results) != len(live):
                    for leg in live:
                        self._mark_unknown(leg.intent_id, "batch_result_count_mismatch")
                else:
                    for leg, result in zip(live, results, strict=True):
                        self._apply_submit_result(leg.intent_id, result)
        return [self.orders[i.intent_id] for i in intents]

    # --- exchange events ----------------------------------------------------------------------

    def on_ack(self, intent_id: str, exchange_order_id: str | None) -> OrderRecord:
        record = self.orders[intent_id]
        if exchange_order_id:
            self._by_exchange_id[exchange_order_id] = intent_id
            if record.exchange_order_id != exchange_order_id:
                record = replace(record, exchange_order_id=exchange_order_id)
                self.orders[intent_id] = record
        if record.state in (S.SUBMITTING, S.UNKNOWN):
            target = S.PARTIALLY_FILLED if record.matched_gross > 0 else S.OPEN
            return self._transition(record, target)
        return record  # A fill already moved it on; a late ACK changes nothing.

    def on_reject(self, intent_id: str, reason: str) -> OrderRecord:
        record = self.orders[intent_id]
        self._journal("rejected", intent_id=intent_id, reason=reason)
        if record.matched_gross > 0:
            self.anomalies.append({"kind": "reject_after_fill", "intent_id": intent_id})
            self.needs_reconciliation = True
            return record
        return self._transition(record, S.REJECTED)

    def on_expired(self, intent_id: str) -> OrderRecord:
        record = self.orders[intent_id]
        if record.state in TERMINAL_ORDER_STATES:
            return record
        return self._transition(record, S.EXPIRED)

    def _intent_for_fill(self, fill: FillRecord) -> str | None:
        if fill.intent_id and fill.intent_id in self.orders:
            return fill.intent_id
        if fill.exchange_order_id:
            return self._by_exchange_id.get(fill.exchange_order_id)
        return None

    def on_fill(self, fill: FillRecord) -> bool:
        """Commit a fill exactly once. Fills may arrive before ACK, or even after a cancel."""
        if fill.status is FillStatus.FAILED:
            self.account.apply_fill(fill)
            self._journal("fill_failed", identity=fill.identity)
            self.needs_reconciliation = True
            return True
        if fill.identity in self._fill_ids:
            self._journal("fill_duplicate", identity=fill.identity)
            return False
        intent_id = self._intent_for_fill(fill)
        if intent_id is None:
            self.orphan_fills.append(fill)
            self._journal("fill_orphan", fill=fill)
            return False
        self._fill_ids.add(fill.identity)
        fill = replace(fill, intent_id=intent_id)
        entries_before = len(self.account.ledger.entries)
        self.account.apply_fill(fill)
        if hasattr(self.journal, "record_fill"):
            self.journal.record_fill(fill, self.account.ledger.entries[entries_before:])
        else:
            self._journal("fill", fill=fill)
        record = self.orders[intent_id]
        matched = record.matched_gross + fill.gross_shares
        if record.state in TERMINAL_ORDER_STATES:
            self.orders[intent_id] = replace(record, matched_gross=matched)
            self.anomalies.append({"kind": "fill_after_terminal", "intent_id": intent_id})
            self.needs_reconciliation = True
            return True
        target = S.FILLED if matched >= record.intent.size else S.PARTIALLY_FILLED
        if record.state is S.PARTIALLY_FILLED and target is S.PARTIALLY_FILLED:
            self._set_matched(record, matched)
        elif record.state is S.UNKNOWN:
            self._set_matched(record, matched)
        else:
            self._transition(record, target, matched_gross=matched)
        return True

    def _set_matched(self, record: OrderRecord, matched: Decimal) -> None:
        updated = replace(record, matched_gross=matched, updated_utc_ms=self.clock.utc_ms())
        self.orders[record.intent.intent_id] = updated
        self._journal(
            "state",
            intent_id=record.intent.intent_id,
            state=record.state,
            exchange_order_id=record.exchange_order_id,
            matched_gross=matched,
            pending_cancel=record.pending_cancel,
        )

    # --- cancellation --------------------------------------------------------------------------

    def cancel(self, intent_id: str, reason: str = "strategy") -> OrderRecord:
        """Cancels reduce risk, so they do not pass new-risk checks and work while frozen."""
        record = self.orders[intent_id]
        if record.state in TERMINAL_ORDER_STATES or record.pending_cancel:
            return record
        if record.state not in (S.OPEN, S.PARTIALLY_FILLED):
            return record  # SUBMITTING or UNKNOWN: nothing certain to cancel yet.
        self._journal("cancel_request", intent_id=intent_id, reason=reason)
        record = self._transition(record, S.CANCEL_PENDING, pending_cancel=True)
        try:
            result = self._port.cancel(intent_id, record.exchange_order_id)
        except Exception as exc:
            self._journal("cancel_ambiguous", intent_id=intent_id, error=type(exc).__name__)
            self.needs_reconciliation = True
            return record
        if result.status is CancelStatus.CONFIRMED:
            return self.on_cancel_confirmed(intent_id)
        if result.status is CancelStatus.REJECTED:
            return self.on_cancel_rejected(intent_id, result.reason or "rejected")
        if result.status is CancelStatus.UNKNOWN:
            self.needs_reconciliation = True
        return record

    def on_cancel_confirmed(self, intent_id: str) -> OrderRecord:
        record = self.orders[intent_id]
        if record.state in TERMINAL_ORDER_STATES:
            return record
        return self._transition(record, S.CANCELED, pending_cancel=False)

    def on_cancel_rejected(self, intent_id: str, reason: str) -> OrderRecord:
        record = self.orders[intent_id]
        self._journal("cancel_rejected", intent_id=intent_id, reason=reason)
        if record.state is not S.CANCEL_PENDING and not record.pending_cancel:
            return record
        if record.state in TERMINAL_ORDER_STATES:
            return record
        target = S.PARTIALLY_FILLED if record.matched_gross > 0 else S.OPEN
        return self._transition(record, target, pending_cancel=False)

    # --- reconciliation hooks and restart ------------------------------------------------------

    def force_state(
        self, intent_id: str, to: OrderState, matched: Decimal | None, evidence: str
    ) -> OrderRecord:
        """Apply reconciled venue state through the legal transition table, with evidence."""
        record = self.orders[intent_id]
        self._journal("reconciled_state", intent_id=intent_id, to=to, evidence=evidence)
        changes: dict[str, Any] = {"pending_cancel": False}
        if matched is not None:
            changes["matched_gross"] = matched
        return self._transition(record, to, **changes)

    def link_exchange_id(self, intent_id: str, exchange_order_id: str) -> None:
        self._by_exchange_id[exchange_order_id] = intent_id
        self.orders[intent_id] = replace(
            self.orders[intent_id], exchange_order_id=exchange_order_id
        )

    def clear_reconciliation(self) -> None:
        self.needs_reconciliation = False

    def has_fill(self, identity: str) -> bool:
        return identity in self._fill_ids

    def restore(self) -> None:
        """Rebuild from the journal after a restart. Anything in flight becomes UNKNOWN.

        A pending order is never assumed rejected. New risk stays frozen until a reconcile runs.
        """
        self.orders.clear()
        for rec in self.journal.read_all():
            kind = rec["kind"]
            if kind == "intent_rejected":
                intent = from_jsonable(QuoteIntent, rec["intent"])
                self.orders[intent.intent_id] = OrderRecord(
                    intent=intent,
                    state=S.RISK_REJECTED,
                    created_utc_ms=rec["ts_utc_ms"],
                    updated_utc_ms=rec["ts_utc_ms"],
                )
            elif kind == "intent":
                intent = from_jsonable(QuoteIntent, rec["intent"])
                self.orders[intent.intent_id] = OrderRecord(
                    intent=intent,
                    state=S.INTENT,
                    created_utc_ms=rec["ts_utc_ms"],
                    updated_utc_ms=rec["ts_utc_ms"],
                )
                if intent.side is Side.BUY:
                    self.account.reserve_cash(
                        intent.intent_id, self._reservation_amount(intent), rec["ts_utc_ms"]
                    )
                else:
                    self.account.reserve_shares(
                        intent.intent_id, intent.asset_id, intent.size, rec["ts_utc_ms"]
                    )
            elif kind == "state":
                record = self.orders[rec["intent_id"]]
                exchange_id = rec.get("exchange_order_id")
                if exchange_id:
                    self._by_exchange_id[exchange_id] = rec["intent_id"]
                state = OrderState(rec["state"])
                self.orders[rec["intent_id"]] = replace(
                    record,
                    state=state,
                    exchange_order_id=exchange_id,
                    matched_gross=Decimal(rec["matched_gross"]),
                    pending_cancel=bool(rec.get("pending_cancel")),
                    updated_utc_ms=rec["ts_utc_ms"],
                )
                if state in TERMINAL_ORDER_STATES:
                    self.account.release_reservation(rec["intent_id"], rec["ts_utc_ms"])
            elif kind == "fill":
                fill = from_jsonable(FillRecord, rec["fill"])
                self._fill_ids.add(fill.identity)
                self.account.apply_fill(fill)
        for intent_id, record in list(self.orders.items()):
            if record.state in (S.INTENT, S.PREPARED, S.SUBMITTING):
                self.orders[intent_id] = replace(record, state=S.UNKNOWN)
        self.needs_reconciliation = True
        self._journal("restart", orders=len(self.orders))
