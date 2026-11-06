"""Regression: the scanner must follow tick changes, or valid extreme-price books look invalid."""

from __future__ import annotations

import json

from bone_reaper.research.scan_runner import run_scan
from bone_reaper.settings import Settings
from bone_reaper.storage.raw_events import RawEventWriter
from helpers import START, EnvelopeFactory, book_payload, make_spec


def test_scan_follows_tick_change_to_finer_prices(tmp_path):
    factory = EnvelopeFactory()
    spec = make_spec(0)
    y, n = spec.yes_asset_id, spec.no_asset_id
    t = START + 1000
    events = [
        factory.spec(spec, START - 1000),
        factory.make(book_payload(y, [("0.95", "100")], [("0.96", "100")], t), t, source_ts=t),
        factory.make(book_payload(n, [("0.04", "100")], [("0.05", "100")], t), t, source_ts=t),
    ]
    tick_time = t + 500
    for asset in (y, n):
        events.append(
            factory.make(
                {
                    "event_type": "tick_size_change",
                    "market": spec.condition_id,
                    "asset_id": asset,
                    "old_tick_size": "0.01",
                    "new_tick_size": "0.001",
                    "timestamp": tick_time,
                },
                tick_time,
                source_ts=tick_time,
            )
        )
    change_time = tick_time + 1200
    events.append(
        factory.make(
            {
                "event_type": "price_change",
                "market": spec.condition_id,
                "timestamp": change_time,
                "price_changes": [
                    {
                        "asset_id": y,
                        "price": "0.955",
                        "size": "10",
                        "side": "BUY",
                        "best_bid": "0.955",
                        "best_ask": "0.96",
                    },
                    {
                        "asset_id": n,
                        "price": "0.045",
                        "size": "10",
                        "side": "SELL",
                        "best_bid": "0.04",
                        "best_ask": "0.045",
                    },
                ],
            },
            change_time,
            asset_id="",
            source_ts=change_time,
        )
    )
    writer = RawEventWriter(tmp_path / "raw", run_id="r")
    for env in events:
        writer.append(env)
    writer.flush()

    report = tmp_path / "scan.json"
    run_scan(tmp_path / "raw", Settings(), report)
    rows = [json.loads(x) for x in report.with_suffix(".records.jsonl").read_text().splitlines()]
    last = [r for r in rows if r["ts_utc_ms"] == change_time]
    assert last, "the update after the tick change was evaluated"
    for row in last:
        reasons = " ".join(row["reasons"])
        assert "off_tick" not in reasons and "book_invalid" not in reasons
