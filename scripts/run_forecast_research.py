"""Fetch public history and run the forecast evaluation (BR-501 to BR-503). Read-only.

Run: uv run python scripts/run_forecast_research.py --markets 2500 --benchmark-markets 300
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from bone_reaper.connectors.polymarket_public import ReadOnlyTransport
from bone_reaper.market.rules import get_instrument
from bone_reaper.research.model_research import (
    HistoricalMarket,
    fetch_binance_closes,
    fetch_market_prices,
    fetch_markets,
    run_forecast_research,
)
from bone_reaper.research.probability import PricePoint

CACHE = Path("data/history")


async def collect(limit: int, benchmark: int) -> tuple[list[HistoricalMarket], dict]:
    transport = ReadOnlyTransport()
    try:
        markets = await fetch_markets(transport, get_instrument("BTC", 5), limit)
        prices: dict[str, list[PricePoint]] = {}
        failures = 0
        for market in markets[-benchmark:]:
            try:
                prices[market.slug] = await fetch_market_prices(transport, market)
            except Exception:
                failures += 1
            await asyncio.sleep(0.05)
        print(json.dumps({"benchmark_markets": len(prices), "benchmark_failures": failures}))
        return markets, prices
    finally:
        await transport.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--markets", type=int, default=2500)
    parser.add_argument("--benchmark-markets", type=int, default=300)
    parser.add_argument("--output", default="reports/model")
    args = parser.parse_args()
    markets, prices = asyncio.run(collect(args.markets, args.benchmark_markets))
    closes = fetch_binance_closes(markets[0].start_ms - 1_800_000, markets[-1].end_ms)
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / "markets.jsonl").write_text(
        "\n".join(m.to_json() for m in markets) + "\n", encoding="utf-8"
    )
    (CACHE / "closes.json").write_text(json.dumps([asdict(c) for c in closes]), encoding="utf-8")
    report = run_forecast_research(markets, closes, prices, Path(args.output))
    print(
        json.dumps(
            {
                "markets": report["markets"],
                "rows": report["rows"],
                "split": report["split"],
                "verdict": report["verdict"],
                "benchmark_rows": report["benchmark_vs_market_on_test"]["rows"],
            },
            indent=1,
        )
    )


if __name__ == "__main__":
    main()
