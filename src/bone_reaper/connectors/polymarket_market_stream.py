"""Public market stream normalization and connector. No trading credentials are involved.

ADR-0004: the SDK does not expose wire frames, so ``payload_json`` holds a lossless normalized form
of the typed event (Decimals as exact strings) instead of the raw bytes.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from typing import Any

from polymarket import AsyncPublicClient
from polymarket.models.clob.market_events import MarketEvent, parse_market_event
from polymarket.streams import MarketSpec as SdkMarketSpec

from ..time import Clock, stamp
from ..types import SCHEMA_VERSION, EventEnvelope, sha256_hex

SOURCE_MARKET = "poly_market"
SOURCE_REST = "poly_rest"
SOURCE_CONTROL = "control"


def _ms(value: datetime | None) -> int | None:
    return int(value.timestamp() * 1000) if value is not None else None


def _s(value: Any) -> str | None:
    if value is None:
        return None
    return format(value, "f") if isinstance(value, Decimal) else str(value)


def normalize_event(event: MarketEvent) -> dict[str, Any]:
    """Lossless JSON form consumed by the book materializer and the replay engine."""
    out: dict[str, Any] = {"event_type": event.type}
    if event.type == "book":
        book = event.payload
        out |= {
            "market": book.condition_id,
            "asset_id": book.asset_id,
            "bids": [{"price": _s(lv.price), "size": _s(lv.size)} for lv in book.bids],
            "asks": [{"price": _s(lv.price), "size": _s(lv.size)} for lv in book.asks],
            "hash": book.hash,
            "timestamp": _ms(book.timestamp),
            "tick_size": _s(book.tick_size),
            "min_order_size": _s(book.min_order_size),
            "last_trade_price": _s(book.last_trade_price),
        }
    elif event.type == "price_change":
        pc = event.payload
        out |= {
            "market": pc.condition_id,
            "timestamp": _ms(pc.timestamp),
            "price_changes": [
                {
                    "asset_id": c.asset_id,
                    "price": _s(c.price),
                    "size": _s(c.size),
                    "side": c.side,
                    "hash": c.hash,
                    "best_bid": _s(c.best_bid),
                    "best_ask": _s(c.best_ask),
                }
                for c in pc.price_changes
            ],
        }
    elif event.type == "last_trade_price":
        lt = event.payload
        out |= {
            "market": lt.condition_id,
            "asset_id": lt.asset_id,
            "price": _s(lt.price),
            "size": _s(lt.size),
            "side": lt.side,
            "fee_rate_bps": _s(lt.fee_rate_bps),
            "transaction_hash": lt.transaction_hash,
            "timestamp": _ms(lt.timestamp),
        }
    elif event.type == "tick_size_change":
        ts = event.payload
        out |= {
            "market": ts.condition_id,
            "asset_id": ts.asset_id,
            "old_tick_size": _s(ts.old_tick_size),
            "new_tick_size": _s(ts.new_tick_size),
            "timestamp": _ms(ts.timestamp),
        }
    elif event.type == "best_bid_ask":
        bb = event.payload
        out |= {
            "market": bb.condition_id,
            "asset_id": bb.asset_id,
            "best_bid": _s(bb.best_bid),
            "best_ask": _s(bb.best_ask),
            "spread": _s(bb.spread),
            "timestamp": _ms(bb.timestamp),
        }
    elif event.type == "market_resolved":
        mr = event.payload
        out |= {
            "market": mr.condition_id,
            "asset_ids": list(mr.asset_ids or ()),
            "winning_asset_id": mr.winning_asset_id,
            "winning_outcome": mr.winning_outcome,
            "timestamp": _ms(mr.timestamp),
        }
    else:
        nm = event.payload
        out |= {"market": nm.market, "slug": nm.slug, "timestamp": _ms(nm.timestamp)}
    return out


def parse_wire_event(raw: dict[str, Any]) -> dict[str, Any]:
    """Parse a wire-format frame with the official SDK model, then normalize it."""
    return normalize_event(parse_market_event(raw))


def make_envelope(
    payload: dict[str, Any],
    *,
    source: str,
    topic: str,
    clock: Clock,
    segment_id: str,
    seq: int,
    market_id: str | None = None,
    asset_id: str | None = None,
    source_ts_ms: int | None = None,
) -> EventEnvelope:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    arrival = stamp(clock, source_ts_ms)
    return EventEnvelope(
        event_id=f"{source}:{segment_id}:{seq}:{sha256_hex(body)[:16]}",
        schema_version=SCHEMA_VERSION,
        source=source,
        topic=topic,
        market_id=market_id,
        asset_id=asset_id,
        source_ts_ms=arrival.source_ts_ms,
        received_at_ns=arrival.received_at_ns,
        received_utc_ms=arrival.received_utc_ms,
        payload_json=body,
        segment_id=segment_id,
        seq=seq,
    )


async def stream_market(
    client: AsyncPublicClient, asset_ids: tuple[str, ...]
) -> AsyncIterator[tuple[dict[str, Any], int]]:
    """Yield ``(normalized_payload, dropped_total)`` for one subscription."""
    handle = await client.subscribe(
        SdkMarketSpec(asset_ids=list(asset_ids), custom_feature_enabled=True)
    )
    async with handle as subscription:
        async for event in subscription:
            yield normalize_event(event), int(getattr(handle, "dropped", 0))
