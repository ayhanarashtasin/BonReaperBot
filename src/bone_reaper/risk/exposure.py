"""Worst-case exposure assuming every open BUY on one outcome fills and none on the other does."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from ..accounting.position import PaperAccount
from ..types import TERMINAL_ORDER_STATES, ZERO, MarketSpec, OrderRecord, Outcome, QuoteIntent, Side


@dataclass(frozen=True, slots=True)
class Exposure:
    open_buy_notional: Decimal
    position_cost: Decimal
    worst_unpaired_notional: Decimal
    worst_unpaired_outcome: Outcome | None

    @property
    def gross_notional(self) -> Decimal:
        return self.open_buy_notional + self.position_cost


def _open_buys(
    orders: Iterable[OrderRecord], market_id: str, outcome: Outcome
) -> tuple[Decimal, Decimal]:
    """Remaining shares and notional. Pending cancels still count: a cancel is not proof."""
    shares = ZERO
    notional = ZERO
    for order in orders:
        intent = order.intent
        if (
            order.state in TERMINAL_ORDER_STATES
            or intent.side is not Side.BUY
            or intent.market_id != market_id
            or intent.outcome is not outcome
        ):
            continue
        shares += order.remaining
        notional += order.remaining * intent.price
    return shares, notional


def market_exposure(
    account: PaperAccount,
    spec: MarketSpec,
    orders: Iterable[OrderRecord],
    extra: QuoteIntent | None = None,
) -> Exposure:
    order_list = list(orders)
    yes_open, yes_open_notional = _open_buys(order_list, spec.market_id, Outcome.YES)
    no_open, no_open_notional = _open_buys(order_list, spec.market_id, Outcome.NO)
    if extra is not None and extra.side is Side.BUY and extra.market_id == spec.market_id:
        if extra.outcome is Outcome.YES:
            yes_open += extra.size
            yes_open_notional += extra.notional
        else:
            no_open += extra.size
            no_open_notional += extra.notional
    flows = account.flows[spec.market_id]
    yes_net = account.shares_net(spec.yes_asset_id)
    no_net = account.shares_net(spec.no_asset_id)
    # Only unsettled cost is at risk here.
    yes_cost = ZERO if flows.settled else flows.yes_cost
    no_cost = ZERO if flows.settled else flows.no_cost

    def scenario(
        mine_net: Decimal, mine_open: Decimal, other_net: Decimal, cost: Decimal
    ) -> Decimal:
        total = mine_net + mine_open
        unpaired = max(total - other_net, ZERO)
        return unpaired * (cost / total) if total > 0 else ZERO

    yes_case = scenario(yes_net, yes_open, no_net, yes_cost + yes_open_notional)
    no_case = scenario(no_net, no_open, yes_net, no_cost + no_open_notional)
    worst = max(yes_case, no_case)
    worst_outcome = None
    if worst > 0:
        worst_outcome = Outcome.YES if yes_case >= no_case else Outcome.NO
    return Exposure(
        open_buy_notional=yes_open_notional + no_open_notional,
        position_cost=yes_cost + no_cost,
        worst_unpaired_notional=worst,
        worst_unpaired_outcome=worst_outcome,
    )
