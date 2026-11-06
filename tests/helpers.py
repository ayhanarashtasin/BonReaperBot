"""Builders for deterministic market specs, payloads and envelopes."""

from __future__ import annotations

import json
from decimal import Decimal

from bone_reaper.market.rules import BTC_5M_RULES_HASH
from bone_reaper.settings import Settings, parse_settings
from bone_reaper.types import (
    BookState,
    BookValidity,
    EventEnvelope,
    FeeCollection,
    FeeParams,
    MarketSpec,
    MarketStatus,
    MarketVersion,
    OrderType,
    Outcome,
    PriceLevel,
    QuoteIntent,
    Side,
    to_jsonable,
)

D = Decimal
START = 1_800_000_000_000
FIVE_MIN = 300_000
YES = "1111"
NO = "2222"


def crypto_fee(collection: FeeCollection = FeeCollection.SHARES_ON_BUY) -> FeeParams:
    return FeeParams(
        enabled=True,
        rate=D("0.07"),
        exponent=D(1),
        taker_only=True,
        rebate_rate=D("0.2"),
        fee_type="crypto_fees_v2",
        collection=collection,
    )


def make_spec(index: int = 0, fee: FeeParams | None = None, **changes) -> MarketSpec:
    start = START + index * FIVE_MIN
    fields = {
        "market_id": f"m{index}",
        "condition_id": f"0x{index:064x}",
        "event_id": f"e{index}",
        "slug": f"btc-updown-5m-{start // 1000}",
        "symbol": "BTC",
        "duration_minutes": 5,
        "start_utc_ms": start,
        "end_utc_ms": start + FIVE_MIN,
        "yes_label": "Up",
        "no_label": "Down",
        "yes_asset_id": f"{YES}{index}",
        "no_asset_id": f"{NO}{index}",
        "version": MarketVersion.CTF,
        "tick_size": D("0.01"),
        "min_order_shares": D(5),
        "fee": fee or crypto_fee(),
        "rules_text": "reviewed rules",
        "rules_hash": BTC_5M_RULES_HASH,
        "oracle_source": "https://data.chain.link/streams/btc-usd-twap-60s-streams",
        "status": MarketStatus.ACTIVE,
    }
    fields.update(changes)
    return MarketSpec(**fields)


def levels(*pairs: tuple[str, str]) -> tuple[PriceLevel, ...]:
    return tuple(PriceLevel(D(p), D(s)) for p, s in pairs)


def book_state(
    asset: str,
    bids: tuple[tuple[str, str], ...] = (),
    asks: tuple[tuple[str, str], ...] = (),
    *,
    now: int = START + 1000,
    market: str = "m0",
    validity: BookValidity = BookValidity.READY,
    tick: str = "0.01",
) -> BookState:
    return BookState(
        market_id=market,
        asset_id=asset,
        bids=levels(*bids),
        asks=levels(*asks),
        tick_size=D(tick),
        last_snapshot_utc_ms=now,
        last_update_utc_ms=now,
        validity=validity,
    )


def make_intent(
    spec: MarketSpec,
    intent_id: str = "i1",
    outcome: Outcome = Outcome.YES,
    price: str = "0.40",
    size: str = "10",
    **changes,
) -> QuoteIntent:
    fields = {
        "intent_id": intent_id,
        "market_id": spec.market_id,
        "asset_id": spec.asset_id(outcome),
        "outcome": outcome,
        "side": Side.BUY,
        "price": D(price),
        "size": D(size),
        "post_only": True,
        "order_type": OrderType.GTC,
        "expiry_cutoff_ms": spec.end_utc_ms,
        "strategy_id": "test",
    }
    fields.update(changes)
    return QuoteIntent(**fields)


def paper_settings(**strategy_changes) -> Settings:
    strategies = {"passive_quotes": True, "scan_pair": True, **strategy_changes}
    return parse_settings({"mode": "paper", "strategies": strategies})


def book_payload(asset: str, bids, asks, ts: int, condition: str = "0x0") -> dict:
    return {
        "event_type": "book",
        "market": condition,
        "asset_id": asset,
        "bids": [{"price": p, "size": s} for p, s in bids],
        "asks": [{"price": p, "size": s} for p, s in asks],
        "timestamp": ts,
        "tick_size": "0.01",
    }


def change(asset: str, side: str, price: str, size: str, best_bid=None, best_ask=None) -> dict:
    return {
        "asset_id": asset,
        "side": side,
        "price": price,
        "size": size,
        "best_bid": best_bid,
        "best_ask": best_ask,
    }


def price_change_payload(ts: int, *changes: dict) -> dict:
    return {
        "event_type": "price_change",
        "market": "0x0",
        "timestamp": ts,
        "price_changes": list(changes),
    }


def trade_payload(asset: str, side: str, price: str, size: str, ts: int, tx: str) -> dict:
    return {
        "event_type": "last_trade_price",
        "market": "0x0",
        "asset_id": asset,
        "price": price,
        "size": size,
        "side": side,
        "timestamp": ts,
        "transaction_hash": tx,
    }


class EnvelopeFactory:
    """Builds envelopes with a deterministic sequence, like the recorder does."""

    def __init__(self) -> None:
        self.seq = 0

    def make(
        self,
        payload: dict,
        recv: int,
        *,
        source: str = "poly_market",
        topic: str | None = None,
        market_id: str | None = "m0",
        asset_id: str | None = None,
        source_ts: int | None = None,
    ) -> EventEnvelope:
        self.seq += 1
        body = json.dumps(payload, sort_keys=True).encode()
        return EventEnvelope(
            event_id=f"{source}:seg:{self.seq}",
            schema_version=1,
            source=source,
            topic=topic or str(payload.get("event_type")),
            market_id=market_id,
            asset_id=asset_id if asset_id is not None else payload.get("asset_id"),
            source_ts_ms=source_ts if source_ts is not None else payload.get("timestamp"),
            received_at_ns=recv * 1_000_000,
            received_utc_ms=recv,
            payload_json=body,
            segment_id="seg",
            seq=self.seq,
        )

    def spec(self, spec: MarketSpec, recv: int) -> EventEnvelope:
        return self.make(
            {"event_type": "market_spec", "spec": to_jsonable(spec)},
            recv,
            source="control",
            topic="market_spec",
            market_id=spec.market_id,
            source_ts=recv,
        )

    def resolution(self, spec: MarketSpec, recv: int, up_wins: bool) -> EventEnvelope:
        payload = {
            "event_type": "market_resolution",
            "market_id": spec.market_id,
            "closed": True,
            "yes_label": "Up",
            "no_label": "Down",
            "yes_price": "1" if up_wins else "0",
            "no_price": "0" if up_wins else "1",
            "price_to_beat": "75000",
            "final_price": "75100" if up_wins else "74900",
        }
        return self.make(
            payload,
            recv,
            source="poly_rest",
            topic="market_resolution",
            market_id=spec.market_id,
            source_ts=recv,
        )
