"""Replay command, sensitivity matrix and chronological walk-forward partitions (BR-308/309/406)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..settings import Settings, StrategiesConfig
from ..storage.raw_events import read_events
from ..types import EventEnvelope, MarketSpec, Mode, from_jsonable
from .maker_queue import QueueScenario
from .metrics import compute_metrics, timeline_records
from .replay import SimScenario, replay_events
from .reports import build_manifest, manifest_input_hash, money, write_json

SCENARIOS: dict[str, SimScenario] = {
    "pessimistic": SimScenario("pessimistic", QueueScenario.PESSIMISTIC),
    "central": SimScenario("central", QueueScenario.CENTRAL),
    "optimistic": SimScenario("optimistic", QueueScenario.OPTIMISTIC),
    "pessimistic_2x_latency": SimScenario(
        "pessimistic_2x_latency", QueueScenario.PESSIMISTIC, latency_factor=2
    ),
    "central_2x_latency": SimScenario(
        "central_2x_latency", QueueScenario.CENTRAL, latency_factor=2
    ),
    "central_higher_fees": SimScenario(
        "central_higher_fees", QueueScenario.CENTRAL, fee_multiplier=Decimal("1.5")
    ),
    "central_late_hedge": SimScenario(
        "central_late_hedge", QueueScenario.CENTRAL, hedge_floor_seconds=25
    ),
}


def _with_strategies(settings: Settings, **changes: Any) -> Settings:
    strategies = StrategiesConfig.model_validate(
        settings.strategies.model_dump(mode="python") | changes
    )
    return settings.model_copy(update={"strategies": strategies, "mode": Mode.PAPER})


def strategy_variants(settings: Settings) -> dict[str, Settings]:
    return {
        "scanner_only": _with_strategies(
            settings,
            scan_pair=True,
            passive_quotes=False,
            allow_taker_taker=False,
            allow_negative_edge_risk_hedge=False,
            max_hedge_loss_usd=Decimal(0),
        ),
        "taker_pair_research": _with_strategies(
            settings,
            scan_pair=True,
            passive_quotes=False,
            allow_taker_taker=True,
            min_net_edge_usd=Decimal("0.01"),
            min_net_roi=Decimal("0.001"),
        ),
        "passive_maker": _with_strategies(
            settings,
            passive_quotes=True,
            allow_taker_taker=False,
            allow_negative_edge_risk_hedge=False,
            max_hedge_loss_usd=Decimal(0),
        ),
        "passive_constrained_hedge": _with_strategies(
            settings,
            passive_quotes=True,
            allow_taker_taker=False,
            allow_negative_edge_risk_hedge=True,
            max_hedge_loss_usd=Decimal("2"),
        ),
    }


def load_all(input_dir: Path) -> list[EventEnvelope]:
    return list(read_events(input_dir))


def market_specs(events: list[EventEnvelope]) -> list[MarketSpec]:
    specs: dict[str, MarketSpec] = {}
    for env in events:
        if env.topic == "market_spec":
            spec = from_jsonable(MarketSpec, env.payload()["spec"])
            specs.setdefault(spec.market_id, spec)
    return sorted(specs.values(), key=lambda s: s.start_utc_ms)


@dataclass(frozen=True, slots=True)
class Partition:
    name: str
    market_ids: frozenset[str]
    slugs: tuple[str, ...]


def chronological_partitions(specs: list[MarketSpec], embargo_markets: int = 1) -> list[Partition]:
    """60/20/20 by market, in time order, with embargoed markets dropped between partitions."""
    usable = len(specs)
    train_end = (usable * 6) // 10
    validation_end = (usable * 8) // 10
    groups = (
        ("train", specs[:train_end]),
        ("validation", specs[train_end + embargo_markets : validation_end]),
        ("untouched_test", specs[validation_end + embargo_markets :]),
    )
    return [
        Partition(name, frozenset(s.market_id for s in part), tuple(s.slug for s in part))
        for name, part in groups
    ]


def _period(events: list[EventEnvelope]) -> tuple[int | None, int | None]:
    return (events[0].received_utc_ms, events[-1].received_utc_ms) if events else (None, None)


def coverage_grade(specs: list[MarketSpec], metrics: dict[str, Any], events_total: int) -> str:
    """A label for how much a result can be trusted, driven only by the data itself (R-07)."""
    resolved = len(specs) - len(metrics["markets_unresolved"])
    if events_total == 0 or not specs:
        return "F: no data"
    if metrics["data"]["gaps"] > 0:
        return "C: discontinuities present"
    if resolved < 30:
        return "B-: complete capture but too few markets for any economic conclusion"
    return "B: continuous capture, conclusions still need out-of-sample review"


def run_replay_command(
    input_dir: Path, settings: Settings, output_dir: Path, seed: int
) -> dict[str, Any]:
    if settings.mode is not Mode.PAPER:
        settings = settings.model_copy(update={"mode": Mode.PAPER})
    events = load_all(input_dir)
    specs = market_specs(events)
    core = replay_events(events, settings, seed=seed)
    metrics = compute_metrics(core)
    grade = coverage_grade(specs, metrics, len(events))
    manifest = build_manifest(
        "replay",
        input_dir,
        settings,
        seed,
        coverage=(("markets", str(len(specs))), ("events", str(len(events))), ("grade", grade)),
        period=_period(events),
        fee_versions=[s.fee.version for s in specs],
        known_gaps=[str(g.get("reason")) for g in core.gaps],
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "metrics.json", metrics)
    write_json(output_dir / "timeline.json", timeline_records(core))
    write_json(output_dir / "manifest.json", manifest)
    return {
        "output": str(output_dir),
        "checksum": metrics["checksum"],
        "input_hash": manifest_input_hash(manifest),
        "coverage_grade": grade,
        "pnl": metrics["pnl"],
        "fills": metrics["fills"],
        "reconciliation": metrics["reconciliation"],
        "ledger_errors": metrics["ledger"]["conservation_errors"],
        "disclaimer": metrics["disclaimer"],
    }


def _row(name: str, scenario: str, partition: str, m: dict[str, Any]) -> dict[str, Any]:
    return {
        "strategy": name,
        "scenario": scenario,
        "partition": partition,
        "markets_traded": m["markets_traded"],
        "realized_pnl": m["pnl"]["realized_settled"],
        "unresolved_cost": m["pnl"]["unresolved_cost_at_risk"],
        "fees": m["pnl"]["fees_usd_equivalent"],
        "fills": m["fills"]["count"],
        "maker_fills": m["fills"]["maker"],
        "taker_fills": m["fills"]["taker"],
        "max_drawdown": m["equity"]["max_drawdown"],
        "unpaired_close_fraction": m["exposure"]["fraction_traded_markets_unpaired_at_close"],
        "markout_5s": m["markout_avg_per_share"]["5s"],
        "ledger_errors": len(m["ledger"]["conservation_errors"]),
        "unclean_reconciliations": m["reconciliation"]["runs"] - m["reconciliation"]["clean"],
        "checksum": m["checksum"],
    }


def run_compare(input_dir: Path, settings: Settings, output_dir: Path, seed: int) -> dict[str, Any]:
    """Run every strategy under every scenario on all data, then per chronological partition."""
    events = load_all(input_dir)
    specs = market_specs(events)
    variants = strategy_variants(settings)
    partitions = chronological_partitions(specs)
    rows: list[dict[str, Any]] = []
    grade = "F: no data"
    for name, variant in variants.items():
        scenario_names = ["central"] if name == "scanner_only" else list(SCENARIOS)
        for scenario_name in scenario_names:
            core = replay_events(
                events, variant, SCENARIOS[scenario_name], seed=seed, strategy_id=name
            )
            metrics = compute_metrics(core)
            grade = coverage_grade(specs, metrics, len(events))
            rows.append(_row(name, scenario_name, "all", metrics))
        for scenario_name in ("pessimistic", "central"):
            for partition in partitions:
                if not partition.market_ids or name == "scanner_only":
                    continue
                core = replay_events(
                    events,
                    variant,
                    SCENARIOS[scenario_name],
                    seed=seed,
                    strategy_id=name,
                    market_filter=partition.market_ids.__contains__,
                )
                rows.append(_row(name, scenario_name, partition.name, compute_metrics(core)))
    manifest = build_manifest(
        "compare",
        input_dir,
        settings,
        seed,
        coverage=(("markets", str(len(specs))), ("events", str(len(events))), ("grade", grade)),
        period=_period(events),
        fee_versions=[s.fee.version for s in specs],
    )
    report = {
        "simulated": True,
        "disclaimer": "SIMULATED, unverified queue assumptions. Not real earnings.",
        "coverage_grade": grade,
        "partitions": [
            {"name": p.name, "markets": len(p.market_ids), "slugs": p.slugs} for p in partitions
        ],
        "rows": rows,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "compare.json", report)
    write_json(output_dir / "manifest.json", manifest)
    (output_dir / "compare.md").write_text(render_compare(report, manifest), encoding="utf-8")
    return {
        "output": str(output_dir),
        "rows": len(rows),
        "coverage_grade": grade,
        "input_hash": manifest_input_hash(manifest),
    }


def render_compare(report: dict[str, Any], manifest: Any) -> str:
    lines = [
        "# Strategy comparison (simulated)",
        "",
        f"> {report['disclaimer']}",
        "",
        f"- Coverage grade: **{report['coverage_grade']}**",
        f"- Code: `{manifest.code_commit}`",
        f"- Config hash: `{manifest.config_hash}`",
        f"- SDK: `{manifest.sdk_version}`",
        f"- Fee schedule: `{', '.join(manifest.fee_versions) or 'n/a'}`",
        f"- Seed: `{manifest.seed}`",
        f"- Input hash: `{manifest_input_hash(manifest)}`",
        "",
        "## Partitions (chronological, one embargoed market between each)",
        "",
    ]
    lines += [f"- {p['name']}: {p['markets']} markets" for p in report["partitions"]]
    lines += [
        "",
        "## Results",
        "",
        "| Strategy | Scenario | Partition | Traded mkts | Realized | Unresolved cost | Fees | "
        "Maker/Taker fills | Max DD | Unpaired at close | 5s markout |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in report["rows"]:
        unpaired = row["unpaired_close_fraction"]
        lines.append(
            f"| {row['strategy']} | {row['scenario']} | {row['partition']} | "
            f"{row['markets_traded']} | {money(row['realized_pnl'])} | "
            f"{money(row['unresolved_cost'])} | {money(row['fees'])} | "
            f"{row['maker_fills']}/{row['taker_fills']} | {money(row['max_drawdown'])} | "
            f"{money(unpaired)} | {money(row['markout_5s'])} |"
        )
    lines += ["", "## Assumptions", ""]
    lines += [f"- {a}" for a in manifest.assumptions]
    lines.append("")
    return "\n".join(lines)
