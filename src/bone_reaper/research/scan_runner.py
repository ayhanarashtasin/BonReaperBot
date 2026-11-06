"""Run the scanner over recorded data and export every candidate and rejection (BR-205/206)."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import polymarket

from ..market.book import BookManager
from ..settings import Settings
from ..storage.raw_events import load_manifest, read_events
from ..strategy.pair_scanner import ScanThresholds, evaluate_pair
from ..types import ArbCandidate, MarketSpec, from_jsonable, sha256_hex, to_jsonable

EVALUATE_TOPICS = {"book", "price_change", "resnapshot"}


def thresholds_from(settings: Settings) -> ScanThresholds:
    return ScanThresholds(
        min_net_edge_usd=settings.strategies.min_net_edge_usd,
        min_net_roi=settings.strategies.min_net_roi,
        max_book_age_ms=settings.risk.max_book_age_ms,
        stop_seconds_before_close=settings.risk.stop_new_quotes_seconds_before_close,
        execution_reserve_per_share=settings.strategies.execution_reserve_per_share,
        allow_taker_taker=settings.strategies.allow_taker_taker,
    )


def _reason_family(reason: str) -> str:
    return (
        reason.split(":", 1)[-1].split(":", 1)[0] if reason.startswith(("yes:", "no:")) else reason
    )


@dataclass(slots=True)
class _Opportunity:
    """Consecutive evaluations of one market and size that stayed positive after the reserve."""

    market_id: str
    q_net: Decimal
    start_ms: int
    last_ms: int
    best_edge: Decimal
    evaluations: int = 1


@dataclass(slots=True)
class ScanAccumulator:
    evaluations: int = 0
    passed: int = 0
    positive_gross: int = 0
    positive_after_fees: int = 0
    positive_after_reserve: int = 0
    reasons: Counter[str] = field(default_factory=Counter)
    best_edge: Decimal | None = None
    best_gross: Decimal | None = None
    size_buckets: dict[str, Counter[str]] = field(default_factory=dict)
    open_runs: dict[tuple[str, str], _Opportunity] = field(default_factory=dict)
    closed_runs: list[_Opportunity] = field(default_factory=list)

    def add(self, candidate: ArbCandidate) -> None:
        self.evaluations += 1
        key = str(candidate.q_net)
        bucket = self.size_buckets.setdefault(key, Counter())
        bucket["evaluated"] += 1
        complete = candidate.yes_leg.gross_shares > 0 and candidate.no_leg.gross_shares > 0
        if complete:
            bucket["fully_fillable"] += 1
            self.best_gross = (
                candidate.gross_edge
                if self.best_gross is None
                else max(self.best_gross, candidate.gross_edge)
            )
            self.best_edge = (
                candidate.edge_after_reserve
                if self.best_edge is None
                else max(self.best_edge, candidate.edge_after_reserve)
            )
            if candidate.gross_edge > 0:
                self.positive_gross += 1
                bucket["positive_gross"] += 1
            if candidate.indicative_edge > 0:
                self.positive_after_fees += 1
                bucket["positive_after_fees"] += 1
            if candidate.edge_after_reserve > 0:
                self.positive_after_reserve += 1
                bucket["positive_after_reserve"] += 1
        if candidate.passed:
            self.passed += 1
            bucket["passed"] += 1
        for reason in candidate.reasons:
            self.reasons[_reason_family(reason)] += 1
        self._track_run(candidate, complete)

    def _track_run(self, candidate: ArbCandidate, complete: bool) -> None:
        key = (candidate.market_id, str(candidate.q_net))
        run = self.open_runs.get(key)
        positive = complete and candidate.edge_after_reserve > 0
        if positive:
            if run is None:
                self.open_runs[key] = _Opportunity(
                    candidate.market_id,
                    candidate.q_net,
                    candidate.ts_utc_ms,
                    candidate.ts_utc_ms,
                    candidate.edge_after_reserve,
                )
            else:
                run.last_ms = candidate.ts_utc_ms
                run.best_edge = max(run.best_edge, candidate.edge_after_reserve)
                run.evaluations += 1
        elif run is not None:
            self.closed_runs.append(run)
            del self.open_runs[key]

    def summary(self) -> dict[str, Any]:
        runs = [*self.closed_runs, *self.open_runs.values()]
        durations = sorted(r.last_ms - r.start_ms for r in runs)
        return {
            "evaluations": self.evaluations,
            "passed_candidates": self.passed,
            "positive_gross_edge": self.positive_gross,
            "positive_after_fees": self.positive_after_fees,
            "positive_after_fees_and_reserve": self.positive_after_reserve,
            "best_gross_edge": self.best_edge if self.best_gross is None else self.best_gross,
            "best_edge_after_reserve": self.best_edge,
            "reject_reason_counts": dict(self.reasons.most_common()),
            "by_q_net": {k: dict(v) for k, v in sorted(self.size_buckets.items(), key=_size_key)},
            "opportunities": len(runs),
            "opportunity_duration_ms_median": durations[len(durations) // 2] if durations else None,
            "opportunity_duration_ms_max": durations[-1] if durations else None,
        }


def _size_key(item: tuple[str, Counter[str]]) -> Decimal:
    return Decimal(item[0])


def run_scan(
    input_dir: Path, settings: Settings, report_path: Path, max_events: int | None = None
) -> dict[str, Any]:
    """Evaluate on every book change, sampled per market at most once per second per asset pair.

    The sampling only bounds report size; it cannot produce a signal the books did not show.
    """
    thresholds = thresholds_from(settings)
    sizes = settings.strategies.scan_sizes
    books = BookManager()
    specs: dict[str, MarketSpec] = {}
    gaps: list[dict[str, Any]] = []
    accumulator = ScanAccumulator()
    last_eval: dict[str, int] = {}
    report_path.parent.mkdir(parents=True, exist_ok=True)
    records_path = report_path.with_suffix(".records.jsonl")
    seen_events = 0
    period_start: int | None = None
    period_end: int | None = None
    fee_versions: set[str] = set()
    with records_path.open("w", encoding="utf-8") as records:
        for env in read_events(input_dir):
            seen_events += 1
            if max_events is not None and seen_events > max_events:
                break
            period_start = env.received_utc_ms if period_start is None else period_start
            period_end = env.received_utc_ms
            payload = env.payload()
            if env.topic == "market_spec":
                spec = from_jsonable(MarketSpec, payload["spec"])
                specs[spec.market_id] = spec
                fee_versions.add(spec.fee.version)
                books.register(spec.market_id, spec.yes_asset_id, spec.tick_size)
                books.register(spec.market_id, spec.no_asset_id, spec.tick_size)
                continue
            if env.topic == "gap":
                gaps.append(payload)
                continue
            if env.source not in {"poly_market", "poly_rest"} or env.topic not in EVALUATE_TOPICS:
                continue
            books.apply(payload, env.received_utc_ms)
            active = specs.get(env.market_id or "")
            if active is None or env.received_utc_ms < active.start_utc_ms:
                continue
            if env.received_utc_ms - last_eval.get(active.market_id, 0) < 1000:
                continue
            yes_book = books.books.get(active.yes_asset_id)
            no_book = books.books.get(active.no_asset_id)
            if yes_book is None or no_book is None:
                continue
            last_eval[active.market_id] = env.received_utc_ms
            for q in sizes:
                candidate = evaluate_pair(
                    active,
                    yes_book.state(),
                    no_book.state(),
                    q,
                    thresholds,
                    env.received_utc_ms,
                )
                accumulator.add(candidate)
                records.write(json.dumps(to_jsonable(candidate), sort_keys=True) + "\n")
    summary = accumulator.summary()
    records_hash = sha256_hex(records_path.read_bytes())
    manifest_hash = sha256_hex((input_dir / "manifest.jsonl").read_bytes())
    report = {
        "generated_by": "bone-reaper scan",
        "execution": "none: the scanner has no order route",
        "interpretation": (
            "candidates are indicative only; two requests are not atomic and no fill is implied"
        ),
        "sdk_version": polymarket.__version__,
        "config_hash": settings.config_hash(),
        "input_manifest_sha256": manifest_hash,
        "parts": len(load_manifest(input_dir)),
        "markets": sorted(s.slug for s in specs.values()),
        "fee_versions": sorted(fee_versions),
        "period_start_utc_ms": period_start,
        "period_end_utc_ms": period_end,
        "known_gaps": gaps,
        "records_file": records_path.name,
        "records_sha256": records_hash,
        "summary": summary,
    }
    report_path.write_text(
        json.dumps(to_jsonable(report), indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    return dict(to_jsonable(summary)) | {
        "report": str(report_path),
        "records_sha256": records_hash,
    }
