"""Shadow execution: identical to paper, but its output is labelled hypothetical with explicit
uncertainty. Nothing is sent to the venue. It cannot reach a network path."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..types import FillRecord, QuoteIntent, to_jsonable
from .paper import PaperExecution, SimExchange
from .ports import AccountSnapshot, CancelResult, SubmitResult

UNCERTAINTY = (
    "hypothetical: queue position, cancels ahead of the order and venue latency are estimated; "
    "this fill never happened and is not evidence of real fillability"
)


class ShadowExecution:
    is_real = False

    def __init__(self, exchange: SimExchange, record_path: Path | None = None) -> None:
        self._inner = PaperExecution(exchange)
        self.record_path = record_path
        self.records: list[dict[str, Any]] = []
        if record_path is not None:
            record_path.parent.mkdir(parents=True, exist_ok=True)
        exchange.on_sim_fill = self._on_fill

    def _record(self, kind: str, body: Any, ts_utc_ms: int) -> None:
        doc = {
            "kind": kind,
            "hypothetical": True,
            "uncertainty": UNCERTAINTY,
            "ts_utc_ms": ts_utc_ms,
            "body": to_jsonable(body),
        }
        self.records.append(doc)
        if self.record_path is not None:
            with self.record_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(doc, sort_keys=True) + "\n")

    def _on_fill(self, fill: FillRecord) -> None:
        self._record("hypothetical_fill", fill, fill.ts_utc_ms)

    def submit(self, intent: QuoteIntent) -> SubmitResult:
        self._record("hypothetical_order", intent, self._inner.exchange.scheduler.now)
        return self._inner.submit(intent)

    def submit_batch(self, intents: Sequence[QuoteIntent]) -> list[SubmitResult]:
        return [self.submit(intent) for intent in intents]

    def cancel(self, intent_id: str, exchange_order_id: str | None) -> CancelResult:
        self._record(
            "hypothetical_cancel", {"intent_id": intent_id}, self._inner.exchange.scheduler.now
        )
        return self._inner.cancel(intent_id, exchange_order_id)

    def account_snapshot(self) -> AccountSnapshot:
        return self._inner.account_snapshot()
