"""Fair-value research pipeline (BR-501 to BR-505): dataset, chronological evaluation, audit.

Forecast evaluation uses official labels and strike values from closed venue events, with Binance
1-minute closes as a *clearly labelled proxy* for the price path. Historical Chainlink TWAP is not
available in bulk. The economic audit replays recorded L2 data with the real TWAP feed instead.

A better forecast is not a profitable strategy. The audit decides on after-cost simulated results
only, and missing or thin data can only ever produce NO-GO or INCONCLUSIVE.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from ..connectors.polymarket_public import ReadOnlyTransport
from ..market.rules import Instrument
from ..settings import Settings, StrategiesConfig
from ..storage.raw_events import read_events
from ..types import MarketSpec, Mode
from .experiments import SCENARIOS, market_specs
from .metrics import compute_metrics
from .probability import (
    FeatureRow,
    LogisticModel,
    PricePoint,
    assert_no_leakage,
    baseline_probability,
    build_features,
    chronological_market_split,
    evaluate,
    fit_logistic,
    load_model,
    reliability_svg,
    reliability_table,
    save_model,
)
from .replay import SimCore, replay_events
from .reports import build_manifest, git_commit, write_json, write_research_json

KLINES_URL = "https://api.binance.com/api/v3/klines"
DECISION_OFFSETS_S = (60, 120, 180, 240)
SOURCE_PROXY = "binance_spot_1m_proxy_with_official_labels"
MIN_MARKETS_FOR_ECONOMIC_DECISION = 30
MIN_TEST_MARKETS_FOR_MODEL = 100


@dataclass(frozen=True, slots=True)
class HistoricalMarket:
    slug: str
    start_ms: int
    end_ms: int
    price_to_beat: str | None
    final_price: str | None
    up_won: bool
    yes_asset_id: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


def _parse_market(event: Any) -> HistoricalMarket | None:
    """Keep only markets with an official final binary outcome and an exact 5-minute window."""
    if len(event.markets) != 1 or not event.slug:
        return None
    market = event.markets[0]
    start = event.schedule.start_time
    end = event.schedule.end_date
    if start is None or end is None or (end - start).total_seconds() != 300:
        return None
    labels = {
        market.outcomes.yes.label.strip().lower(): market.outcomes.yes,
        market.outcomes.no.label.strip().lower(): market.outcomes.no,
    }
    up = labels.get("up")
    down = labels.get("down")
    if up is None or down is None or up.price is None or down.price is None:
        return None
    if {up.price, down.price} != {Decimal(0), Decimal(1)} or not market.state.closed:
        return None
    metadata = event.metadata or {}
    return HistoricalMarket(
        slug=event.slug,
        start_ms=int(start.timestamp() * 1000),
        end_ms=int(end.timestamp() * 1000),
        price_to_beat=str(metadata["priceToBeat"]) if "priceToBeat" in metadata else None,
        final_price=str(metadata["finalPrice"]) if "finalPrice" in metadata else None,
        up_won=up.price == 1,
        yes_asset_id=str(up.token_id or up.position_id),
    )


async def fetch_markets(
    transport: ReadOnlyTransport, instrument: Instrument, limit: int
) -> list[HistoricalMarket]:
    series_id = await transport.series_id(instrument.series_slug)
    pager = transport.client.list_events(
        series_ids=series_id, closed=True, order="endDate", ascending=False, page_size=100
    )
    markets: list[HistoricalMarket] = []
    async for page in pager:
        for event in page.items:
            parsed = _parse_market(event)
            if parsed is not None:
                markets.append(parsed)
        if len(markets) >= limit:
            break
        await asyncio.sleep(0.1)
    return sorted(markets[:limit], key=lambda m: m.start_ms)


def fetch_binance_closes(start_ms: int, end_ms: int) -> list[PricePoint]:
    """BTCUSDT 1-minute closes. The timestamp is the candle's close, the moment it became known."""
    points: list[PricePoint] = []
    cursor = start_ms
    with httpx.Client(timeout=20.0, trust_env=False) as client:
        while cursor < end_ms:
            response = client.get(
                KLINES_URL,
                params={
                    "symbol": "BTCUSDT",
                    "interval": "1m",
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": 1000,
                },
            )
            response.raise_for_status()
            rows = response.json()
            if not rows:
                break
            points.extend(PricePoint(int(r[6]), float(r[4])) for r in rows)
            cursor = int(rows[-1][6]) + 1
    return points


async def fetch_market_prices(
    transport: ReadOnlyTransport, market: HistoricalMarket
) -> list[PricePoint]:
    """The venue's own Up price history for one market, used as the benchmark forecast."""
    pager = transport.client.list_price_history(
        asset_id=market.yes_asset_id,
        start=datetime.fromtimestamp(market.start_ms / 1000, tz=UTC),
        end=datetime.fromtimestamp(market.end_ms / 1000, tz=UTC),
        bucket_seconds=60,
    )
    points = []
    async for page in pager:
        for item in page.items:
            points.append(PricePoint(int(item.timestamp.timestamp() * 1000), float(item.price)))
    return sorted(points, key=lambda p: p.ts_ms)


def build_rows(
    markets: Sequence[HistoricalMarket], closes: Sequence[PricePoint]
) -> tuple[list[FeatureRow], int]:
    """Rows at fixed offsets into each window. Returns the rows and how many were unusable."""
    ordered = sorted(closes, key=lambda p: p.ts_ms)
    rows: list[FeatureRow] = []
    skipped = 0
    low = 0
    for market in sorted(markets, key=lambda m: m.start_ms):
        while low < len(ordered) and ordered[low].ts_ms < market.start_ms - 1_000_000:
            low += 1
        recent = []
        for point in ordered[low:]:
            if point.ts_ms >= market.end_ms:
                break
            recent.append(point)
        before_start = [p for p in recent if p.ts_ms <= market.start_ms]
        if not before_start:
            skipped += len(DECISION_OFFSETS_S)
            continue
        # Binance price at the window start. The official strike is a Chainlink level, and mixing
        # the two would add their basis to the distance feature.
        strike = before_start[-1].price
        for offset in DECISION_OFFSETS_S:
            decision = market.start_ms + offset * 1000
            history = [p for p in recent if p.ts_ms <= decision]
            row = build_features(
                market_slug=market.slug,
                market_end_ms=market.end_ms,
                window_seconds=300,
                decision_ms=decision,
                strike=strike,
                history=history,
                label_up=int(market.up_won),
                source=SOURCE_PROXY,
            )
            if row is None:
                skipped += 1
            else:
                rows.append(row)
    return rows, skipped


def market_probability_lookup(
    prices: dict[str, list[PricePoint]],
) -> Callable[[FeatureRow], float | None]:
    def lookup(row: FeatureRow) -> float | None:
        series = prices.get(row.market_slug, [])
        known = [p for p in series if p.ts_ms <= row.decision_ms]
        if not known:
            return None
        return min(max(known[-1].price, 0.001), 0.999)

    return lookup


def _strip(report: dict[str, object]) -> dict[str, object]:
    return {k: v for k, v in report.items() if k != "reliability"}


def _by_horizon(
    rows: Sequence[FeatureRow], predict: Callable[[FeatureRow], float], label: str
) -> list[dict[str, object]]:
    out = []
    for offset in DECISION_OFFSETS_S:
        subset = [r for r in rows if abs(r.seconds_left - (300 - offset)) < 0.5]
        out.append({"elapsed_s": offset, **_strip(evaluate(subset, predict, label))})
    return out


def run_forecast_research(
    markets: Sequence[HistoricalMarket],
    closes: Sequence[PricePoint],
    market_prices: dict[str, list[PricePoint]],
    output_dir: Path,
) -> dict[str, Any]:
    rows, skipped = build_rows(markets, closes)
    if not rows:
        raise ValueError("no usable rows")
    split = chronological_market_split(rows)
    assert_no_leakage(split, rows)
    groups = {
        "train": [r for r in rows if r.market_slug in set(split.train)],
        "validation": [r for r in rows if r.market_slug in set(split.validation)],
        "test": [r for r in rows if r.market_slug in set(split.test)],
    }
    model = fit_logistic(groups["train"])
    output_dir.mkdir(parents=True, exist_ok=True)
    model_hash = save_model(model, output_dir / "logistic.json")
    evaluations: dict[str, dict[str, object]] = {}
    for name, subset in groups.items():
        evaluations[name] = {
            "baseline": evaluate(subset, baseline_probability, "baseline_random_walk"),
            "logistic": evaluate(subset, model.predict, "logistic"),
        }
    lookup = market_probability_lookup(market_prices)
    comparable = [r for r in groups["test"] if lookup(r) is not None]

    def market_or_half(row: FeatureRow) -> float:
        value = lookup(row)
        return 0.5 if value is None else value

    benchmark = {
        "rows": len(comparable),
        "market_price": _strip(evaluate(comparable, market_or_half, "market_price")),
        "baseline": _strip(evaluate(comparable, baseline_probability, "baseline_random_walk")),
        "logistic": _strip(evaluate(comparable, model.predict, "logistic")),
    }
    verdict = forecast_verdict(evaluations, benchmark, len(split.test))
    report = {
        "generated_utc": datetime.now(tz=UTC).isoformat(),
        "code_commit": git_commit(),
        "feature_source": SOURCE_PROXY,
        "caveat": (
            "Features use Binance BTCUSDT 1-minute closes as a proxy; labels are official outcomes "
            "from Chainlink TWAP settlement. Live inputs would differ. A better forecast is not a "
            "profitable strategy."
        ),
        "markets": len({r.market_slug for r in rows}),
        "rows": len(rows),
        "rows_skipped_missing_data": skipped,
        "period_start_utc_ms": min(r.market_end_ms for r in rows) - 300_000,
        "period_end_utc_ms": max(r.market_end_ms for r in rows),
        "split": {k: len(v) for k, v in asdict(split).items()},
        "embargo_markets": 2,
        "base_rate_up": sum(r.label_up or 0 for r in rows) / len(rows),
        "model_hash": model_hash,
        "model": asdict(model),
        "evaluation": evaluations,
        "test_by_horizon": {
            "baseline": _by_horizon(groups["test"], baseline_probability, "baseline"),
            "logistic": _by_horizon(groups["test"], model.predict, "logistic"),
        },
        "benchmark_vs_market_on_test": benchmark,
        "verdict": verdict,
    }
    write_research_json(output_dir / "forecast_report.json", report)
    for label, predict in (("baseline", baseline_probability), ("logistic", model.predict)):
        pairs = [(predict(r), int(r.label_up or 0)) for r in groups["test"]]
        (output_dir / f"reliability_{label}.svg").write_text(
            reliability_svg(reliability_table(pairs), f"{label}: untouched test"), encoding="utf-8"
        )
    return report


def forecast_verdict(
    evaluations: dict[str, dict[str, object]], benchmark: dict[str, Any], test_markets: int
) -> dict[str, object]:
    """Adopt only what beats the simpler model out of sample on both proper scoring rules."""

    def score(partition: str, model: str, metric: str) -> float:
        section = evaluations[partition][model]
        if not isinstance(section, dict):
            raise TypeError(section)
        return float(section[metric])

    enough = test_markets >= MIN_TEST_MARKETS_FOR_MODEL
    beats_baseline = all(
        score(p, "logistic", m) < score(p, "baseline", m)
        for p in ("validation", "test")
        for m in ("brier", "log_loss")
    )
    market_section = benchmark["market_price"]
    logistic_section = benchmark["logistic"]
    has_market = benchmark["rows"] > 0
    beats_market = bool(
        has_market and float(logistic_section["brier"]) < float(market_section["brier"])
    )
    return {
        "enough_test_markets": enough,
        "logistic_beats_baseline_out_of_sample": beats_baseline,
        "logistic_beats_market_price_on_test": beats_market if has_market else None,
        "preferred_forecast_model": "logistic" if (enough and beats_baseline) else "baseline",
        "note": "Forecast scores only. Economic use is decided by the separate audit.",
    }


# --- quote integration and economic audit (BR-504, BR-505) --------------------------------------


def reference_probability(
    core: SimCore,
    predict: Callable[[FeatureRow], float],
    *,
    delay_ms: int = 0,
) -> Callable[[], float | None]:
    """A live P(Up) from the Chainlink TWAP in the simulator. ``None`` means use the midpoint."""

    def probability() -> float | None:
        now = core.scheduler.now - delay_ms
        spec = core._active_spec(core.scheduler.now)
        if spec is None:
            return None
        strike = core.reference.observed_strike(spec.start_utc_ms)
        if strike is None:
            return None
        history = [
            PricePoint(ts, float(value))
            for ts, value, _received in core.reference.history
            if now - 900_000 <= ts <= now
        ]
        if not history or now - history[-1].ts_ms > core.limits.max_reference_age_ms + delay_ms:
            return None
        row = build_features(
            market_slug=spec.slug,
            market_end_ms=spec.end_utc_ms,
            window_seconds=spec.duration_minutes * 60,
            decision_ms=min(now, spec.end_utc_ms - 1),
            strike=float(strike),
            history=history,
            label_up=None,
            source="chainlink_twap",
        )
        return predict(row) if row is not None else None

    return probability


def _audit_settings(settings: Settings, model_path: str, *, model_on: bool) -> Settings:
    strategies = StrategiesConfig.model_validate(
        settings.strategies.model_dump(mode="python")
        | {
            "passive_quotes": True,
            "scan_pair": False,
            "allow_taker_taker": False,
            "allow_negative_edge_risk_hedge": False,
            "max_hedge_loss_usd": Decimal(0),
            "probability_model": model_on,
            "model_path": model_path if model_on else None,
        }
    )
    return settings.model_copy(update={"strategies": strategies, "mode": Mode.PAPER})


def _audit_row(variant: str, scenario: str, metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "variant": variant,
        "scenario": scenario,
        "realized_pnl": metrics["pnl"]["realized_settled"],
        "unresolved_cost": metrics["pnl"]["unresolved_cost_at_risk"],
        "markets_traded": metrics["markets_traded"],
        "fills": metrics["fills"]["count"],
        "max_drawdown": metrics["equity"]["max_drawdown"],
        "markout_5s": metrics["markout_avg_per_share"]["5s"],
        "unpaired_close_fraction": metrics["exposure"]["fraction_traded_markets_unpaired_at_close"],
        "fair_value_sources": metrics["fair_value_sources"],
        "ledger_errors": len(metrics["ledger"]["conservation_errors"]),
        "checksum": metrics["checksum"],
    }


def audit_decision(rows: Iterable[dict[str, Any]], resolved_markets: int) -> dict[str, object]:
    """GO only if a model beats the midpoint baseline under every scenario, on enough markets."""
    table = list(rows)
    baseline = {r["scenario"]: r for r in table if r["variant"] == "midpoint_baseline"}
    decisions: dict[str, object] = {}
    any_go = False
    for variant in sorted({r["variant"] for r in table} - {"midpoint_baseline"}):
        mine = [r for r in table if r["variant"] == variant and r["scenario"] in baseline]
        better_everywhere = bool(mine) and all(
            Decimal(str(r["realized_pnl"])) > Decimal(str(baseline[r["scenario"]]["realized_pnl"]))
            for r in mine
        )
        decisions[variant] = {"beats_midpoint_in_all_scenarios": better_everywhere}
        any_go = any_go or better_everywhere
    if resolved_markets < MIN_MARKETS_FOR_ECONOMIC_DECISION:
        status = "INCONCLUSIVE"
        reason = (
            f"only {resolved_markets} resolved markets; at least "
            f"{MIN_MARKETS_FOR_ECONOMIC_DECISION} are required before any economic conclusion"
        )
    elif any_go:
        status = "CANDIDATE"
        reason = "beat the midpoint in simulation; still needs fresh held-out data and review"
    else:
        status = "NO-GO"
        reason = "no model variant beat the midpoint baseline in every scenario"
    return {
        "status": status,
        "reason": reason,
        "model_enabled_by_default": False,
        "variants": decisions,
        "note": "The probability model stays disabled unless an operator explicitly enables it.",
    }


AUDIT_VARIANTS = (
    "midpoint_baseline",
    "random_walk_model",
    "logistic_model",
    "logistic_model_delayed_twap_5s",
)


def _audit_job(
    variant: str, scenario: str, input_dir: Path, settings: Settings, model_path: Path, seed: int
) -> tuple[dict[str, Any], int]:
    """One replay. Top-level, so it can run in a worker process."""
    model = load_model(model_path)
    if variant == "midpoint_baseline":
        core = replay_events(
            read_events(input_dir, verify=False),
            _audit_settings(settings, str(model_path), model_on=False),
            SCENARIOS[scenario],
            seed=seed,
            strategy_id=variant,
        )
        return _audit_row(variant, scenario, compute_metrics(core)), len(core.resolved)
    predict = baseline_probability if variant == "random_walk_model" else model.predict
    delay = 5000 if variant == "logistic_model_delayed_twap_5s" else 0

    def factory(sim: SimCore) -> Callable[[], float | None]:
        return reference_probability(sim, predict, delay_ms=delay)

    core = replay_events(
        read_events(input_dir, verify=False),
        _audit_settings(settings, str(model_path), model_on=True),
        SCENARIOS[scenario],
        seed=seed,
        strategy_id=variant,
        probability_factory=factory,
        model_hash=model.model_hash,
    )
    return _audit_row(variant, scenario, compute_metrics(core)), len(core.resolved)


def run_model_audit(
    input_dir: Path,
    settings: Settings,
    model_path: Path,
    output_dir: Path,
    seed: int,
    workers: int = 1,
) -> dict[str, Any]:
    model: LogisticModel = load_model(model_path)
    specs: list[MarketSpec] = market_specs(read_events(input_dir, sources=["control"]))
    jobs = [(v, s) for s in ("pessimistic", "central") for v in AUDIT_VARIANTS]
    count = len(jobs)
    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            results = list(
                pool.map(
                    _audit_job,
                    [v for v, _ in jobs],
                    [s for _, s in jobs],
                    [input_dir] * count,
                    [settings] * count,
                    [model_path] * count,
                    [seed] * count,
                )
            )
    else:
        results = [_audit_job(v, s, input_dir, settings, model_path, seed) for v, s in jobs]
    rows = [row for row, _ in results]
    resolved = max(count for _, count in results)
    decision = audit_decision([r for r in rows if "delayed" not in r["variant"]], resolved)
    manifest = build_manifest(
        "model_audit",
        input_dir,
        settings,
        seed,
        coverage=(("markets", str(len(specs))), ("resolved", str(resolved))),
        fee_versions=[s.fee.version for s in specs],
        model_hash=model.model_hash,
    )
    report = {
        "simulated": True,
        "disclaimer": "SIMULATED, unverified queue assumptions. Not real earnings.",
        "model_hash": model.model_hash,
        "rows": rows,
        "decision": decision,
    }
    write_json(output_dir / "model_audit.json", report)
    write_json(output_dir / "model_audit_manifest.json", manifest)
    return report
