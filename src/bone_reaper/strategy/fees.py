"""Market-level taker fee calculations.

Formula (docs.polymarket.com/trading/fees, and the SDK's order math):
    fee_usd = shares * rate * (price * (1 - price)) ** exponent
Rounded to 5 decimal places, so anything below 0.000005 rounds to zero. Makers pay nothing, and
rebates are variable and paid later, so they never enter a pre-trade edge.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from ..types import ONE, ZERO, FeeBreakdown, FeeCollection, FeeParams, LiquidityRole

FEE_QUANTUM = Decimal("0.00001")
SHARE_QUANTUM = Decimal("0.000001")


def round_fee(value: Decimal) -> Decimal:
    return value.quantize(FEE_QUANTUM, rounding=ROUND_HALF_UP)


def unrounded_fee(shares: Decimal, price: Decimal, fee: FeeParams) -> Decimal:
    if not fee.enabled or fee.rate == 0:
        return ZERO
    return shares * fee.rate * (price * (ONE - price)) ** fee.exponent


def fee_usd(shares: Decimal, price: Decimal, fee: FeeParams) -> Decimal:
    if not ZERO < price < ONE:
        raise ValueError(f"price {price} outside (0, 1)")
    if shares < 0:
        raise ValueError("shares must be non-negative")
    return round_fee(unrounded_fee(shares, price, fee))


def buy_fill_economics(
    gross_shares: Decimal, price: Decimal, fee: FeeParams, role: LiquidityRole
) -> tuple[Decimal, FeeBreakdown, Decimal]:
    """Return ``(net_shares, fee, cash_spent)`` for one BUY fill at one price."""
    notional = gross_shares * price
    if role is LiquidityRole.MAKER and fee.taker_only:
        return gross_shares, FeeBreakdown(ZERO, ZERO, ZERO), notional
    fee_equiv = fee_usd(gross_shares, price, fee)
    if fee.collection is FeeCollection.SHARES_ON_BUY:
        # Fee over price does not terminate. Conditional tokens have 6 decimals, so rounding up
        # to that keeps every later sum exact and never understates the fee.
        fee_shares = (fee_equiv / price).quantize(SHARE_QUANTUM, rounding=ROUND_CEILING)
        return (
            gross_shares - fee_shares,
            FeeBreakdown(ZERO, fee_shares, fee_equiv),
            notional,
        )
    return gross_shares, FeeBreakdown(fee_equiv, ZERO, fee_equiv), notional + fee_equiv


def sell_fill_economics(
    gross_shares: Decimal, price: Decimal, fee: FeeParams, role: LiquidityRole
) -> tuple[FeeBreakdown, Decimal]:
    """Return ``(fee, cash_received)``. SELL fees are always paid in cash."""
    notional = gross_shares * price
    if role is LiquidityRole.MAKER and fee.taker_only:
        return FeeBreakdown(ZERO, ZERO, ZERO), notional
    fee_equiv = fee_usd(gross_shares, price, fee)
    return FeeBreakdown(fee_equiv, ZERO, fee_equiv), notional - fee_equiv
