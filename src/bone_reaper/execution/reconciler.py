"""Exchange-versus-local reconciliation (Architecture 9.3). New risk stays frozen unless every
difference is explained within budget. History is never edited."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ..types import TERMINAL_ORDER_STATES, OrderState, to_jsonable
from .oms import OrderManager
from .ports import AccountSnapshot, ExecutionPort

S = OrderState
INFLIGHT_GRACE_MS = 5000


@dataclass(slots=True)
class ReconciliationReport:
    run_id: str
    ts_utc_ms: int
    fills_merged: int = 0
    duplicate_fills_ignored: int = 0
    orders_updated: list[str] = field(default_factory=list)
    inflight_left_alone: list[str] = field(default_factory=list)
    unexplained_remote_orders: list[str] = field(default_factory=list)
    cash_diff: Decimal = Decimal(0)
    position_diffs: dict[str, Decimal] = field(default_factory=dict)
    orphan_fills_remaining: int = 0
    unknown_remaining: int = 0
    clean: bool = False

    def as_dict(self) -> dict[str, Any]:
        return dict(
            to_jsonable(
                {
                    "run_id": self.run_id,
                    "ts_utc_ms": self.ts_utc_ms,
                    "fills_merged": self.fills_merged,
                    "duplicate_fills_ignored": self.duplicate_fills_ignored,
                    "orders_updated": self.orders_updated,
                    "inflight_left_alone": self.inflight_left_alone,
                    "unexplained_remote_orders": self.unexplained_remote_orders,
                    "cash_diff": self.cash_diff,
                    "position_diffs": self.position_diffs,
                    "orphan_fills_remaining": self.orphan_fills_remaining,
                    "unknown_remaining": self.unknown_remaining,
                    "clean": self.clean,
                }
            )
        )


def reconcile(
    oms: OrderManager,
    port: ExecutionPort,
    run_id: str,
    max_unexplained_usd: Decimal = Decimal("0.01"),
    snapshot: AccountSnapshot | None = None,
    inflight_grace_ms: int = INFLIGHT_GRACE_MS,
) -> ReconciliationReport:
    """Merge venue truth into the journal. Freezes risk first and only clears on a clean result.

    A request younger than the grace period may still be travelling, so absence from the venue
    proves nothing about it yet. UNKNOWN orders have no such grace: they are resolved on evidence.
    """
    oms.needs_reconciliation = True
    snap = snapshot if snapshot is not None else port.account_snapshot()
    now = oms.clock.utc_ms()
    report = ReconciliationReport(run_id=run_id, ts_utc_ms=now)

    remote_by_intent = {o.intent_id: o for o in snap.open_orders if o.intent_id}
    remote_by_id = {o.exchange_order_id: o for o in snap.open_orders}
    for order in snap.open_orders:
        if order.intent_id and order.intent_id in oms.orders:
            if oms.orders[order.intent_id].exchange_order_id != order.exchange_order_id:
                oms.link_exchange_id(order.intent_id, order.exchange_order_id)
        else:
            report.unexplained_remote_orders.append(order.exchange_order_id)

    # Retry orphans now that IDs may be linked, then merge venue trades. Dedup makes this safe.
    orphans, oms.orphan_fills = oms.orphan_fills, []
    for fill in [*orphans, *snap.trades]:
        if oms.on_fill(fill):
            report.fills_merged += 1
        elif oms.has_fill(fill.identity):
            report.duplicate_fills_ignored += 1

    for intent_id, record in list(oms.orders.items()):
        current = oms.orders[intent_id]
        if current.state in TERMINAL_ORDER_STATES:
            continue
        remote = remote_by_intent.get(intent_id) or remote_by_id.get(
            current.exchange_order_id or ""
        )
        matched = current.matched_gross
        young = now - record.updated_utc_ms < inflight_grace_ms
        if remote is not None:
            target = S.PARTIALLY_FILLED if matched > 0 else S.OPEN
            if current.state is S.CANCEL_PENDING:
                if young:
                    report.inflight_left_alone.append(intent_id)
                else:
                    oms.on_cancel_rejected(intent_id, "still_open_at_venue")
                    report.orders_updated.append(f"{intent_id}:cancel_not_effective")
            elif current.state is not target:
                oms.force_state(intent_id, target, matched, "open_at_venue")
                report.orders_updated.append(f"{intent_id}:{target.value}")
            continue
        if current.state is S.SUBMITTING and young:
            report.inflight_left_alone.append(intent_id)
            continue
        if current.state is S.SUBMITTING:
            oms.force_state(intent_id, S.UNKNOWN, matched, "no_ack_within_grace")
            current = oms.orders[intent_id]
        # Not open at the venue. Trades above are authoritative for how much it matched.
        if matched >= record.intent.size:
            final = S.FILLED
        elif matched > 0 or current.state in (S.OPEN, S.PARTIALLY_FILLED, S.CANCEL_PENDING):
            final = S.CANCELED
        else:
            final = S.REJECTED  # Unknown, absent from open orders and no trades: never accepted.
        oms.force_state(intent_id, final, matched, "absent_from_venue")
        report.orders_updated.append(f"{intent_id}:{final.value}")

    # Compare balances. Differences are reported, not silently absorbed.
    report.cash_diff = snap.cash_balance - oms.account.total_cash
    for asset_id, quantity in snap.positions:
        diff = quantity - oms.account.shares_net(asset_id)
        if diff != 0:
            report.position_diffs[asset_id] = diff

    report.orphan_fills_remaining = len(oms.orphan_fills)
    report.unknown_remaining = len(oms.unknown_orders)
    report.clean = (
        abs(report.cash_diff) <= max_unexplained_usd
        and not report.position_diffs
        and not report.unexplained_remote_orders
        and report.orphan_fills_remaining == 0
        and report.unknown_remaining == 0
    )
    oms.journal.append({"kind": "reconciliation", **report.as_dict()})
    if report.clean:
        oms.clear_reconciliation()
    return report
