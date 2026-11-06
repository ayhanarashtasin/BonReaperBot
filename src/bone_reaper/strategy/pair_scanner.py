"""Depth- and fee-aware complete-set scanner. It produces records and cannot place orders.

A result is at most *candidate executable*: two separate requests are never atomic, and the books
can move between them. ``classification`` keeps gross, indicative and feasible edge apart. The
scanner never reports a confirmed lock; only reconciled fills in the ledger can show one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from ..market.validation import book_age_ms, book_problems
from ..types import (
    ONE,
    ZERO,
    AdverseCase,
    ArbCandidate,
    BookState,
    DepthFill,
    FeeBreakdown,
    FeeParams,
    LegPlan,
    LiquidityRole,
    MarketSpec,
    Outcome,
    PriceLevel,
    sha256_hex,
)
from .fees import buy_fill_economics

SHARE_LOT = Decimal("0.01")

REJECT_LIQUIDITY = "insufficient_liquidity"
REJECT_BELOW_MIN = "below_min_order_size"
REJECT_EDGE = "edge_below_threshold"
REJECT_ROI = "roi_below_threshold"
REJECT_NEGATIVE = "negative_net_edge"
REJECT_EXPIRY = "too_close_to_expiry"
REJECT_EXPIRED = "market_expired"
REJECT_NOT_STARTED = "market_not_started"
REJECT_STATUS = "market_not_accepting_orders"
REJECT_OFF_TICK = "off_tick_ask"
REJECT_ROUTE = "taker_taker_route_disabled"

CLASS_REJECTED = "rejected"
CLASS_CANDIDATE = "candidate_executable"


@dataclass(frozen=True, slots=True)
class ScanThresholds:
    min_net_edge_usd: Decimal
    min_net_roi: Decimal
    max_book_age_ms: int
    stop_seconds_before_close: int
    execution_reserve_per_share: Decimal = Decimal("0.002")
    settlement_cost_per_share: Decimal = ZERO
    allow_taker_taker: bool = False


@dataclass(frozen=True, slots=True)
class LegResult:
    plan: LegPlan | None
    available_net: Decimal
    reason: str | None


def _round_up_lot(value: Decimal) -> Decimal:
    return (value / SHARE_LOT).to_integral_value(rounding=ROUND_CEILING) * SHARE_LOT


def _net_per_gross(price: Decimal, fee: FeeParams) -> Decimal:
    """Net shares per gross share at this price, unrounded, for sizing a target."""
    probe = Decimal(1000)
    net, _, _ = buy_fill_economics(probe, price, fee, LiquidityRole.TAKER)
    return net / probe


def buy_plan_for_net_shares(
    asks: Sequence[PriceLevel],
    q_net: Decimal,
    outcome: Outcome,
    asset_id: str,
    fee: FeeParams,
) -> LegResult:
    """Walk asks from the best price until ``q_net`` *net* shares are acquired."""
    if q_net <= 0:
        raise ValueError("q_net must be positive")
    remaining_net = q_net
    fills: list[DepthFill] = []
    total_net = ZERO
    total_notional = ZERO
    total_cash = ZERO
    fee_cash = ZERO
    fee_shares = ZERO
    fee_equiv = ZERO
    for level in sorted(asks, key=lambda lv: lv.price):
        if remaining_net <= 0:
            break
        needed_gross = _round_up_lot(remaining_net / _net_per_gross(level.price, fee))
        take = min(needed_gross, level.size)
        if take <= 0:
            continue
        net, leg_fee, cash = buy_fill_economics(take, level.price, fee, LiquidityRole.TAKER)
        # Fee rounding can leave a sliver short of the target. Top up by a lot if this level can.
        while net < remaining_net and take < level.size:
            take = min(take + SHARE_LOT, level.size)
            net, leg_fee, cash = buy_fill_economics(take, level.price, fee, LiquidityRole.TAKER)
        fills.append(DepthFill(level.price, take))
        total_net += net
        total_notional += take * level.price
        total_cash += cash
        fee_cash += leg_fee.fee_cash
        fee_shares += leg_fee.fee_shares
        fee_equiv += leg_fee.fee_usd_equivalent
        remaining_net -= net
    if remaining_net > 0:
        return LegResult(None, total_net, REJECT_LIQUIDITY)
    gross = sum((f.gross_shares for f in fills), ZERO)
    return LegResult(
        LegPlan(
            outcome=outcome,
            asset_id=asset_id,
            levels=tuple(fills),
            gross_shares=gross,
            net_shares=total_net,
            notional=total_notional,
            fee=FeeBreakdown(fee_cash, fee_shares, fee_equiv),
            cash_spent=total_cash,
            vwap=total_notional / gross,
            worst_price=fills[-1].price,
        ),
        total_net,
        None,
    )


def _empty_leg(outcome: Outcome, asset_id: str) -> LegPlan:
    return LegPlan(
        outcome=outcome,
        asset_id=asset_id,
        levels=(),
        gross_shares=ZERO,
        net_shares=ZERO,
        notional=ZERO,
        fee=FeeBreakdown(ZERO, ZERO, ZERO),
        cash_spent=ZERO,
        vwap=ZERO,
        worst_price=ZERO,
    )


def _adverse_cases(
    yes: LegPlan, no: LegPlan, spec: MarketSpec, reserve_edge: Decimal
) -> tuple[AdverseCase, ...]:
    """Each candidate shows what happens if the two non-atomic requests do not both go well."""
    capital = yes.cash_spent + no.cash_spent
    matched = min(yes.net_shares, no.net_shares)
    tick = spec.tick_size
    gross_shares = no.gross_shares
    bumped_no_cost = no.cash_spent + gross_shares * tick
    return (
        AdverseCase(
            name="one_tick_worse_second_leg",
            edge=matched - yes.cash_spent - bumped_no_cost,
            funding_required=yes.cash_spent + bumped_no_cost,
            note="second leg fills one tick worse; requests are not atomic",
        ),
        AdverseCase(
            name="second_leg_fails_yes_held",
            edge=-yes.cash_spent,
            funding_required=yes.cash_spent,
            note="only YES fills: directional position, worst case total loss of that leg",
        ),
        AdverseCase(
            name="second_leg_fails_no_held",
            edge=-no.cash_spent,
            funding_required=no.cash_spent,
            note="only NO fills: directional position, worst case total loss of that leg",
        ),
        AdverseCase(
            name="no_rebates_after_reserve",
            edge=reserve_edge,
            funding_required=capital,
            note="maker rebates excluded; rebates never count toward edge",
        ),
    )


def _freshness_reasons(book: BookState, label: str, now: int, max_age: int) -> list[str]:
    return [f"{label}:{p}" for p in book_problems(book, now, max_age)]


def evaluate_pair(
    spec: MarketSpec,
    yes_book: BookState,
    no_book: BookState,
    q_net: Decimal,
    thresholds: ScanThresholds,
    now_utc_ms: int,
) -> ArbCandidate:
    """Evaluate one size. Always returns a record: passing or with precise reject reasons."""
    reasons: list[str] = []
    reasons += _freshness_reasons(yes_book, "yes", now_utc_ms, thresholds.max_book_age_ms)
    reasons += _freshness_reasons(no_book, "no", now_utc_ms, thresholds.max_book_age_ms)
    if yes_book.asset_id != spec.yes_asset_id or no_book.asset_id != spec.no_asset_id:
        reasons.append("book_asset_mismatch")

    seconds_left = Decimal(spec.end_utc_ms - now_utc_ms) / 1000
    if now_utc_ms < spec.start_utc_ms:
        reasons.append(REJECT_NOT_STARTED)
    if seconds_left <= 0:
        reasons.append(REJECT_EXPIRED)
    elif seconds_left <= thresholds.stop_seconds_before_close:
        reasons.append(REJECT_EXPIRY)
    if not spec.accepting_orders:
        reasons.append(REJECT_STATUS)
    if q_net < spec.min_order_shares:
        reasons.append(REJECT_BELOW_MIN)
    for book in (yes_book, no_book):
        # The live tick comes from the book: the venue narrows it near price extremes.
        if any(lv.price % book.tick_size != 0 for lv in book.asks):
            reasons.append(REJECT_OFF_TICK)
            break

    yes_result = buy_plan_for_net_shares(
        yes_book.asks, q_net, Outcome.YES, spec.yes_asset_id, spec.fee
    )
    no_result = buy_plan_for_net_shares(no_book.asks, q_net, Outcome.NO, spec.no_asset_id, spec.fee)
    if yes_result.reason:
        reasons.append(f"yes:{yes_result.reason}")
    if no_result.reason:
        reasons.append(f"no:{no_result.reason}")
    yes_plan = yes_result.plan or _empty_leg(Outcome.YES, spec.yes_asset_id)
    no_plan = no_result.plan or _empty_leg(Outcome.NO, spec.no_asset_id)

    complete = yes_result.plan is not None and no_result.plan is not None
    matched = min(yes_plan.net_shares, no_plan.net_shares)
    capital = yes_plan.cash_spent + no_plan.cash_spent
    fee_equiv = yes_plan.fee.fee_usd_equivalent + no_plan.fee.fee_usd_equivalent
    gross_edge = q_net * ONE - (yes_plan.vwap + no_plan.vwap) * q_net if complete else ZERO
    # Fees-in-shares reduce the matched payout, and fees-in-cash raise capital, so this formula
    # covers both collection methods without subtracting fees a second time.
    indicative = matched - capital if complete else ZERO
    reserve = q_net * (
        thresholds.execution_reserve_per_share + thresholds.settlement_cost_per_share
    )
    after_reserve = indicative - reserve if complete else ZERO
    roi = after_reserve / capital if capital > 0 else ZERO

    if complete:
        if after_reserve <= 0:
            reasons.append(REJECT_NEGATIVE)
        if after_reserve < thresholds.min_net_edge_usd:
            reasons.append(REJECT_EDGE)
        if roi < thresholds.min_net_roi:
            reasons.append(REJECT_ROI)
    economically_passed = not reasons
    # A passing record is research output only unless the route is explicitly enabled.
    route_note: tuple[str, ...] = (
        (REJECT_ROUTE,) if economically_passed and not thresholds.allow_taker_taker else ()
    )

    identity = f"{spec.market_id}:{now_utc_ms}:{q_net}"
    return ArbCandidate(
        candidate_id=sha256_hex(identity.encode())[:16],
        market_id=spec.market_id,
        ts_utc_ms=now_utc_ms,
        q_net=q_net,
        yes_leg=yes_plan,
        no_leg=no_plan,
        capital=capital,
        gross_edge=gross_edge,
        fee_usd_equivalent=fee_equiv,
        indicative_edge=indicative,
        edge_after_reserve=after_reserve,
        roi=roi,
        seconds_to_expiry=seconds_left,
        yes_book_age_ms=book_age_ms(yes_book, now_utc_ms) or 0,
        no_book_age_ms=book_age_ms(no_book, now_utc_ms) or 0,
        passed=economically_passed,
        reasons=tuple(reasons) + route_note,
        fee_version=spec.fee.version,
        adverse_cases=_adverse_cases(yes_plan, no_plan, spec, after_reserve) if complete else (),
        classification=CLASS_CANDIDATE if economically_passed else CLASS_REJECTED,
    )


def scan_market(
    spec: MarketSpec,
    yes_book: BookState,
    no_book: BookState,
    sizes: Sequence[Decimal],
    thresholds: ScanThresholds,
    now_utc_ms: int,
) -> list[ArbCandidate]:
    return [evaluate_pair(spec, yes_book, no_book, q, thresholds, now_utc_ms) for q in sizes]
