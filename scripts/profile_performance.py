"""BR-803: measure simulator throughput and decision latency on real recorded data.

Run: uv run python scripts/profile_performance.py --input data/raw --events 300000
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import time
from pathlib import Path

import psutil

from bone_reaper.research.metrics import compute_metrics
from bone_reaper.research.replay import SimCore, replay_events
from bone_reaper.settings import load_settings
from bone_reaper.storage.raw_events import read_events


def percentile(ordered: list[float], q: float) -> float:
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/raw")
    parser.add_argument("--events", type=int, default=300_000)
    parser.add_argument("--output", default="reports/performance.json")
    args = parser.parse_args()
    settings = load_settings("config/paper.yaml")
    started = time.perf_counter()
    events = list(itertools.islice(read_events(Path(args.input), verify=False), args.events))
    load_s = time.perf_counter() - started

    ticks: list[float] = []
    original = SimCore._decide

    def timed(self: SimCore, now: int) -> None:
        begin = time.perf_counter()
        was_due = self.orchestrator.due(now)
        original(self, now)
        if was_due:
            ticks.append((time.perf_counter() - begin) * 1000)

    SimCore._decide = timed  # type: ignore[method-assign]
    started = time.perf_counter()
    core = replay_events(events, settings, seed=7)
    replay_s = time.perf_counter() - started
    SimCore._decide = original  # type: ignore[method-assign]
    metrics = compute_metrics(core)
    ticks.sort()
    report = {
        "cpu_count": os.cpu_count(),
        "ram_gb": round(psutil.virtual_memory().total / 2**30, 1),
        "events": len(events),
        "load_seconds": round(load_s, 2),
        "replay_seconds": round(replay_s, 2),
        "events_per_second": round(len(events) / replay_s),
        "peak_rss_gb": round(psutil.Process().memory_info().rss / 2**30, 2),
        "decision_ticks": len(ticks),
        "decision_ms": {
            "p50": round(percentile(ticks, 0.50), 3),
            "p95": round(percentile(ticks, 0.95), 3),
            "p99": round(percentile(ticks, 0.99), 3),
        },
        "metrics_checksum": metrics["checksum"],
        "not_measured": "Real submit, ACK and cancel latency: no order is ever sent.",
    }
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
