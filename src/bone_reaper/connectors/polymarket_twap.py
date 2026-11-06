"""Chainlink TWAP reference connectors.

Two official routes exist (reports/sdk_audit.md):

* ``prices.crypto.twap`` via ``AsyncSecureClient`` (PolyBolt). The current route, and it needs
  credentials. The facade below takes only a ``subscribe`` callable, so strategy and OMS code
  holding it have no path to order placement.
* the public RTDS ``prices.crypto.chainlink.twap`` stream. No credentials, but the venue announced
  a shutdown on 2026-11-30, so it is labelled deprecated in every record.

If neither is available the state is explicitly UNAVAILABLE. The scanner keeps working, and the
fair-value model fails closed. Binance is never substituted.
"""

from __future__ import annotations

import enum
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from polymarket import AsyncPublicClient
from polymarket.streams import CryptoPricesChainlinkTwapSpec, CryptoTwapPriceSpec

from ..types import ReferenceSource, ReferenceState

SOURCE_TWAP = "poly_twap"
FEED_POLYBOLT = "polybolt:prices.crypto.twap"
FEED_RTDS = "rtds:prices.crypto.chainlink.twap(deprecated-sunset-2026-11-30)"


class TwapAvailability(enum.StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


def _ms(value: datetime | None) -> int | None:
    return int(value.timestamp() * 1000) if value is not None else None


def normalize_rtds_event(event: Any) -> dict[str, Any]:
    """Normalize a public RTDS TWAP update."""
    p = event.payload
    return {
        "event_type": "twap_update",
        "feed": FEED_RTDS,
        "symbol": p.symbol,
        "value": format(p.value, "f"),
        "window_seconds": p.window_seconds,
        "timestamp": int(p.timestamp),
    }


def normalize_polybolt_event(event: Any) -> dict[str, Any]:
    """Normalize a PolyBolt TWAP update or snapshot (``type`` is ``update`` or ``subscribe``)."""
    p = event.payload
    base: dict[str, Any] = {
        "feed": FEED_POLYBOLT,
        "symbol": p.symbol,
        "source": str(p.source),
        "window_seconds": p.window_seconds,
        "seq": getattr(event, "seq", None),
        "dropped": getattr(event, "dropped", None),
    }
    if event.type == "subscribe":
        return base | {
            "event_type": "twap_snapshot",
            "timestamp": _ms(event.timestamp),
            "history": [
                {"timestamp": _ms(pt.timestamp), "value": format(pt.value, "f")} for pt in p.data
            ],
        }
    return base | {
        "event_type": "twap_update",
        "value": format(p.value, "f"),
        "timestamp": _ms(p.timestamp),
    }


class TwapSource(Protocol):
    availability: TwapAvailability
    reason: str

    def stream(self) -> AsyncIterator[dict[str, Any]]: ...


class UnavailableTwapSource:
    """An explicit no-data state. Yields nothing and never fabricates a price."""

    availability = TwapAvailability.UNAVAILABLE

    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        return
        yield {}  # pragma: no cover - makes this an async generator


class RtdsTwapSource:
    availability = TwapAvailability.AVAILABLE
    reason = "public_rtds_deprecated_sunset_2026_11_30"

    def __init__(self, client: AsyncPublicClient, rtds_symbol: str) -> None:
        self._client = client
        self._symbol = rtds_symbol

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        spec = CryptoPricesChainlinkTwapSpec(window_seconds=60, symbols=[self._symbol])
        async with await self._client.subscribe(spec) as subscription:
            async for event in subscription:
                yield normalize_rtds_event(event)


Subscribe = Callable[[CryptoTwapPriceSpec], Awaitable[Any]]


class SecureTwapFacade:
    """Exposes only a TWAP stream. Holds a subscribe function, never the secure client itself."""

    availability = TwapAvailability.AVAILABLE
    reason = "authenticated_polybolt"
    __slots__ = ("_subscribe", "_symbol")

    def __init__(self, subscribe: Subscribe, twap_symbol: str) -> None:
        self._subscribe = subscribe
        self._symbol = twap_symbol

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        handle = await self._subscribe(CryptoTwapPriceSpec(symbols=[self._symbol]))
        async with handle as subscription:
            async for event in subscription:
                yield normalize_polybolt_event(event)


class ReferenceTracker:
    """Builds ReferenceState and tracks the observed TWAP at each window start."""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.history: list[tuple[int, Decimal, int]] = []
        self.feed = "none"

    def on_payload(self, payload: dict[str, Any], received_utc_ms: int) -> None:
        self.feed = str(payload.get("feed", self.feed))
        if payload.get("event_type") == "twap_snapshot":
            for point in payload.get("history", ()):
                self._append(int(point["timestamp"]), Decimal(point["value"]), received_utc_ms)
        elif payload.get("event_type") == "twap_update":
            self._append(int(payload["timestamp"]), Decimal(payload["value"]), received_utc_ms)

    def _append(self, ts: int, value: Decimal, received: int) -> None:
        if self.history and ts <= self.history[-1][0]:
            return
        self.history.append((ts, value, received))

    def observed_strike(self, window_start_ms: int, tolerance_ms: int = 2000) -> Decimal | None:
        """Latest TWAP at or before the window start, if close enough to trust.

        This is an estimate of the price to beat; the official figure only appears in event
        metadata after close. ``strike_provenance`` on the state says which one is in use.
        """
        best: tuple[int, Decimal, int] | None = None
        for point in reversed(self.history):
            if point[0] <= window_start_ms:
                best = point
                break
        if best is None or window_start_ms - best[0] > tolerance_ms:
            return None
        return best[1]

    def state(self, window_start_ms: int | None = None) -> ReferenceState:
        latest = self.history[-1] if self.history else None
        strike = self.observed_strike(window_start_ms) if window_start_ms is not None else None
        return ReferenceState(
            symbol=self.symbol,
            twap=latest[1] if latest else None,
            window_seconds=60,
            source=ReferenceSource.CHAINLINK_TWAP,
            feed=self.feed,
            source_ts_ms=latest[0] if latest else None,
            received_utc_ms=latest[2] if latest else None,
            start_strike=strike,
            strike_provenance="observed_twap_at_window_start" if strike is not None else None,
        )
