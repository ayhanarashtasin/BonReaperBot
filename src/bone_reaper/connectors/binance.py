"""Binance historical file parsing and optional live aggTrade connector.

Binance data is a forecasting input only. It carries its own instrument and contract type, cannot
be labelled Chainlink (enforced by ``BinanceReference``), and is never settlement evidence.
"""

from __future__ import annotations

import asyncio
import csv
import itertools
import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import websockets

from ..time import Clock
from ..types import ReferenceSource, sha256_hex

SOURCE_BINANCE = "binance"
LIVE_SPOT_URL = "wss://stream.binance.com:9443/ws/{symbol}@aggTrade"
SERVER_TIME_URL = "https://api.binance.com/api/v3/time"


@dataclass(frozen=True, slots=True)
class AggTrade:
    agg_trade_id: int
    price: Decimal
    quantity: Decimal
    ts_ms: int
    buyer_is_maker: bool

    @property
    def aggressor(self) -> str:
        return "SELL" if self.buyer_is_maker else "BUY"


@dataclass(frozen=True, slots=True)
class Candle:
    open_ms: int
    close_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True, slots=True)
class IngestionManifest:
    path: str
    instrument: str
    source: ReferenceSource
    quote_currency: str
    timezone: str
    units: str
    rows: int
    duplicates: int
    out_of_order: int
    gaps: int
    largest_gap_ms: int
    first_ts_ms: int | None
    last_ts_ms: int | None
    sha256: str


def _to_ms(raw: str) -> int:
    """Binance public files use ms, and microseconds from 2025 for spot. Normalize to ms."""
    value = int(raw)
    return value // 1000 if value > 10**14 else value


def _has_header(first: list[str]) -> bool:
    return not first[0].strip().lstrip("-").isdigit()


def _rows(path: Path) -> Iterator[list[str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        for index, row in enumerate(reader):
            if not row or (index == 0 and _has_header(row)):
                continue
            yield row


def read_agg_trades(path: Path) -> Iterator[AggTrade]:
    """Columns: id, price, qty, first_id, last_id, ts, buyer_is_maker[, best_match]."""
    for row in _rows(path):
        yield AggTrade(
            agg_trade_id=int(row[0]),
            price=Decimal(row[1]),
            quantity=Decimal(row[2]),
            ts_ms=_to_ms(row[5]),
            buyer_is_maker=row[6].strip().lower() == "true",
        )


def read_candles(path: Path) -> Iterator[Candle]:
    """Columns: open_time, open, high, low, close, volume, close_time, ..."""
    for row in _rows(path):
        yield Candle(
            open_ms=_to_ms(row[0]),
            close_ms=_to_ms(row[6]),
            open=Decimal(row[1]),
            high=Decimal(row[2]),
            low=Decimal(row[3]),
            close=Decimal(row[4]),
            volume=Decimal(row[5]),
        )


def _quality(timestamps: list[int], ids: list[int], max_gap_ms: int) -> tuple[int, int, int, int]:
    duplicates = len(ids) - len(set(ids))
    out_of_order = sum(1 for a, b in itertools.pairwise(timestamps) if b < a)
    gap_sizes = [b - a for a, b in itertools.pairwise(timestamps) if b - a > max_gap_ms]
    return duplicates, out_of_order, len(gap_sizes), max(gap_sizes, default=0)


def ingest_agg_trades(
    path: Path,
    instrument: str,
    source: ReferenceSource,
    quote_currency: str = "USDT",
    max_gap_ms: int = 60_000,
) -> tuple[list[AggTrade], IngestionManifest]:
    if source is ReferenceSource.CHAINLINK_TWAP:
        raise ValueError("Binance files can never be ingested as Chainlink")
    trades = list(read_agg_trades(path))
    stamps = [t.ts_ms for t in trades]
    dup, ooo, gaps, largest = _quality(stamps, [t.agg_trade_id for t in trades], max_gap_ms)
    manifest = IngestionManifest(
        path=str(path),
        instrument=instrument,
        source=source,
        quote_currency=quote_currency,
        timezone="UTC",
        units="price=quote/base, qty=base",
        rows=len(trades),
        duplicates=dup,
        out_of_order=ooo,
        gaps=gaps,
        largest_gap_ms=largest,
        first_ts_ms=stamps[0] if stamps else None,
        last_ts_ms=stamps[-1] if stamps else None,
        sha256=sha256_hex(path.read_bytes()),
    )
    return trades, manifest


def ingest_candles(
    path: Path,
    instrument: str,
    source: ReferenceSource,
    quote_currency: str = "USDT",
    interval_ms: int = 60_000,
) -> tuple[list[Candle], IngestionManifest]:
    if source is ReferenceSource.CHAINLINK_TWAP:
        raise ValueError("Binance files can never be ingested as Chainlink")
    candles = list(read_candles(path))
    stamps = [c.open_ms for c in candles]
    dup, ooo, gaps, largest = _quality(stamps, stamps, interval_ms)
    manifest = IngestionManifest(
        path=str(path),
        instrument=instrument,
        source=source,
        quote_currency=quote_currency,
        timezone="UTC",
        units="price=quote/base, volume=base",
        rows=len(candles),
        duplicates=dup,
        out_of_order=ooo,
        gaps=gaps,
        largest_gap_ms=largest,
        first_ts_ms=stamps[0] if stamps else None,
        last_ts_ms=stamps[-1] if stamps else None,
        sha256=sha256_hex(path.read_bytes()),
    )
    return candles, manifest


def normalize_live_agg_trade(frame: dict[str, Any], source: ReferenceSource) -> dict[str, Any]:
    if source is ReferenceSource.CHAINLINK_TWAP:
        raise ValueError("Binance frames can never be labelled Chainlink")
    return {
        "event_type": "agg_trade",
        "reference_source": source.value,
        "instrument": str(frame["s"]),
        "agg_trade_id": int(frame["a"]),
        "price": str(frame["p"]),
        "quantity": str(frame["q"]),
        "timestamp": int(frame["T"]),
        "aggressor": "SELL" if frame["m"] else "BUY",
    }


async def measure_clock_offset(clock: Clock, attempts: int = 3) -> dict[str, Any]:
    """Local clock minus Binance server time, from the probe with the smallest round trip.

    Used only as an external time reference. It reveals clock error; it never prices anything.
    """
    # A worker thread keeps the timing away from a busy event loop; loop delay would inflate it.
    return await asyncio.to_thread(_probe_clock_offset, clock, attempts)


def _probe_clock_offset(clock: Clock, attempts: int) -> dict[str, Any]:
    best: tuple[int, int] | None = None
    with httpx.Client(timeout=10.0, trust_env=False) as client:
        for _ in range(attempts):
            before = clock.utc_ms()
            response = client.get(SERVER_TIME_URL)
            after = clock.utc_ms()
            response.raise_for_status()
            server_ms = int(response.json()["serverTime"])
            rtt = after - before
            offset = (before + after) // 2 - server_ms
            if best is None or rtt < best[1]:
                best = (offset, rtt)
    if best is None:
        raise RuntimeError("clock probe made no attempts")
    return {"offset_ms": best[0], "rtt_ms": best[1], "reference": "binance_server_time"}


async def stream_spot_agg_trades(symbol: str = "btcusdt") -> AsyncIterator[dict[str, Any]]:
    """Optional public live feed. BTCUSDT spot is its own instrument, not BTC/USD Chainlink."""
    async with websockets.connect(LIVE_SPOT_URL.format(symbol=symbol.lower())) as socket:
        async for message in socket:
            yield normalize_live_agg_trade(json.loads(message), ReferenceSource.BINANCE_SPOT)
