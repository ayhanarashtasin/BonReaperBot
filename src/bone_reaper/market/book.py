"""L2 book materialization from normalized stream payloads.

Feed semantics measured on recorded data (reports/sdk_audit.md, ADR-0005):

* ``price_change`` carries absolute level sizes for order placement and cancellation, always one
  entry per outcome, plus the venue's current best bid and ask for that asset.
* A trade sends no ``price_change``. The consumed levels change only at the next full ``book``
  message, so between a trade and that message our book can hold levels that no longer exist.
* There are no sequence numbers.

So the reported top of book is used two ways. If it is *worse* than ours, our better levels are
provably gone and are pruned. If it is *better* than ours, we are missing a level we cannot infer;
the book goes SYNCING, is unusable for decisions, and becomes INVALID if a snapshot does not repair
it within the grace period. Sizes of partly consumed levels stay stale until the next ``book``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any

from ..types import BookState, BookValidity, PriceLevel

DEFAULT_SYNC_GRACE_MS = 2000


def _dec(value: Any) -> Decimal:
    return Decimal(str(value))


@lru_cache(maxsize=4096)
def _price(text: str) -> Decimal:
    """Prices come from a small set of ticks and arrive hundreds of times a second."""
    return Decimal(text)


@lru_cache(maxsize=16384)
def _on_tick(price: Decimal, tick: Decimal) -> bool:
    try:
        return (price % tick) == 0 and Decimal(0) < price < Decimal(1)
    except InvalidOperation:
        return False


def _levels(raw: Iterable[Mapping[str, Any]]) -> dict[Decimal, Decimal]:
    out: dict[Decimal, Decimal] = {}
    for level in raw:
        out[_dec(level["price"])] = _dec(level["size"])
    return out


class L2Book:
    def __init__(self, market_id: str, asset_id: str, tick_size: Decimal) -> None:
        self.market_id = market_id
        self.asset_id = asset_id
        self.tick_size = tick_size
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.validity = BookValidity.UNINITIALIZED
        self.invalid_reason: str | None = None
        self.book_hash: str | None = None
        self.last_snapshot_utc_ms: int | None = None
        self.last_update_utc_ms: int | None = None
        self.last_source_ts_ms: int | None = None
        self.sync_since_utc_ms: int | None = None
        self.ignored_before_snapshot = 0
        self.snapshot_count = 0
        self.pruned_levels = 0
        self.sync_entries = 0
        self._best_bid: Decimal | None = None
        self._best_ask: Decimal | None = None

    def invalidate(self, reason: str) -> None:
        self.validity = BookValidity.INVALID
        self.invalid_reason = reason
        self.sync_since_utc_ms = None

    def apply_snapshot(
        self,
        bids: Iterable[Mapping[str, Any]],
        asks: Iterable[Mapping[str, Any]],
        recv_utc_ms: int,
        source_ts_ms: int | None = None,
        book_hash: str | None = None,
        tick_size: Decimal | None = None,
    ) -> None:
        if tick_size is not None:
            self.tick_size = tick_size
        self.bids = _levels(bids)
        self.asks = _levels(asks)
        self._best_bid = max(self.bids) if self.bids else None
        self._best_ask = min(self.asks) if self.asks else None
        self.book_hash = book_hash
        self.last_snapshot_utc_ms = recv_utc_ms
        self.last_update_utc_ms = recv_utc_ms
        self.last_source_ts_ms = source_ts_ms
        self.snapshot_count += 1
        self.validity = BookValidity.READY
        self.invalid_reason = None
        self.sync_since_utc_ms = None
        self._validate_all()

    def apply_level(self, side: str, price: Decimal, size: Decimal) -> None:
        """Exact size replacement; zero deletes. Caller reconciles the top after a batch."""
        if self.validity is BookValidity.UNINITIALIZED:
            self.ignored_before_snapshot += 1
            return
        if size < 0:
            self.invalidate("negative_size")
            return
        if not self._on_tick(price):
            self.invalidate("off_tick_price")
            return
        if side == "BUY":
            if size == 0:
                self.bids.pop(price, None)
                if price == self._best_bid:
                    self._best_bid = max(self.bids) if self.bids else None
            else:
                self.bids[price] = size
                if self._best_bid is None or price > self._best_bid:
                    self._best_bid = price
        elif size == 0:
            self.asks.pop(price, None)
            if price == self._best_ask:
                self._best_ask = min(self.asks) if self.asks else None
        else:
            self.asks[price] = size
            if self._best_ask is None or price < self._best_ask:
                self._best_ask = price

    def apply_tick_change(self, new_tick: Decimal, recv_utc_ms: int) -> None:
        """The tick moves near price extremes. A coarser tick is repaired by the next snapshot."""
        self.tick_size = new_tick
        self.last_update_utc_ms = recv_utc_ms
        self._validate_all()

    def touch(self, recv_utc_ms: int, source_ts_ms: int | None) -> None:
        self.last_update_utc_ms = recv_utc_ms
        if source_ts_ms is not None:
            self.last_source_ts_ms = source_ts_ms

    def _on_tick(self, price: Decimal) -> bool:
        return _on_tick(price, self.tick_size)

    def _validate_all(self) -> None:
        for levels in (self.bids, self.asks):
            for price, size in levels.items():
                if size <= 0:
                    self.invalidate("nonpositive_size_in_snapshot")
                    return
                if not self._on_tick(price):
                    self.invalidate("off_tick_price")
                    return
        if self._crossed():
            self.invalidate("crossed_book")

    def _crossed(self) -> bool:
        best_bid = self.best_bid
        best_ask = self.best_ask
        return best_bid is not None and best_ask is not None and best_bid >= best_ask

    def reconcile_top(
        self, reported_bid: Decimal | None, reported_ask: Decimal | None, recv_utc_ms: int
    ) -> None:
        """Bring the book in line with the venue-reported top, or mark it SYNCING."""
        if self.validity not in (BookValidity.READY, BookValidity.SYNCING):
            return
        # Only scan the levels when the cached best shows there is something to prune.
        if (
            reported_bid is not None
            and self._best_bid is not None
            and self._best_bid > reported_bid
        ):
            for price in [p for p in self.bids if p > reported_bid]:
                del self.bids[price]
                self.pruned_levels += 1
            self._best_bid = max(self.bids) if self.bids else None
        if (
            reported_ask is not None
            and self._best_ask is not None
            and self._best_ask < reported_ask
        ):
            for price in [p for p in self.asks if p < reported_ask]:
                del self.asks[price]
                self.pruned_levels += 1
            self._best_ask = min(self.asks) if self.asks else None
        bid_ok = reported_bid is None or reported_bid == (self.best_bid or Decimal(0))
        ask_ok = reported_ask is None or reported_ask == (self.best_ask or Decimal(1))
        if bid_ok and ask_ok and not self._crossed():
            self.validity = BookValidity.READY
            self.invalid_reason = None
            self.sync_since_utc_ms = None
        elif self.validity is BookValidity.READY:
            self.validity = BookValidity.SYNCING
            self.invalid_reason = "reported_top_better_than_book"
            self.sync_since_utc_ms = recv_utc_ms
            self.sync_entries += 1

    def expire_sync(self, now_utc_ms: int, grace_ms: int) -> bool:
        """Escalate a SYNCING book that no snapshot repaired. Returns True if it escalated."""
        if self.validity is not BookValidity.SYNCING or self.sync_since_utc_ms is None:
            return False
        if now_utc_ms - self.sync_since_utc_ms <= grace_ms:
            return False
        self.invalidate("desync_timeout")
        return True

    @property
    def best_bid(self) -> Decimal | None:
        return self._best_bid

    @property
    def best_ask(self) -> Decimal | None:
        return self._best_ask

    def state(self) -> BookState:
        return BookState(
            market_id=self.market_id,
            asset_id=self.asset_id,
            bids=tuple(PriceLevel(p, self.bids[p]) for p in sorted(self.bids, reverse=True)),
            asks=tuple(PriceLevel(p, self.asks[p]) for p in sorted(self.asks)),
            tick_size=self.tick_size,
            last_snapshot_utc_ms=self.last_snapshot_utc_ms,
            last_update_utc_ms=self.last_update_utc_ms,
            validity=self.validity,
            book_hash=self.book_hash,
            invalid_reason=self.invalid_reason,
        )


@dataclass(slots=True)
class ApplyResult:
    changed_assets: list[str] = field(default_factory=list)
    invalidated: list[tuple[str, str]] = field(default_factory=list)
    trade: dict[str, Any] | None = None


class BookManager:
    """All books for registered assets. Assets outside the registry are counted, never applied."""

    def __init__(self, sync_grace_ms: int = DEFAULT_SYNC_GRACE_MS) -> None:
        self.books: dict[str, L2Book] = {}
        self.unregistered_events = 0
        self.sync_grace_ms = sync_grace_ms

    def register(self, market_id: str, asset_id: str, tick_size: Decimal) -> L2Book:
        book = self.books.get(asset_id)
        if book is None:
            book = L2Book(market_id, asset_id, tick_size)
            self.books[asset_id] = book
        return book

    def unregister(self, asset_id: str) -> None:
        self.books.pop(asset_id, None)

    def invalidate_all(self, reason: str) -> None:
        for book in self.books.values():
            book.invalidate(reason)

    def expire_syncing(self, now_utc_ms: int) -> list[tuple[str, str]]:
        expired = []
        for book in self.books.values():
            if book.expire_sync(now_utc_ms, self.sync_grace_ms):
                expired.append((book.asset_id, book.invalid_reason or "desync_timeout"))
        return expired

    def apply(self, payload: Mapping[str, Any], recv_utc_ms: int) -> ApplyResult:
        result = ApplyResult()
        kind = payload.get("event_type")
        source_ts = payload.get("timestamp")
        source_ts_ms = int(source_ts) if source_ts is not None else None
        if kind == "book":
            self._apply_book(payload, recv_utc_ms, source_ts_ms, result)
        elif kind == "price_change":
            self._apply_price_change(payload, recv_utc_ms, source_ts_ms, result)
        elif kind == "tick_size_change":
            book = self.books.get(str(payload.get("asset_id")))
            if book is None:
                self.unregistered_events += 1
            else:
                book.apply_tick_change(_dec(payload["new_tick_size"]), recv_utc_ms)
                result.changed_assets.append(book.asset_id)
                self._note_invalid(book, result)
        elif kind == "last_trade_price":
            result.trade = dict(payload)
        result.invalidated.extend(self.expire_syncing(recv_utc_ms))
        return result

    def _apply_book(
        self,
        payload: Mapping[str, Any],
        recv_utc_ms: int,
        source_ts_ms: int | None,
        result: ApplyResult,
    ) -> None:
        book = self.books.get(str(payload.get("asset_id")))
        if book is None:
            self.unregistered_events += 1
            return
        tick = payload.get("tick_size")
        book.apply_snapshot(
            payload.get("bids", ()),
            payload.get("asks", ()),
            recv_utc_ms,
            source_ts_ms,
            payload.get("hash"),
            _dec(tick) if tick is not None else None,
        )
        result.changed_assets.append(book.asset_id)
        self._note_invalid(book, result)

    def _apply_price_change(
        self,
        payload: Mapping[str, Any],
        recv_utc_ms: int,
        source_ts_ms: int | None,
        result: ApplyResult,
    ) -> None:
        reported: dict[str, tuple[Any, Any]] = {}
        for change in payload.get("price_changes", ()):
            asset_id = str(change.get("asset_id"))
            book = self.books.get(asset_id)
            if book is None:
                self.unregistered_events += 1
                continue
            book.apply_level(change["side"], _price(change["price"]), Decimal(change["size"]))
            reported[asset_id] = (change.get("best_bid"), change.get("best_ask"))
        for asset_id, (best_bid, best_ask) in reported.items():
            book = self.books[asset_id]
            if book.validity is BookValidity.UNINITIALIZED:
                continue
            book.touch(recv_utc_ms, source_ts_ms)
            book.reconcile_top(
                _price(best_bid) if best_bid is not None else None,
                _price(best_ask) if best_ask is not None else None,
                recv_utc_ms,
            )
            result.changed_assets.append(asset_id)
            self._note_invalid(book, result)

    @staticmethod
    def _note_invalid(book: L2Book, result: ApplyResult) -> None:
        if book.validity is BookValidity.INVALID:
            result.invalidated.append((book.asset_id, book.invalid_reason or "unknown"))
