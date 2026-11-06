"""Research metrics from a finished simulation. Output is canonical JSON, so identical inputs give
byte-identical results (FR-09). All figures are simulated."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..accounting.pnl import locked_pair_pnl, market_pnl, realized_total
from ..types import ZERO, canonical_json, sha256_hex, to_jsonable
from .replay import MARKOUT_HORIZONS_MS, SimCore

DISCLAIMER = (
    "SIMULATED. Not real earnings. Fills depend on stated queue and latency assumptions that have "
    "not been validated against real fills."
)


def _max_drawdown(timeline: list[dict[str, Any]]) -> Decimal:
    peak: Decimal | None = None
    worst = ZERO
    for point in timeline:
        equity = point["equity"]
        peak = equity if peak is None else max(peak, equity)
        worst = max(worst, peak - equity)
    return worst


def _avg(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, ZERO) / len(values)


def _distribution(values: list[Decimal]) -> dict[str, Any]:
    """Spread of per-market results, so that a total is never read without its uncertainty."""
    count = len(values)
    if count == 0:
        return {"count": 0}
    mean = sum(values, ZERO) / count
    out: dict[str, Any] = {
        "count": count,
        "mean": mean,
        "winners": sum(1 for v in values if v > 0),
        "losers": sum(1 for v in values if v < 0),
        "best": max(values),
        "worst": min(values),
    }
    if count > 1:
        variance = sum(((v - mean) ** 2 for v in values), ZERO) / (count - 1)
        stderr = (variance / count).sqrt()
        out["stderr"] = stderr
        out["mean_95ci_low"] = mean - 2 * stderr
        out["mean_95ci_high"] = mean + 2 * stderr
    return out


def _books_mid(core: SimCore, asset: str) -> Decimal | None:
    book = core.exchange.books.books.get(asset)
    if book is None:
        return None
    state = book.state()
    if state.best_bid is None or state.best_ask is None:
        return None
    return (state.best_bid + state.best_ask) / 2


def compute_metrics(core: SimCore) -> dict[str, Any]:
    account = core.account
    per_market: list[dict[str, Any]] = []
    unrealized_total = ZERO
    unresolved_exposure = ZERO
    for spec in sorted(core.specs.values(), key=lambda s: s.start_utc_ms):
        yes_mid = _books_mid(core, spec.yes_asset_id)
        no_mid = _books_mid(core, spec.no_asset_id)
        pnl = market_pnl(account, spec, yes_mid, no_mid)
        flows = account.flows[spec.market_id]
        close = core.market_close_inventory.get(spec.market_id, {})
        if pnl.unrealized_mtm is not None and not flows.settled:
            unrealized_total += pnl.unrealized_mtm
        if not flows.settled:
            unresolved_exposure += flows.yes_cost + flows.no_cost
        per_market.append(
            {
                "market_id": spec.market_id,
                "slug": spec.slug,
                "realized_pnl": pnl.realized,
                "unrealized_mtm": pnl.unrealized_mtm,
                "locked_pair_pnl_unsettled": locked_pair_pnl(account, spec)
                if not flows.settled
                else None,
                "yes_gross": flows.yes_gross,
                "no_gross": flows.no_gross,
                "cost": flows.yes_cost + flows.no_cost,
                "fees_usd_equivalent": flows.fees_usd_equivalent,
                "settled": flows.settled,
                "winning": flows.winning,
                "payout": flows.payout,
                "paired_at_close": close.get("paired"),
                "unpaired_yes_at_close": close.get("unpaired_yes"),
                "unpaired_no_at_close": close.get("unpaired_no"),
            }
        )
    realized = realized_total(account)
    stats = core.fill_stats
    traded = [m for m in per_market if m["yes_gross"] > 0 or m["no_gross"] > 0]
    unpaired_markets = [
        m
        for m in traded
        if (m["unpaired_yes_at_close"] or ZERO) > 0 or (m["unpaired_no_at_close"] or ZERO) > 0
    ]
    timeline = core.timeline
    recon = core.reconciliations
    outcomes = [m["realized_pnl"] for m in traded if m["realized_pnl"] is not None]
    metrics: dict[str, Any] = {
        "simulated": True,
        "disclaimer": DISCLAIMER,
        "scenario": core.scenario.describe(),
        "seed": core.seed,
        "strategy_id": core.orchestrator.strategy_id,
        "events_processed": core.events_processed,
        "markets_total": len(core.specs),
        "markets_traded": len(traded),
        "markets_unresolved": sorted(core.unresolved),
        "pnl": {
            "realized_settled": realized,
            "unrealized_mtm_unresolved": unrealized_total,
            "unresolved_cost_at_risk": unresolved_exposure,
            "fees_usd_equivalent": stats.fees_usd_equivalent,
            "rebates_counted": "0 (never pre-counted)",
            "per_traded_market_avg": realized / len(traded) if traded else None,
        },
        "per_traded_market": _distribution(outcomes),
        "equity": {
            "starting": account.starting_cash,
            "ending_cash_available": account.cash_available,
            "ending_cash_reserved": account.cash_reserved,
            "ending_cash_redeemable": account.cash_redeemable,
            "max_drawdown": _max_drawdown(timeline),
            "samples": len(timeline),
        },
        "exposure": {
            "markets_with_unpaired_at_close": len(unpaired_markets),
            "fraction_traded_markets_unpaired_at_close": (
                Decimal(len(unpaired_markets)) / len(traded) if traded else None
            ),
            "time_with_unpaired_inventory_ms": core._exposed_ms,
            "time_sampled_ms": core._sampled_ms,
        },
        "fills": {
            "count": stats.count,
            "maker": stats.maker,
            "taker": stats.taker,
            "by_purpose": dict(stats.by_purpose),
            "gross_shares": stats.gross_shares,
            "notional": stats.notional,
            "hedge_cost": stats.hedge_cost,
            "hedge_loss_budget_used": core.risk.hedge_loss_used,
        },
        "markout_avg_per_share": {
            f"{h // 1000}s": _avg(stats.markouts[h]) for h in MARKOUT_HORIZONS_MS
        },
        "markout_samples": {f"{h // 1000}s": len(stats.markouts[h]) for h in MARKOUT_HORIZONS_MS},
        "orders": {
            "by_state": core.order_state_counts(),
            "by_purpose": core.order_purpose_counts(),
            "risk_rejected": core.rejected_orders(),
            "risk_rejection_reasons": dict(sorted(core.risk.rejections_by_reason.items())),
            "exchange": dict(sorted(core.exchange.stats.items())),
            "unknown_remaining": len(core.oms.unknown_orders),
            "orphan_fills": len(core.oms.orphan_fills),
            "anomalies": core.oms.anomalies,
        },
        "latency_ms": core.latency_report(),
        "reconciliation": {
            "runs": len(recon),
            "clean": sum(1 for r in recon if r.clean),
            "max_abs_cash_diff": max((abs(r.cash_diff) for r in recon), default=ZERO),
            "position_diff_runs": sum(1 for r in recon if r.position_diffs),
        },
        "ledger": {
            "entries": len(account.ledger.entries),
            "conservation_errors": account.ledger.conservation_errors(),
        },
        "safety": {
            "kill_events": [list(h) for h in core.kill.history],
            "kill_active_at_end": core.kill.reasons,
            "alerts": core.orchestrator.alerts.records,
            "visibility_violations": core.visibility_violations,
            "port_is_real": core.port.is_real,
        },
        "data": {
            "gaps": len(core.gaps),
            "market_gaps": sum(1 for g in core.gaps if g.get("market_id")),
            "book_invalidations": dict(sorted(core.invalidations.items())),
            "book_validity_at_end": core.book_validity_counts(),
        },
        "scan": core.orchestrator.scan.summary(),
        "fair_value_sources": core.orchestrator.fair_sources,
        "per_market": per_market,
    }
    metrics["checksum"] = sha256_hex(canonical_json(metrics))
    return metrics


def timeline_records(core: SimCore) -> list[Any]:
    return [to_jsonable(p) for p in core.timeline]
