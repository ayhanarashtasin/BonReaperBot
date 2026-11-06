"""Doctor, health and data-coverage reports. All are read-only."""

from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import polymarket

from ..connectors.geoblock import check_geoblock
from ..execution.real import live_gate_reasons
from ..market.book import BookManager
from ..market.validation import compare_snapshots
from ..settings import Settings
from ..storage.raw_events import load_manifest, read_events, verify_manifest
from ..types import MarketSpec, Mode, from_jsonable

_SECRET_ENV_HINTS = ("PRIVATE_KEY", "MNEMONIC", "SEED_PHRASE", "API_SECRET", "PASSPHRASE")


def doctor_report(
    settings: Settings, *, offline: bool, build_execution: Callable[..., Any]
) -> dict[str, Any]:
    checks: dict[str, Any] = {
        "python": sys.version.split()[0],
        "python_ok": sys.version_info >= (3, 11),
        "sdk": "polymarket-client",
        "sdk_version": polymarket.__version__,
        "mode": settings.mode.value,
    }
    if settings.mode is Mode.READ_ONLY:
        try:
            build_execution(settings)
            checks["read_only_no_adapter"] = False
        except Exception as exc:
            checks["read_only_no_adapter"] = type(exc).__name__ == "NoExecutionInReadOnlyError"
    geo = None
    if offline:
        checks["geoblock"] = "skipped_offline"
    else:
        geo = check_geoblock()
        checks["geoblock"] = {
            "status": geo.status.value,
            "country": geo.country,
            "detail": geo.detail,
        }
    checks["live_gate"] = live_gate_reasons(settings, geo)
    checks["live_disabled"] = bool(checks["live_gate"])
    leaked = sorted(k for k in os.environ if any(h in k.upper() for h in _SECRET_ENV_HINTS))
    checks["secret_like_env_var_names"] = leaked
    data_dir = Path(settings.storage.raw_dir)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        checks["raw_dir_writable"] = os.access(data_dir, os.W_OK)
    except OSError:
        checks["raw_dir_writable"] = False
    checks["ok"] = bool(
        checks["python_ok"]
        and checks["live_disabled"]
        and checks["raw_dir_writable"]
        and checks.get("read_only_no_adapter", True)
    )
    return checks


def health_report(settings: Settings, raw_dir: Path) -> dict[str, Any]:
    health_path = raw_dir / "health.json"
    report: dict[str, Any] = {
        "mode": settings.mode.value,
        "open_orders": 0,
        "unknown_orders": 0,
        "kill_switch": "not_applicable_read_only" if settings.mode is Mode.READ_ONLY else "unset",
    }
    if not health_path.exists():
        return report | {"recorder": "no_health_file", "status": "no_recorder_data"}
    doc = json.loads(health_path.read_text("utf-8"))
    return report | {"recorder": doc, "status": "recorded_state"}


def coverage_report(raw_dir: Path) -> dict[str, Any]:
    """Data coverage, gaps, and rebuilt-book agreement with independent REST samples (FR-04)."""
    records = load_manifest(raw_dir)
    problems = verify_manifest(raw_dir, records)
    specs: dict[str, MarketSpec] = {}
    per_market: dict[str, Counter[str]] = defaultdict(Counter)
    gaps: list[dict[str, Any]] = []
    first: dict[str, int] = {}
    last: dict[str, int] = {}
    resolutions: dict[str, dict[str, Any]] = {}
    twap_points = 0
    books = BookManager()
    samples = 0
    exact = 0
    top_equal = 0
    invalid_samples = 0
    invalid_reasons: Counter[str] = Counter()
    events_total = 0
    stream_checks = 0
    stream_exact = 0
    stream_top_equal = 0
    stream_level_diffs = 0
    clock_offsets: list[int] = []
    for env in read_events(raw_dir, verify=False):
        events_total += 1
        payload = env.payload()
        if env.topic == "market_spec":
            spec = from_jsonable(MarketSpec, payload["spec"])
            specs[spec.market_id] = spec
            books.register(spec.market_id, spec.yes_asset_id, spec.tick_size)
            books.register(spec.market_id, spec.no_asset_id, spec.tick_size)
        elif env.topic == "gap":
            gaps.append(payload)
        elif env.topic == "market_resolution" and env.market_id:
            resolutions[env.market_id] = payload
        elif env.topic == "clock_probe":
            clock_offsets.append(int(payload["offset_ms"]))
        elif env.source == "poly_twap":
            twap_points += 1
        elif env.source == "poly_market" or env.topic == "resnapshot":
            if env.market_id is None:
                continue
            mid = env.market_id
            per_market[mid][env.topic] += 1
            first.setdefault(mid, env.received_utc_ms)
            last[mid] = env.received_utc_ms
            existing = books.books.get(env.asset_id or "")
            if env.topic == "book" and existing is not None and existing.snapshot_count > 0:
                # Each stream snapshot is an independent check on the book rebuilt since the last.
                fresh = BookManager()
                fresh.register(existing.market_id, existing.asset_id, existing.tick_size)
                fresh.apply(payload, env.received_utc_ms)
                check = compare_snapshots(existing.state(), fresh.books[existing.asset_id].state())
                stream_checks += 1
                stream_exact += int(check.matches)
                stream_top_equal += int(check.top_of_book_equal)
                stream_level_diffs += check.bid_level_diffs + check.ask_level_diffs
            result = books.apply(payload, env.received_utc_ms)
            for _asset, reason in result.invalidated:
                invalid_reasons[reason] += 1
        elif env.topic == "rest_book_sample" and env.asset_id in books.books:
            book = books.books[env.asset_id]
            rest = BookManager()
            rest.register(book.market_id, book.asset_id, book.tick_size)
            rest.apply(payload, env.received_utc_ms)
            comparison = compare_snapshots(book.state(), rest.books[env.asset_id].state())
            if book.state().validity.value != "ready":
                invalid_samples += 1
            samples += 1
            exact += int(comparison.matches)
            top_equal += int(comparison.top_of_book_equal)
    markets = []
    for mid, spec in sorted(specs.items(), key=lambda kv: kv[1].start_utc_ms):
        counts = per_market.get(mid, Counter())
        start_cover = first.get(mid)
        end_cover = last.get(mid)
        full = (
            start_cover is not None
            and end_cover is not None
            and start_cover <= spec.start_utc_ms
            and end_cover >= spec.end_utc_ms - 1000
        )
        resolution = resolutions.get(mid)
        markets.append(
            {
                "market_id": mid,
                "slug": spec.slug,
                "events": dict(counts),
                "first_event_utc_ms": start_cover,
                "last_event_utc_ms": end_cover,
                "full_lifespan_captured": full,
                "market_gaps": sum(1 for g in gaps if g.get("market_id") == mid),
                "resolved": resolution is not None,
                "price_to_beat": resolution.get("price_to_beat") if resolution else None,
                "final_price": resolution.get("final_price") if resolution else None,
                "yes_price": resolution.get("yes_price") if resolution else None,
            }
        )
    return {
        "events_total": events_total,
        "parts": len(records),
        "checksum_problems": problems,
        "markets": markets,
        "full_lifespans": sum(1 for m in markets if m["full_lifespan_captured"]),
        "twap_points": twap_points,
        "gaps_total": len(gaps),
        "gaps": gaps[:20],
        "book_invalidations": dict(invalid_reasons),
        "rest_validation": {
            "samples": samples,
            "exact_depth_match": exact,
            "top_of_book_match": top_equal,
            "samples_while_invalid": invalid_samples,
            "tolerance": (
                "REST and stream samples are not simultaneous, so fast markets can differ by "
                "a few levels without any missed event"
            ),
            "exact_rate": str(Decimal(exact) / Decimal(samples)) if samples else None,
        },
        "stream_snapshot_validation": {
            "checks": stream_checks,
            "exact_depth_match": stream_exact,
            "top_of_book_match": stream_top_equal,
            "avg_level_diffs": (
                str(Decimal(stream_level_diffs) / Decimal(stream_checks)) if stream_checks else None
            ),
            "note": (
                "A book message follows a trade, so some difference is expected: the trade changed "
                "those levels and no price_change reported it"
            ),
        },
        "clock_offset_ms_latest": clock_offsets[-1] if clock_offsets else None,
        "clock_probes": len(clock_offsets),
    }
