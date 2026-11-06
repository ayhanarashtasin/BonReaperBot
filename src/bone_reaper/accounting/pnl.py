"""PnL views. Locked value counts matched net shares only; unpaired stays directional. All paper
figures are simulated and never represent actual earnings."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..types import ONE, ZERO, MarketSpec
from .position import PaperAccount


@dataclass(frozen=True, slots=True)
class MarketPnl:
    market_id: str
    realized: Decimal | None
    unrealized_mtm: Decimal | None
    locked_pair_pnl: Decimal
    paired: Decimal
    unpaired_yes: Decimal
    unpaired_no: Decimal
    fees_usd_equivalent: Decimal
    settled: bool
    redeemed: bool


def locked_pair_pnl(account: PaperAccount, spec: MarketSpec) -> Decimal:
    """Payout on matched shares minus their pro-rata cost. Zero without both sides held.

    A YES-only fill therefore has zero locked PnL whatever the other order is doing.
    """
    inventory = account.inventory(spec)
    if inventory.paired <= 0:
        return ZERO
    flows = account.flows[spec.market_id]
    yes_avg = flows.yes_cost / inventory.yes_net if inventory.yes_net > 0 else ZERO
    no_avg = flows.no_cost / inventory.no_net if inventory.no_net > 0 else ZERO
    return inventory.paired * (ONE - yes_avg - no_avg)


def market_pnl(
    account: PaperAccount,
    spec: MarketSpec,
    yes_mark: Decimal | None = None,
    no_mark: Decimal | None = None,
) -> MarketPnl:
    flows = account.flows[spec.market_id]
    inventory = account.inventory(spec)
    unrealized: Decimal | None = None
    if not flows.settled and yes_mark is not None and no_mark is not None:
        value = inventory.yes_net * yes_mark + inventory.no_net * no_mark
        unrealized = value + flows.cash_in - flows.cash_out
    return MarketPnl(
        market_id=spec.market_id,
        realized=flows.realized_pnl if flows.settled else None,
        unrealized_mtm=unrealized,
        locked_pair_pnl=locked_pair_pnl(account, spec) if not flows.settled else ZERO,
        paired=inventory.paired,
        unpaired_yes=inventory.unpaired_yes,
        unpaired_no=inventory.unpaired_no,
        fees_usd_equivalent=flows.fees_usd_equivalent,
        settled=flows.settled,
        redeemed=flows.redeemed,
    )


def realized_total(account: PaperAccount) -> Decimal:
    return sum((f.realized_pnl for f in account.flows.values() if f.settled), ZERO)


def daily_realized_loss(account: PaperAccount) -> Decimal:
    """Positive number. Losses on settled markets only; gains do not offset the stop."""
    total = realized_total(account)
    return -total if total < 0 else ZERO
