"""Conservative maker queue estimator. A price touch alone never fills an order.

A new order joins behind visible resting size at its price, plus an uncertainty reserve. Trades at
the price consume the queue ahead first. Size that disappears without a trade may be a cancel ahead
or behind us, and the feed cannot say which, so it is bracketed by scenario:

* pessimistic: every unexplained removal is behind us (no queue progress) and we join 25% deeper
* central: removals advance the queue in proportion to the size ahead
* optimistic: every unexplained removal is ahead of us

These are assumptions, not measurements, and every report states which one it used.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from decimal import Decimal

from ..types import ZERO


class QueueScenario(enum.StrEnum):
    PESSIMISTIC = "pessimistic"
    CENTRAL = "central"
    OPTIMISTIC = "optimistic"


JOIN_MULTIPLIER = {
    QueueScenario.PESSIMISTIC: Decimal("1.25"),
    QueueScenario.CENTRAL: Decimal("1"),
    QueueScenario.OPTIMISTIC: Decimal("1"),
}


@dataclass(slots=True)
class QueuePosition:
    """Tracks one resting BUY order's place in the queue at its price."""

    price: Decimal
    remaining: Decimal
    scenario: QueueScenario
    ahead: Decimal
    level_size: Decimal
    trade_credit: Decimal = ZERO
    touched_count: int = field(default=0)

    @classmethod
    def join(
        cls, price: Decimal, size: Decimal, visible_level_size: Decimal, scenario: QueueScenario
    ) -> QueuePosition:
        return cls(
            price=price,
            remaining=size,
            scenario=scenario,
            ahead=visible_level_size * JOIN_MULTIPLIER[scenario],
            level_size=visible_level_size,
        )

    def on_trade_at_price(self, shares: Decimal) -> Decimal:
        """A taker traded ``shares`` at exactly our price. Returns how much filled us."""
        self.touched_count += 1
        self.trade_credit += shares
        consumed_ahead = min(self.ahead, shares)
        self.ahead -= consumed_ahead
        fill = min(shares - consumed_ahead, self.remaining)
        self.remaining -= fill
        return fill

    def on_trade_through(self) -> Decimal:
        """A taker traded at a worse price than ours, so our level was exhausted first."""
        self.ahead = ZERO
        fill = self.remaining
        self.remaining = ZERO
        return fill

    def on_level_size(self, new_size: Decimal) -> None:
        """The venue reported a new visible size at our price (excluding our simulated order)."""
        change = new_size - self.level_size
        self.level_size = new_size
        if change >= 0:
            return  # Later arrivals queue behind us and change nothing.
        removed = -change
        explained = min(self.trade_credit, removed)
        self.trade_credit -= explained
        unexplained = removed - explained
        if unexplained <= 0 or self.ahead <= 0:
            return
        if self.scenario is QueueScenario.OPTIMISTIC:
            self.ahead = max(self.ahead - unexplained, ZERO)
        elif self.scenario is QueueScenario.CENTRAL:
            total = new_size + removed
            if total > 0:
                self.ahead = max(self.ahead - unexplained * (self.ahead / total), ZERO)
        self.ahead = min(self.ahead, new_size * JOIN_MULTIPLIER[self.scenario])
