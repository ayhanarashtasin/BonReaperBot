"""Paper account over the ledger: reservations, fills, merge, settlement and redemption.

Redeemable value is not spendable cash until redeemed. Merge proceeds and settlement payouts cannot
double count because both burn the underlying shares through guarded accounts.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from ..types import (
    ONE,
    SETTLEMENT_ASSET,
    ZERO,
    FillRecord,
    FillStatus,
    InventorySnapshot,
    MarketSpec,
    Outcome,
    Side,
)
from .ledger import (
    ADJUSTMENT,
    CAPITAL,
    CASH_AVAILABLE,
    CASH_REDEEMABLE,
    CASH_RESERVED,
    FEES_CASH,
    FEES_SHARES,
    MERGE,
    SETTLEMENT,
    SHARES_BURNED,
    SHARES_HELD,
    SHARES_RESERVED,
    VENUE,
    Ledger,
    LedgerError,
    Line,
    transfer,
)


class SettlementError(RuntimeError):
    pass


@dataclass(slots=True)
class MarketFlows:
    """Per-market cash and share counters. Derived data, the ledger is authoritative."""

    cash_out: Decimal = ZERO
    cash_in: Decimal = ZERO
    fees_usd_equivalent: Decimal = ZERO
    yes_gross: Decimal = ZERO
    no_gross: Decimal = ZERO
    yes_cost: Decimal = ZERO
    no_cost: Decimal = ZERO
    settled: bool = False
    redeemed: bool = False
    winning: Outcome | None = None
    payout: Decimal = ZERO
    merged: Decimal = ZERO

    @property
    def realized_pnl(self) -> Decimal:
        """Cash in (fills, merge, settlement) minus cash out. Final only once settled."""
        return self.cash_in - self.cash_out


class PaperAccount:
    def __init__(self, starting_cash: Decimal = ZERO, *, ts_utc_ms: int = 0) -> None:
        self.ledger = Ledger()
        self.starting_cash = starting_cash
        self._cash_reservations: dict[str, Decimal] = {}
        self._share_reservations: dict[str, tuple[str, Decimal]] = {}
        self._fills: dict[str, FillRecord] = {}
        self.flows: dict[str, MarketFlows] = defaultdict(MarketFlows)
        self._merge_counter: dict[str, int] = defaultdict(int)
        if starting_cash > 0:
            self.ledger.post(
                "deposit:initial",
                transfer(SETTLEMENT_ASSET, CASH_AVAILABLE, CAPITAL, starting_cash),
                ts_utc_ms,
            )

    # --- cash views ------------------------------------------------------------------------------

    @property
    def cash_available(self) -> Decimal:
        return self.ledger.balance(CASH_AVAILABLE)

    @property
    def cash_reserved(self) -> Decimal:
        return self.ledger.balance(CASH_RESERVED)

    @property
    def cash_redeemable(self) -> Decimal:
        return self.ledger.balance(CASH_REDEEMABLE)

    @property
    def total_cash(self) -> Decimal:
        return self.cash_available + self.cash_reserved

    def shares_net(self, asset_id: str) -> Decimal:
        return self.ledger.balance(SHARES_HELD, asset_id) + self.ledger.balance(
            SHARES_RESERVED, asset_id
        )

    def shares_free(self, asset_id: str) -> Decimal:
        return self.ledger.balance(SHARES_HELD, asset_id)

    # --- reservations ----------------------------------------------------------------------------

    def reserve_cash(self, intent_id: str, amount: Decimal, ts_utc_ms: int) -> None:
        if amount <= 0:
            raise LedgerError("reservation must be positive")
        posted = self.ledger.post(
            f"reserve:{intent_id}",
            transfer(SETTLEMENT_ASSET, CASH_RESERVED, CASH_AVAILABLE, amount),
            ts_utc_ms,
        )
        if posted:
            self._cash_reservations[intent_id] = amount

    def reserve_shares(
        self, intent_id: str, asset_id: str, amount: Decimal, ts_utc_ms: int
    ) -> None:
        posted = self.ledger.post(
            f"reserve_shares:{intent_id}",
            transfer(asset_id, SHARES_RESERVED, SHARES_HELD, amount),
            ts_utc_ms,
        )
        if posted:
            self._share_reservations[intent_id] = (asset_id, amount)

    def reserved_for(self, intent_id: str) -> Decimal:
        return self._cash_reservations.get(intent_id, ZERO)

    def release_reservation(self, intent_id: str, ts_utc_ms: int) -> Decimal:
        """Release whatever is left of an intent's reservation. Idempotent."""
        released = ZERO
        cash = self._cash_reservations.pop(intent_id, ZERO)
        if cash > 0:
            self.ledger.post(
                f"release:{intent_id}",
                transfer(SETTLEMENT_ASSET, CASH_AVAILABLE, CASH_RESERVED, cash),
                ts_utc_ms,
            )
            released = cash
        shares = self._share_reservations.pop(intent_id, None)
        if shares is not None and shares[1] > 0:
            asset_id, amount = shares
            self.ledger.post(
                f"release_shares:{intent_id}",
                transfer(asset_id, SHARES_HELD, SHARES_RESERVED, amount),
                ts_utc_ms,
            )
        return released

    # --- fills -----------------------------------------------------------------------------------

    def apply_fill(self, fill: FillRecord) -> bool:
        """Apply a fill exactly once. Returns False for a duplicate of a known identity."""
        cause = f"fill:{fill.identity}"
        if fill.status is FillStatus.FAILED:
            return self._reverse_fill(fill)
        if self.ledger.has_cause(cause):
            return False
        lines = self._buy_lines(fill) if fill.side is Side.BUY else self._sell_lines(fill)
        self.ledger.post(cause, lines, fill.ts_utc_ms, settlement_status="unsettled")
        self._fills[fill.identity] = fill
        self._note_flows(fill, sign=1)
        return True

    def _buy_lines(self, fill: FillRecord) -> list[Line]:
        notional = fill.price * fill.gross_shares
        cash_needed = notional + fill.fee_cash
        reserved_left = (
            self._cash_reservations.get(fill.intent_id, ZERO) if fill.intent_id else ZERO
        )
        from_reserved = min(reserved_left, cash_needed)
        from_available = cash_needed - from_reserved
        if fill.intent_id and fill.intent_id in self._cash_reservations:
            self._cash_reservations[fill.intent_id] = reserved_left - from_reserved
        return [
            Line(VENUE, SETTLEMENT_ASSET, debit=notional),
            Line(FEES_CASH, SETTLEMENT_ASSET, debit=fill.fee_cash),
            Line(CASH_RESERVED, SETTLEMENT_ASSET, credit=from_reserved),
            Line(CASH_AVAILABLE, SETTLEMENT_ASSET, credit=from_available),
            Line(SHARES_HELD, fill.asset_id, debit=fill.net_shares),
            Line(FEES_SHARES, fill.asset_id, debit=fill.fee_shares),
            Line(VENUE, fill.asset_id, credit=fill.gross_shares),
        ]

    def _sell_lines(self, fill: FillRecord) -> list[Line]:
        notional = fill.price * fill.gross_shares
        reserved = self._share_reservations.get(fill.intent_id or "", None)
        from_reserved = min(reserved[1], fill.gross_shares) if reserved else ZERO
        from_held = fill.gross_shares - from_reserved
        if reserved and fill.intent_id:
            self._share_reservations[fill.intent_id] = (reserved[0], reserved[1] - from_reserved)
        return [
            Line(VENUE, fill.asset_id, debit=fill.gross_shares),
            Line(SHARES_RESERVED, fill.asset_id, credit=from_reserved),
            Line(SHARES_HELD, fill.asset_id, credit=from_held),
            Line(CASH_AVAILABLE, SETTLEMENT_ASSET, debit=notional - fill.fee_cash),
            Line(FEES_CASH, SETTLEMENT_ASSET, debit=fill.fee_cash),
            Line(VENUE, SETTLEMENT_ASSET, credit=notional),
        ]

    def _reverse_fill(self, fill: FillRecord) -> bool:
        cause = f"fill:{fill.identity}"
        if not self.ledger.has_cause(cause):
            return False
        posted = self.ledger.reverse(cause, fill.ts_utc_ms)
        if posted:
            self._note_flows(self._fills[fill.identity], sign=-1)
        return posted

    def _note_flows(self, fill: FillRecord, *, sign: int) -> None:
        flows = self.flows[fill.market_id]
        factor = Decimal(sign)
        if fill.side is Side.BUY:
            cost = (fill.price * fill.gross_shares + fill.fee_cash) * factor
            flows.cash_out += cost
            if fill.outcome is Outcome.YES:
                flows.yes_gross += fill.gross_shares * factor
                flows.yes_cost += cost
            else:
                flows.no_gross += fill.gross_shares * factor
                flows.no_cost += cost
        else:
            flows.cash_in += (fill.price * fill.gross_shares - fill.fee_cash) * factor
        flows.fees_usd_equivalent += (fill.fee_cash + fill.fee_shares * fill.price) * factor

    # --- merge, settlement and redemption --------------------------------------------------------

    def merge(self, spec: MarketSpec, quantity: Decimal, ts_utc_ms: int) -> None:
        """Burn complete sets for cash. Limited to matched free shares; settled markets refuse."""
        flows = self.flows[spec.market_id]
        if flows.settled:
            raise SettlementError(f"{spec.market_id} already settled; cannot merge")
        free = min(self.shares_free(spec.yes_asset_id), self.shares_free(spec.no_asset_id))
        if quantity <= 0 or quantity > free:
            raise SettlementError(f"cannot merge {quantity}; {free} complete sets are free")
        self._merge_counter[spec.market_id] += 1
        self.ledger.post(
            f"merge:{spec.market_id}:{self._merge_counter[spec.market_id]}",
            [
                *transfer(spec.yes_asset_id, SHARES_BURNED, SHARES_HELD, quantity),
                *transfer(spec.no_asset_id, SHARES_BURNED, SHARES_HELD, quantity),
                *transfer(SETTLEMENT_ASSET, CASH_AVAILABLE, MERGE, quantity * ONE),
            ],
            ts_utc_ms,
            settlement_status="merged",
        )
        flows.cash_in += quantity * ONE
        flows.merged += quantity

    def settle(self, spec: MarketSpec, winning: Outcome, ts_utc_ms: int) -> Decimal:
        """Mark winners redeemable and burn all shares. Payout is 1 per winning net share."""
        flows = self.flows[spec.market_id]
        cause = f"settle:{spec.market_id}"
        if self.ledger.has_cause(cause):
            return ZERO
        yes_id, no_id = spec.yes_asset_id, spec.no_asset_id
        for asset in (yes_id, no_id):
            if self.ledger.balance(SHARES_RESERVED, asset) != 0:
                raise SettlementError(f"{asset} has shares reserved by open orders")
        yes = self.shares_free(yes_id)
        no = self.shares_free(no_id)
        payout = (yes if winning is Outcome.YES else no) * ONE
        self.ledger.post(
            cause,
            [
                *transfer(yes_id, SHARES_BURNED, SHARES_HELD, yes),
                *transfer(no_id, SHARES_BURNED, SHARES_HELD, no),
                *transfer(SETTLEMENT_ASSET, CASH_REDEEMABLE, SETTLEMENT, payout),
            ],
            ts_utc_ms,
            settlement_status="redeemable",
        )
        flows.settled = True
        flows.winning = winning
        flows.payout = payout
        flows.cash_in += payout
        return payout

    def redeem(self, spec: MarketSpec, ts_utc_ms: int) -> Decimal:
        """Move a settled market's payout from redeemable to spendable cash."""
        flows = self.flows[spec.market_id]
        if not flows.settled:
            raise SettlementError(f"{spec.market_id} is not settled")
        posted = self.ledger.post(
            f"redeem:{spec.market_id}",
            transfer(SETTLEMENT_ASSET, CASH_AVAILABLE, CASH_REDEEMABLE, flows.payout),
            ts_utc_ms,
            settlement_status="redeemed",
        )
        if posted:
            flows.redeemed = True
            return flows.payout
        return ZERO

    def post_adjustment(self, run_id: str, amount: Decimal, ts_utc_ms: int) -> None:
        """A documented reconciliation correction. Positive means venue cash exceeds local."""
        debit, credit = (
            (CASH_AVAILABLE, ADJUSTMENT) if amount >= 0 else (ADJUSTMENT, CASH_AVAILABLE)
        )
        self.ledger.post(
            f"adjust:{run_id}",
            transfer(SETTLEMENT_ASSET, debit, credit, abs(amount)),
            ts_utc_ms,
            settlement_status="reconciliation",
        )

    # --- inventory -------------------------------------------------------------------------------

    def inventory(self, spec: MarketSpec) -> InventorySnapshot:
        flows = self.flows[spec.market_id]
        yes = self.shares_net(spec.yes_asset_id)
        no = self.shares_net(spec.no_asset_id)
        return InventorySnapshot(
            market_id=spec.market_id,
            cash_available=self.cash_available,
            cash_reserved=self.cash_reserved,
            yes_gross=flows.yes_gross,
            yes_net=yes,
            no_gross=flows.no_gross,
            no_net=no,
            paired=min(yes, no),
            unpaired_yes=max(yes - no, ZERO),
            unpaired_no=max(no - yes, ZERO),
            cost_basis=flows.yes_cost + flows.no_cost,
        )
