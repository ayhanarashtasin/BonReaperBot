"""Append official outcomes for recorded markets that lack one. Read-only against the venue.

Append-only: existing parts are untouched; new parts are added and flagged ``backfilled``. They are
stamped at market end + 60s, so replay settles a market after close and never sooner.

Run: uv run python scripts/backfill_resolutions.py --input data/raw
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path

from bone_reaper.connectors.polymarket_market_stream import SOURCE_REST, make_envelope
from bone_reaper.connectors.polymarket_public import ReadOnlyTransport, resolution_payload
from bone_reaper.execution.settlement import (
    ResolutionUnknownError,
    winning_outcome_from_resolution,
)
from bone_reaper.storage.raw_events import RawEventWriter, read_events
from bone_reaper.time import FakeClock
from bone_reaper.types import MarketSpec, from_jsonable

SETTLE_DELAY_MS = 60_000


async def main_async(root: Path) -> dict[str, int]:
    specs: dict[str, MarketSpec] = {}
    resolved: set[str] = set()
    for env in read_events(root, verify=False, sources=["control", "poly_rest"]):
        if env.topic == "market_spec":
            spec = from_jsonable(MarketSpec, env.payload()["spec"])
            specs.setdefault(spec.market_id, spec)
        elif env.topic == "market_resolution" and env.market_id:
            resolved.add(env.market_id)
    run_id = f"backfill-{uuid.uuid4().hex[:8]}"
    writer = RawEventWriter(root, run_id=run_id)
    transport = ReadOnlyTransport()
    counts = {"markets": len(specs), "already_resolved": len(resolved), "added": 0, "not_final": 0}
    try:
        for seq, spec in enumerate(specs.values(), start=1):
            if spec.market_id in resolved:
                continue
            payload = resolution_payload(await transport.event_by_slug(spec.slug))
            try:
                winning_outcome_from_resolution(payload, spec)
            except ResolutionUnknownError:
                counts["not_final"] += 1
                continue
            payload["backfilled"] = True
            recv = spec.end_utc_ms + SETTLE_DELAY_MS
            writer.append(
                make_envelope(
                    payload,
                    source=SOURCE_REST,
                    topic="market_resolution",
                    clock=FakeClock(recv, recv * 1_000_000),
                    segment_id=f"{run_id}:rest",
                    seq=seq,
                    market_id=spec.market_id,
                )
            )
            counts["added"] += 1
            await asyncio.sleep(0.05)
    finally:
        await transport.close()
    writer.flush()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/raw")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(main_async(Path(args.input)))))


if __name__ == "__main__":
    main()
