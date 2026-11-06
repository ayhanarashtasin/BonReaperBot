"""Resolution, merge and redemption simulation. Outcomes come only from official venue fields."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ..accounting.position import PaperAccount
from ..types import MarketSpec, MarketVersion, Outcome


class ResolutionUnknownError(ValueError):
    pass


def winning_outcome_from_resolution(payload: dict[str, Any], spec: MarketSpec) -> Outcome:
    """Read the winner from official outcome prices. Label order is checked, never assumed."""
    if not payload.get("closed"):
        raise ResolutionUnknownError("market is not closed")
    yes_label = str(payload.get("yes_label", "")).strip().lower()
    no_label = str(payload.get("no_label", "")).strip().lower()
    prices = {yes_label: payload.get("yes_price"), no_label: payload.get("no_price")}
    up = prices.get(spec.yes_label.lower())
    down = prices.get(spec.no_label.lower())
    if up is None or down is None:
        raise ResolutionUnknownError("outcome labels do not match the verified market")
    up_d, down_d = Decimal(str(up)), Decimal(str(down))
    if up_d == 1 and down_d == 0:
        return Outcome.YES
    if up_d == 0 and down_d == 1:
        return Outcome.NO
    raise ResolutionUnknownError(f"outcome prices are not final: {up_d}/{down_d}")


def merge_supported(spec: MarketSpec) -> bool:
    """Both current protocol versions support complete-set merges on binary markets."""
    return spec.version in (MarketVersion.CTF, MarketVersion.V2)


@dataclass(frozen=True, slots=True)
class SettlementResult:
    market_id: str
    winning: Outcome
    payout: Decimal
    redeemable_until_redeemed: Decimal


def settle_market(
    account: PaperAccount, spec: MarketSpec, payload: dict[str, Any], ts_utc_ms: int
) -> SettlementResult:
    winning = winning_outcome_from_resolution(payload, spec)
    payout = account.settle(spec, winning, ts_utc_ms)
    return SettlementResult(spec.market_id, winning, payout, account.cash_redeemable)
