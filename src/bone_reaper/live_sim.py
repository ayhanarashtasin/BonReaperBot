"""Live paper and shadow sessions: real public feeds, entirely simulated orders.

The recorder's event tap feeds a SimCore, so paper uses the same signal, risk and OMS code as replay
(FR-14). No secure client exists in this code path, and neither mode can reach an order endpoint.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .app import Recorder
from .connectors.polymarket_public import ReadOnlyTransport
from .logging import configure
from .market.rules import get_instrument
from .research.metrics import compute_metrics
from .research.replay import SimCore
from .research.reports import write_json
from .settings import Settings
from .storage.atomic import atomic_write_text
from .types import EventEnvelope, Mode, to_jsonable

PUMP_INTERVAL_S = 0.2
SHUTDOWN_SETTLE_MS = 2000
SNAPSHOT_INTERVAL_S = 5.0
SNAPSHOT_EQUITY_POINTS = 720  # One hour at the five-second sampling interval.
LIVE_SNAPSHOT_FILE = "live.json"

LOG = logging.getLogger("bone_reaper.live_sim")


def attach_core(recorder: Recorder, core: SimCore) -> Callable[[EventEnvelope], None]:
    def tap(envelope: EventEnvelope) -> None:
        core.schedule_envelope(envelope, live=True)
        core.run_until(envelope.received_utc_ms)

    recorder.tap = tap
    return tap


def live_snapshot(
    core: SimCore, now_utc_ms: int, *, started_utc_ms: int, running: bool
) -> dict[str, Any]:
    """What a viewer needs to follow a session. Reads the core and changes nothing in it."""
    active = core._active_spec(now_utc_ms)
    market: dict[str, Any] | None = None
    if active is not None:
        sides = {}
        for outcome, asset in (("YES", active.yes_asset_id), ("NO", active.no_asset_id)):
            book = core.visible.books.get(asset)
            state = book.state() if book is not None else None
            sides[outcome] = {
                "bid": state.best_bid if state else None,
                "ask": state.best_ask if state else None,
                "validity": state.validity.value if state else None,
            }
        market = {
            "market_id": active.market_id,
            "slug": active.slug,
            "start_utc_ms": active.start_utc_ms,
            "end_utc_ms": active.end_utc_ms,
            "sides": sides,
        }
    return {
        "simulated": True,
        "running": running,
        "mode": core.settings.mode.value,
        "ts_utc_ms": now_utc_ms,
        "started_utc_ms": started_utc_ms,
        "active_market": market,
        "open_orders": [
            {
                "intent_id": order.intent.intent_id,
                "outcome": order.intent.outcome,
                "side": order.intent.side,
                "price": order.intent.price,
                "size": order.intent.size,
                "matched": order.matched_gross,
                "state": order.state,
                "purpose": order.intent.purpose,
            }
            for order in core.oms.open_orders
        ],
        "equity_curve": [
            [point["ts_utc_ms"], point["equity"]]
            for point in core.timeline[-SNAPSHOT_EQUITY_POINTS:]
        ],
        "metrics": compute_metrics(core),
    }


def _write_snapshot(core: SimCore, output_dir: Path, started: int, *, running: bool) -> None:
    """Observing a session must never stop it, so a failure here is logged and skipped."""
    try:
        doc = live_snapshot(core, core.scheduler.now, started_utc_ms=started, running=running)
        atomic_write_text(
            output_dir / LIVE_SNAPSHOT_FILE,
            json.dumps(to_jsonable(doc), indent=1, sort_keys=True),
        )
    except Exception:
        LOG.exception("live snapshot skipped")


async def _pump(core: SimCore, recorder: Recorder, output_dir: Path, started: int) -> None:
    """Advance simulated time when the feed is quiet, so timers and staleness checks still run."""
    next_snapshot = started
    while True:
        await asyncio.sleep(PUMP_INTERVAL_S)
        now = recorder.clock.utc_ms()
        core.run_until(now)
        # Decisions run on quiet feeds too, or a silent stream would never trip the kill switch.
        core._decide(core.scheduler.now)
        if now >= next_snapshot:
            next_snapshot = now + int(SNAPSHOT_INTERVAL_S * 1000)
            _write_snapshot(core, output_dir, started, running=True)


def shutdown_orders(core: SimCore, now_utc_ms: int) -> None:
    """Stop new risk and try to cancel. Whatever remains is reported, not hidden.

    An order still in flight cannot be cancelled until its ACK arrives, so this makes a second
    pass after in-flight messages have landed.
    """
    for offset in (0, SHUTDOWN_SETTLE_MS):
        core.run_until(now_utc_ms + offset)
        for market_id in list(core.specs):
            core.orchestrator.cancel_all(market_id, "shutdown")
    core.run_until(now_utc_ms + 2 * SHUTDOWN_SETTLE_MS)


async def run_live_sim(
    settings: Settings, output_dir: Path, seconds: float | None
) -> dict[str, Any]:
    if settings.mode not in (Mode.PAPER, Mode.SHADOW):
        raise ValueError("live simulation runs only in paper or shadow mode")
    configure()
    output_dir.mkdir(parents=True, exist_ok=True)
    core = SimCore(settings, shadow_path=output_dir / "shadow_records.jsonl")
    transport = ReadOnlyTransport()
    recorder = Recorder(
        settings,
        get_instrument(settings.markets.symbols[0], settings.markets.durations_minutes[0]),
        output_dir / "raw",
        transport=transport,
    )
    attach_core(recorder, core)
    started = recorder.clock.utc_ms()
    _write_snapshot(core, output_dir, started, running=True)
    pump = asyncio.create_task(_pump(core, recorder, output_dir, started))
    try:
        # Ctrl+C is the documented way to stop, so it still ends with a full report.
        with contextlib.suppress(asyncio.CancelledError):
            await recorder.run(seconds)
    finally:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump
        await transport.close()
    shutdown_orders(core, recorder.clock.utc_ms())
    metrics = compute_metrics(core)
    unresolved_orders = [o.intent.intent_id for o in core.oms.open_orders]
    metrics["shutdown"] = {
        "clean": not unresolved_orders and not core.oms.risk_frozen,
        "orders_still_open": unresolved_orders,
        "freeze_reasons": core.oms.freeze_reasons(),
        "unresolved_positions": sorted(core.unresolved),
    }
    write_json(output_dir / "metrics.json", metrics)
    _write_snapshot(core, output_dir, started, running=False)
    return {
        "output": str(output_dir),
        "mode": settings.mode.value,
        "shutdown": metrics["shutdown"],
        "fills": metrics["fills"],
        "pnl": metrics["pnl"],
        "disclaimer": metrics["disclaimer"],
    }
