"""Counterpart hedge controller (PRD S3). Priority: passive counterpart, profitable taker hedge,
separately authorized loss-making hedge, then stop and alert.

A loss-making hedge is a risk reduction with a capped cost. It never becomes an arbitrage entry and
its loss is recorded as such.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from decimal import Decimal

from ..types import ONE, ZERO, BookState, IntentPurpose, InventorySnapshot, MarketSpec, Outcome
from .fees import fee_usd
from .pair_scanner import buy_plan_for_net_shares


class HedgeAction(enum.StrEnum):
    NONE = "none"
    PASSIVE_COUNTERPART = "passive_counterpart"
    PROFITABLE_TAKER_HEDGE = "profitable_taker_hedge"
    RISK_REDUCING_HEDGE = "risk_reducing_hedge"
    STOP_AND_ALERT = "stop_and_alert"


@dataclass(frozen=True, slots=True)
class HedgeDecision:
    action: HedgeAction
    outcome: Outcome | None = None
    quantity: Decimal = ZERO
    limit_price: Decimal | None = None
    expected_pair_pnl: Decimal = ZERO
    purpose: IntentPurpose | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class HedgePolicy:
    min_profit_per_pair: Decimal
    allow_negative_edge: bool
    max_hedge_loss: Decimal
    hedge_loss_used: Decimal
    min_order_shares: Decimal
    passive_time_floor_seconds: int = 45


def decide_hedge(
    spec: MarketSpec,
    inventory: InventorySnapshot,
    unpaired_cost_per_share: Decimal,
    counter_book: BookState,
    policy: HedgePolicy,
    now_utc_ms: int,
    counterpart_quote_live: bool,
) -> HedgeDecision:
    """Decide what to do about unpaired inventory. Reads visible book state only."""
    if inventory.unpaired_yes > 0:
        short_side, quantity = Outcome.NO, inventory.unpaired_yes
    elif inventory.unpaired_no > 0:
        short_side, quantity = Outcome.YES, inventory.unpaired_no
    else:
        return HedgeDecision(HedgeAction.NONE, reason="flat")
    if quantity < policy.min_order_shares:
        return HedgeDecision(HedgeAction.NONE, reason="unpaired_below_min_order")

    seconds_left = (spec.end_utc_ms - now_utc_ms) // 1000
    if counterpart_quote_live and seconds_left > policy.passive_time_floor_seconds:
        return HedgeDecision(
            HedgeAction.PASSIVE_COUNTERPART,
            outcome=short_side,
            quantity=quantity,
            reason="counterpart_quote_resting",
        )

    leg = buy_plan_for_net_shares(
        counter_book.asks, quantity, short_side, spec.asset_id(short_side), spec.fee
    )
    if leg.plan is None:
        return HedgeDecision(
            HedgeAction.STOP_AND_ALERT,
            outcome=short_side,
            quantity=quantity,
            reason="insufficient_counterpart_liquidity",
        )
    plan = leg.plan
    matched = min(quantity, plan.net_shares)
    pair_pnl = matched * ONE - matched * unpaired_cost_per_share - plan.cash_spent
    if pair_pnl >= policy.min_profit_per_pair * quantity:
        return HedgeDecision(
            HedgeAction.PROFITABLE_TAKER_HEDGE,
            outcome=short_side,
            quantity=plan.gross_shares,
            limit_price=plan.worst_price,
            expected_pair_pnl=pair_pnl,
            purpose=IntentPurpose.PROFITABLE_HEDGE,
            reason="all_in_pair_profitable",
        )
    if not policy.allow_negative_edge:
        return HedgeDecision(
            HedgeAction.STOP_AND_ALERT,
            outcome=short_side,
            quantity=quantity,
            expected_pair_pnl=pair_pnl,
            reason="hedge_unprofitable_and_not_authorized",
        )
    remaining_budget = policy.max_hedge_loss - policy.hedge_loss_used
    if -pair_pnl > remaining_budget:
        return HedgeDecision(
            HedgeAction.STOP_AND_ALERT,
            outcome=short_side,
            quantity=quantity,
            expected_pair_pnl=pair_pnl,
            reason="hedge_loss_budget_exceeded",
        )
    return HedgeDecision(
        HedgeAction.RISK_REDUCING_HEDGE,
        outcome=short_side,
        quantity=plan.gross_shares,
        limit_price=plan.worst_price,
        expected_pair_pnl=pair_pnl,
        purpose=IntentPurpose.RISK_REDUCING_HEDGE,
        reason="authorized_capped_loss_hedge",
    )


def hedge_fee_estimate(quantity: Decimal, price: Decimal, spec: MarketSpec) -> Decimal:
    return fee_usd(quantity, price, spec.fee)
