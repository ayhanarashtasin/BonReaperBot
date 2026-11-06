"""Generate tests/fixtures/replay: a small, deterministic, clearly SYNTHETIC recorded dataset.

Three sequential 5-minute markets with the feed semantics measured from real data: mirrored
price_change messages carrying the reported top, trades without price_change, full book messages
after trades, a stream gap with REST recovery, TWAP points, REST samples and official resolutions.

It exists to test the pipeline. Nothing derived from it says anything about real markets.
Run: uv run python scripts/make_replay_fixture.py
"""

from __future__ import annotations

import json
import random
import shutil
import sys
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from bone_reaper.market.rules import rules_hash  # noqa: E402
from bone_reaper.storage.raw_events import RawEventWriter  # noqa: E402
from bone_reaper.types import MarketSpec, MarketStatus, MarketVersion, to_jsonable  # noqa: E402
from helpers import EnvelopeFactory, crypto_fee  # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "replay"
START = 1_800_000_000_000
FIVE_MIN = 300_000
STEP_MS = 200
RULES = json.loads((ROOT / "tests/fixtures/gamma/event_closed_ctf.json").read_text())[0]["markets"][
    0
]["description"]


def price(cents: int) -> str:
    return format(D(cents) / 100, "f")


def spec_for(index: int) -> MarketSpec:
    start = START + index * FIVE_MIN
    return MarketSpec(
        market_id=f"fixture-{index}",
        condition_id=f"0x{index + 1:064x}",
        event_id=f"fixture-event-{index}",
        slug=f"btc-updown-5m-{start // 1000}",
        symbol="BTC",
        duration_minutes=5,
        start_utc_ms=start,
        end_utc_ms=start + FIVE_MIN,
        yes_label="Up",
        no_label="Down",
        yes_asset_id=f"{9000 + index}" + "1" * 60,
        no_asset_id=f"{9000 + index}" + "2" * 60,
        version=MarketVersion.CTF,
        tick_size=D("0.01"),
        min_order_shares=D(5),
        fee=crypto_fee(),
        rules_text=RULES,
        rules_hash=rules_hash(RULES),
        oracle_source="https://data.chain.link/streams/btc-usd-twap-60s-streams",
        status=MarketStatus.ACTIVE,
    )


class SyntheticMarket:
    def __init__(self, spec: MarketSpec, rng: random.Random, factory: EnvelopeFactory) -> None:
        self.spec = spec
        self.rng = rng
        self.factory = factory
        self.bid = 49
        self.ask = 51
        self.bids = {self.bid - i: rng.randint(40, 200) for i in range(5)}
        self.asks = {self.ask + i: rng.randint(40, 200) for i in range(5)}

    def _book(self, asset: str, t: int) -> dict:
        if asset == self.spec.yes_asset_id:
            bids, asks = self.bids, self.asks
        else:
            bids = {100 - p: s for p, s in self.asks.items()}
            asks = {100 - p: s for p, s in self.bids.items()}
        return {
            "event_type": "book",
            "market": self.spec.condition_id,
            "asset_id": asset,
            "bids": [{"price": price(p), "size": str(s)} for p, s in sorted(bids.items())],
            "asks": [
                {"price": price(p), "size": str(s)} for p, s in sorted(asks.items(), reverse=True)
            ],
            "hash": f"fixture-{t}",
            "timestamp": t,
            "tick_size": "0.01",
            "min_order_size": "5",
        }

    def _emit(self, payload: dict, t: int, asset: str | None = None, **kwargs):
        return self.factory.make(
            payload,
            t + 40,
            market_id=self.spec.market_id,
            asset_id=asset,
            source_ts=t,
            **kwargs,
        )

    def books(self, t: int) -> list:
        return [
            self._emit(self._book(self.spec.yes_asset_id, t), t, self.spec.yes_asset_id),
            self._emit(self._book(self.spec.no_asset_id, t), t, self.spec.no_asset_id),
        ]

    def _change(self, side: str, cents: int, size: int, t: int):
        """One venue update reported on both outcome tokens, as the real feed does."""
        spec = self.spec
        other = "SELL" if side == "BUY" else "BUY"
        return self._emit(
            {
                "event_type": "price_change",
                "market": spec.condition_id,
                "timestamp": t,
                "price_changes": [
                    {
                        "asset_id": spec.yes_asset_id,
                        "price": price(cents),
                        "size": str(size),
                        "side": side,
                        "hash": f"y{t}",
                        "best_bid": price(max(self.bids)),
                        "best_ask": price(min(self.asks)),
                    },
                    {
                        "asset_id": spec.no_asset_id,
                        "price": price(100 - cents),
                        "size": str(size),
                        "side": other,
                        "hash": f"n{t}",
                        "best_bid": price(100 - min(self.asks)),
                        "best_ask": price(100 - max(self.bids)),
                    },
                ],
            },
            t,
            "",
        )

    def _trade(self, t: int) -> list:
        """A taker consumes the top of one side. No price_change: a book message reports it."""
        spec = self.spec
        out = []
        if self.rng.random() < 0.5:
            level = min(self.asks)
            size = min(self.asks[level], self.rng.randint(5, 80))
            self.asks[level] -= size
            asset, side, cents = spec.yes_asset_id, "BUY", level
            if self.asks[level] == 0:
                del self.asks[level]
                self.asks.setdefault(level + 5, self.rng.randint(40, 200))
            if self.bids and min(self.asks) - max(self.bids) > 3:
                self.bids[min(self.asks) - 2] = self.rng.randint(30, 150)
        else:
            level = max(self.bids)
            size = min(self.bids[level], self.rng.randint(5, 80))
            self.bids[level] -= size
            asset, side, cents = spec.no_asset_id, "BUY", 100 - level
            if self.bids[level] == 0:
                del self.bids[level]
                self.bids.setdefault(level - 5, self.rng.randint(40, 200))
            if self.asks and min(self.asks) - max(self.bids) > 3:
                self.asks[max(self.bids) + 2] = self.rng.randint(30, 150)
        self.bids = {p: s for p, s in self.bids.items() if 1 <= p < min(self.asks)}
        out.append(
            self._emit(
                {
                    "event_type": "last_trade_price",
                    "market": spec.condition_id,
                    "asset_id": asset,
                    "price": price(cents),
                    "size": str(size),
                    "side": side,
                    "fee_rate_bps": "0",
                    "transaction_hash": f"0xfixture{spec.market_id}{t}",
                    "timestamp": t,
                },
                t,
                asset,
            )
        )
        out.extend(self.books(t + 15))
        return out

    def step(self, t: int) -> list:
        roll = self.rng.random()
        if roll < 0.12:
            return self._trade(t)
        side = "BUY" if self.rng.random() < 0.5 else "SELL"
        levels = self.bids if side == "BUY" else self.asks
        best = max(self.bids) if side == "BUY" else min(self.asks)
        cents = best - self.rng.randint(0, 4) if side == "BUY" else best + self.rng.randint(0, 4)
        if not 1 <= cents <= 99:
            cents = best
        size = self.rng.choice([0, 25, 60, 120, 200])
        if size == 0 and (cents == best or len(levels) <= 1):
            size = 50
        if size == 0:
            levels.pop(cents, None)
        else:
            levels[cents] = size
        return [self._change(side, cents, size, t)]


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    rng = random.Random(20261107)
    factory = EnvelopeFactory()
    events = []
    btc = 75_000.0
    for index in range(3):
        spec = spec_for(index)
        market = SyntheticMarket(spec, rng, factory)
        lead = spec.start_utc_ms - 10_000
        events.append(factory.spec(spec, lead))
        events.append(
            factory.make(
                {
                    "event_type": "clock_probe",
                    "offset_ms": 0,
                    "rtt_ms": 100,
                    "reference": "fixture",
                },
                lead,
                source="control",
                topic="clock_probe",
                market_id=None,
                source_ts=lead,
            )
        )
        events.extend(market.books(lead + 1000))
        strike = round(btc, 2)
        t = lead + 2000
        while t < spec.end_utc_ms + 5000:
            events.extend(market.step(t))
            if t % 1000 == 0:
                btc += rng.gauss(0, 4)
                events.append(
                    factory.make(
                        {
                            "event_type": "twap_update",
                            "feed": "fixture:synthetic",
                            "symbol": "btc/usd",
                            "value": f"{btc:.8f}",
                            "window_seconds": 60,
                            "timestamp": t,
                        },
                        t + 40,
                        source="poly_twap",
                        topic="twap_update",
                        market_id=None,
                        source_ts=t,
                    )
                )
            if index == 1 and t == spec.start_utc_ms + 120_000:
                # A stream gap with REST recovery, so replay and coverage must disclose it.
                gap = {
                    "event_type": "gap",
                    "reason": "stream_error:fixture",
                    "market_id": spec.market_id,
                    "ts_utc_ms": t,
                }
                events.append(factory.make(gap, t + 41, source="control", topic="gap", source_ts=t))
                recovery = market._book(spec.yes_asset_id, t + 2000) | {"origin": "rest"}
                events.append(
                    factory.make(
                        recovery,
                        t + 2040,
                        source="poly_rest",
                        topic="resnapshot",
                        market_id=spec.market_id,
                        asset_id=spec.yes_asset_id,
                        source_ts=t + 2000,
                    )
                )
                other = market._book(spec.no_asset_id, t + 2000) | {"origin": "rest"}
                events.append(
                    factory.make(
                        other,
                        t + 2041,
                        source="poly_rest",
                        topic="resnapshot",
                        market_id=spec.market_id,
                        asset_id=spec.no_asset_id,
                        source_ts=t + 2000,
                    )
                )
                t += 2200
                continue
            if (t - spec.start_utc_ms) % 30_000 == 0 and t < spec.end_utc_ms:
                sample = market._book(spec.yes_asset_id, t) | {"origin": "rest"}
                events.append(
                    factory.make(
                        sample,
                        t + 60,
                        source="poly_rest",
                        topic="rest_book_sample",
                        market_id=spec.market_id,
                        asset_id=spec.yes_asset_id,
                        source_ts=t,
                    )
                )
            t += STEP_MS
        up_wins = btc >= strike
        resolution = factory.resolution(spec, spec.end_utc_ms + 30_000, up_wins)
        events.append(resolution)
    events.sort(key=lambda e: (e.received_utc_ms, e.seq))
    writer = RawEventWriter(OUT, symbol="BTC", run_id="fixture", max_buffer=10**9)
    for env in events:
        writer.append(env)
    writer.flush()
    (OUT / "README.md").write_text(
        "# Synthetic replay fixture\n\n"
        "Generated by `scripts/make_replay_fixture.py`. **Synthetic**: for pipeline tests only. "
        "It says nothing about real markets, fills or profitability.\n",
        encoding="utf-8",
    )
    print(json.dumps(to_jsonable({"events": len(events), "out": str(OUT)})))


if __name__ == "__main__":
    main()
