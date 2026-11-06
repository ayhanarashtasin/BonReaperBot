"""Read-only transport over the official public client. It has no way to reach order APIs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from polymarket import AsyncPublicClient, Event, OrderBook

from ..types import to_jsonable


def book_to_payload(book: OrderBook) -> dict[str, Any]:
    """Normalize a REST book into the same payload shape as a stream ``book`` event."""
    return {
        "event_type": "book",
        "market": book.condition_id,
        "asset_id": book.asset_id,
        "bids": [{"price": str(lv.price), "size": str(lv.size)} for lv in book.bids],
        "asks": [{"price": str(lv.price), "size": str(lv.size)} for lv in book.asks],
        "hash": book.hash,
        "timestamp": int(book.timestamp.timestamp() * 1000) if book.timestamp else None,
        "tick_size": str(book.tick_size),
        "min_order_size": str(book.min_order_size),
        "origin": "rest",
    }


class ReadOnlyTransport:
    """Public market data only. No credentials, no signing, no order methods."""

    is_real = False

    def __init__(self, client: AsyncPublicClient | None = None) -> None:
        self._client = client or AsyncPublicClient()
        self._series_ids: dict[str, int] = {}

    @property
    def client(self) -> AsyncPublicClient:
        return self._client

    async def close(self) -> None:
        await self._client.close()

    async def series_id(self, slug: str) -> int:
        cached = self._series_ids.get(slug)
        if cached is not None:
            return cached
        page = await self._client.list_series(slug=slug).first_page()
        for series in page.items:
            if series.slug == slug:
                self._series_ids[slug] = int(series.id)
                return int(series.id)
        raise LookupError(f"series {slug} not found")

    async def open_events(self, series_id: int, now_utc_ms: int, limit: int) -> list[Event]:
        end_min = datetime.fromtimestamp(now_utc_ms / 1000, tz=UTC).isoformat()
        page = await self._client.list_events(
            series_ids=series_id,
            closed=False,
            order="endDate",
            ascending=True,
            end_date_min=end_min,
            page_size=limit,
        ).first_page()
        return list(page.items)

    async def event_by_slug(self, slug: str) -> Event:
        return await self._client.get_event(slug=slug)

    async def book_payload(self, asset_id: str) -> dict[str, Any]:
        return book_to_payload(await self._client.get_order_book(asset_id=asset_id))


def resolution_payload(event: Event) -> dict[str, Any]:
    """Official post-close facts. Only venue fields are used, never Binance candles."""
    market = event.markets[0] if event.markets else None
    metadata = event.metadata or {}
    prices = (
        {
            "yes_label": market.outcomes.yes.label,
            "yes_price": market.outcomes.yes.price,
            "no_label": market.outcomes.no.label,
            "no_price": market.outcomes.no.price,
        }
        if market
        else {}
    )
    return dict(
        to_jsonable(
            {
                "event_type": "market_resolution",
                "market_id": market.id if market else None,
                "event_id": event.id,
                "slug": event.slug,
                "closed": bool(market.state.closed) if market else None,
                "uma_resolution_status": (
                    market.resolution.uma_resolution_status.value
                    if market and market.resolution.uma_resolution_status
                    else None
                ),
                "price_to_beat": (
                    str(metadata["priceToBeat"]) if "priceToBeat" in metadata else None
                ),
                "final_price": str(metadata["finalPrice"]) if "finalPrice" in metadata else None,
                **prices,
            }
        )
    )
