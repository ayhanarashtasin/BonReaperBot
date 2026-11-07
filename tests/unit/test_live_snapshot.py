"""Watching a live session must not change it, and writing status must not stop a recording."""

from __future__ import annotations

import json
from itertools import pairwise
from pathlib import Path

import pytest

from bone_reaper.live_sim import live_snapshot
from bone_reaper.research.metrics import compute_metrics
from bone_reaper.research.replay import EQUITY_SAMPLE_MS, SimCore
from bone_reaper.settings import parse_settings
from bone_reaper.storage.atomic import atomic_write_text
from bone_reaper.storage.raw_events import read_events
from bone_reaper.types import to_jsonable

FIXTURE = Path(__file__).parents[1] / "fixtures" / "replay"


def paper_settings():
    return parse_settings({"mode": "paper", "strategies": {"passive_quotes": True}})


def live_core(*, snapshot_every: int | None = None) -> tuple[SimCore, list[dict]]:
    """Feed the recording the way a live session does, one event at a time."""
    core = SimCore(paper_settings(), seed=7)
    snapshots = []
    for index, env in enumerate(read_events(FIXTURE, verify=False)):
        core.schedule_envelope(env, live=True)
        core.run_until(env.received_utc_ms)
        if snapshot_every and index % snapshot_every == 0:
            snapshots.append(
                live_snapshot(
                    core, core.scheduler.now, started_utc_ms=env.received_utc_ms, running=True
                )
            )
    return core, snapshots


class TestEquitySampling:
    def test_a_live_feed_samples_on_the_interval_not_per_event(self):
        core, _ = live_core()
        assert core.events_processed > 100
        times = [p["ts_utc_ms"] for p in core.timeline]
        assert len(times) < core.events_processed / 2
        assert all(b - a >= EQUITY_SAMPLE_MS for a, b in pairwise(times))

    def test_sampled_time_does_not_exceed_the_session(self):
        core, _ = live_core()
        times = [p["ts_utc_ms"] for p in core.timeline]
        assert core._sampled_ms <= times[-1] - times[0] + EQUITY_SAMPLE_MS


class TestLiveSnapshot:
    def test_taking_snapshots_does_not_change_the_result(self):
        plain, _ = live_core()
        watched, snapshots = live_core(snapshot_every=50)
        assert snapshots
        assert compute_metrics(watched)["checksum"] == compute_metrics(plain)["checksum"]

    def test_a_snapshot_is_valid_json_and_labelled_simulated(self):
        _, snapshots = live_core(snapshot_every=200)
        doc = json.loads(json.dumps(to_jsonable(snapshots[-1])))
        assert doc["simulated"] is True and doc["running"] is True and doc["mode"] == "paper"
        assert doc["metrics"]["simulated"] is True
        assert doc["equity_curve"] and len(doc["equity_curve"][0]) == 2

    def test_a_snapshot_describes_open_orders_and_the_market(self):
        _, snapshots = live_core(snapshot_every=20)
        with_orders = [s for s in snapshots if s["open_orders"]]
        assert with_orders, "the fixture quotes at some point"
        order = to_jsonable(with_orders[0]["open_orders"][0])
        assert {"outcome", "side", "price", "size", "matched", "state"} <= set(order)
        market = next(s["active_market"] for s in with_orders if s["active_market"])
        assert set(market["sides"]) == {"YES", "NO"}


class TestAtomicWrite:
    def test_writes_and_leaves_no_temp_file(self, tmp_path):
        target = tmp_path / "health.json"
        assert atomic_write_text(target, "one") is True
        assert atomic_write_text(target, "two") is True
        assert target.read_text("utf-8") == "two"
        assert [p.name for p in tmp_path.iterdir()] == ["health.json"]

    def test_a_reader_holding_the_file_does_not_raise(self, tmp_path, monkeypatch):
        target = tmp_path / "health.json"
        target.write_text("old", encoding="utf-8")

        def locked(self, _other):
            raise PermissionError("in use")

        monkeypatch.setattr(Path, "replace", locked)
        assert atomic_write_text(target, "new") is False
        assert target.read_text("utf-8") == "old"

    def test_a_brief_lock_is_retried(self, tmp_path, monkeypatch):
        target = tmp_path / "health.json"
        real_replace = Path.replace
        calls = []

        def locked_once(self, other):
            calls.append(other)
            if len(calls) == 1:
                raise PermissionError("in use")
            return real_replace(self, other)

        monkeypatch.setattr(Path, "replace", locked_once)
        assert atomic_write_text(target, "new") is True
        assert len(calls) == 2 and target.read_text("utf-8") == "new"

    def test_other_storage_errors_still_raise(self, tmp_path):
        with pytest.raises(OSError):
            atomic_write_text(tmp_path / "missing" / "health.json", "x")
