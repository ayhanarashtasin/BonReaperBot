"""Typed read-only SDK smoke test (BR-002). Reads public data only: no credentials, no orders.

Run: uv run python scripts/smoke_read_only.py
"""

from __future__ import annotations

import asyncio
import json
import time

import polymarket
from polymarket import AsyncPublicClient, Event, OrderBook
from polymarket.streams import MarketSpec as SdkMarketSpec

from bone_reaper.connectors.geoblock import check_geoblock
from bone_reaper.connectors.polymarket_public import ReadOnlyTransport
from bone_reaper.market.discovery import discover
from bone_reaper.market.rules import get_instrument
from bone_reaper.time import SystemClock


async def main() -> None:
    client = AsyncPublicClient()
    transport = ReadOnlyTransport(client)
    instrument = get_instrument("BTC", 5)
    try:
        result = await discover(transport, instrument, SystemClock().utc_ms(), limit=3)
        spec = result.accepted[0]
        event: Event = await transport.event_by_slug(spec.slug)
        book: OrderBook = await client.get_order_book(asset_id=spec.yes_asset_id)
        counts: dict[str, int] = {}
        handle = await client.subscribe(
            SdkMarketSpec(asset_ids=[spec.yes_asset_id, spec.no_asset_id])
        )
        deadline = time.monotonic() + 8
        async with handle as stream:
            async for message in stream:
                counts[message.type] = counts.get(message.type, 0) + 1
                if time.monotonic() > deadline:
                    break
        geo = check_geoblock()
        print(
            json.dumps(
                {
                    "sdk_version": polymarket.__version__,
                    "market": spec.slug,
                    "protocol_version": spec.version.value,
                    "fee": spec.fee.version,
                    "event_window_start": str(event.schedule.start_time),
                    "book_levels": {"bids": len(book.bids), "asks": len(book.asks)},
                    "stream_event_counts": counts,
                    "geoblock": geo.status.value,
                },
                indent=1,
            )
        )
    finally:
        await transport.close()


if __name__ == "__main__":
    asyncio.run(main())
