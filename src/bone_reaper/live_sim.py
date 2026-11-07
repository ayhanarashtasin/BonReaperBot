"""Live paper and shadow sessions: real public feeds, entirely simulated orders.

The recorder's event tap feeds a SimCore, so paper uses the same signal, risk and OMS code as replay
(FR-14). No secure client exists in this code path, and neither mode can reach an order endpoint.
"""

from __future__ import annotations

import asyncio
import contextlib
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
from .types import EventEnvelope, Mode

PUMP_INTERVAL_S = 0.2
SHUTDOWN_SETTLE_MS = 2000


def attach_core(recorder: Recorder, core: SimCore) -> Callable[[EventEnvelope], None]:
    def tap(envelope: EventEnvelope) -> None:
        core.schedule_envelope(envelope, live=True)
        core.run_until(envelope.received_utc_ms)

    recorder.tap = tap
    return tap


async def _pump(core: SimCore, recorder: Recorder) -> None:
    """Advance simulated time when the feed is quiet, so timers and staleness checks still run."""
    while True:
        await asyncio.sleep(PUMP_INTERVAL_S)
        core.run_until(recorder.clock.utc_ms())
        # Decisions run on quiet feeds too, or a silent stream would never trip the kill switch.
        core._decide(core.scheduler.now)


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
    pump = asyncio.create_task(_pump(core, recorder))
    try:
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
    return {
        "output": str(output_dir),
        "mode": settings.mode.value,
        "shutdown": metrics["shutdown"],
        "fills": metrics["fills"],
        "pnl": metrics["pnl"],
        "disclaimer": metrics["disclaimer"],
    }
