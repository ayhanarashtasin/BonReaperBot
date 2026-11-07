"""Read-only recorder service wiring discovery, rollover, streams, resnapshots and storage."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .connectors.binance import SOURCE_BINANCE, measure_clock_offset, stream_spot_agg_trades
from .connectors.polymarket_market_stream import (
    SOURCE_CONTROL,
    SOURCE_MARKET,
    SOURCE_REST,
    make_envelope,
    stream_market,
)
from .connectors.polymarket_public import ReadOnlyTransport, resolution_payload
from .connectors.polymarket_twap import SOURCE_TWAP, RtdsTwapSource
from .execution.settlement import ResolutionUnknownError, winning_outcome_from_resolution
from .logging import log_event
from .market.book import BookManager
from .market.discovery import discover
from .market.rollover import RolloverController
from .market.rules import Instrument
from .settings import Settings
from .storage.atomic import atomic_write_text
from .storage.checkpoints import Checkpoint, CheckpointWriter
from .storage.raw_events import RawEventWriter
from .time import Clock, SystemClock
from .types import EventEnvelope, MarketSpec, to_jsonable

LOG = logging.getLogger("bone_reaper.recorder")
RETIRE_GRACE_MS = 15_000
RESNAPSHOT_MIN_INTERVAL_MS = 5_000
DISCOVERY_INTERVAL_S = 10.0
RESOLUTION_POLL_S = 20.0
RESOLUTION_MAX_POLLS = 40


@dataclass(slots=True)
class RecorderState:
    events_by_source: Counter[str] = field(default_factory=Counter)
    last_event_utc_ms: dict[str, int] = field(default_factory=dict)
    gaps: list[dict[str, Any]] = field(default_factory=list)
    invalidations: Counter[str] = field(default_factory=Counter)
    resnapshots: int = 0
    rest_samples: int = 0
    markets_seen: dict[str, str] = field(default_factory=dict)
    resolutions: dict[str, dict[str, Any]] = field(default_factory=dict)
    rollover_failures: list[str] = field(default_factory=list)
    twap_status: str = "not_started"
    binance_status: str = "disabled"
    clock_offset_ms: int | None = None


class Recorder:
    def __init__(
        self,
        settings: Settings,
        instrument: Instrument,
        out_dir: Path,
        *,
        transport: ReadOnlyTransport,
        clock: Clock | None = None,
        enable_twap: bool = True,
        enable_binance: bool = False,
        market_stream: Callable[..., AsyncIterator[tuple[dict[str, Any], int]]] = stream_market,
        twap_source: Any = None,
    ) -> None:
        self.settings = settings
        self.instrument = instrument
        self.out_dir = out_dir
        self.transport = transport
        self.clock: Clock = clock or SystemClock()
        self.enable_twap = enable_twap
        self.enable_binance = enable_binance
        self._market_stream = market_stream
        self._twap_source = twap_source
        self.run_id = uuid.uuid4().hex[:12]
        self.writer = RawEventWriter(out_dir, symbol=instrument.symbol, run_id=self.run_id)
        self.checkpoints = CheckpointWriter(out_dir / "checkpoints" / f"{self.run_id}.jsonl")
        self.books = BookManager()
        self.rollover = RolloverController(
            quote_cutoff_seconds=settings.risk.stop_new_quotes_seconds_before_close
        )
        self.state = RecorderState()
        self._seq = 0
        self._specs: dict[str, MarketSpec] = {}
        self._streams: dict[str, asyncio.Task[None]] = {}
        self._resnapshotting: set[str] = set()
        self._last_resnapshot: dict[str, int] = {}
        self._background: set[asyncio.Task[None]] = set()
        self.tap: Callable[[EventEnvelope], None] | None = None

    def _emit(
        self,
        payload: dict[str, Any],
        *,
        source: str,
        topic: str,
        segment: str,
        market_id: str | None = None,
        asset_id: str | None = None,
        source_ts_ms: int | None = None,
    ) -> int:
        self._seq += 1
        envelope = make_envelope(
            payload,
            source=source,
            topic=topic,
            clock=self.clock,
            segment_id=segment,
            seq=self._seq,
            market_id=market_id,
            asset_id=asset_id,
            source_ts_ms=source_ts_ms,
        )
        self.writer.append(envelope)
        if self.tap is not None:
            self.tap(envelope)
        self.state.events_by_source[source] += 1
        self.state.last_event_utc_ms[source] = envelope.received_utc_ms
        return envelope.received_utc_ms

    def _gap(self, reason: str, segment: str, market_id: str | None = None) -> None:
        record = {
            "event_type": "gap",
            "reason": reason,
            "segment": segment,
            "market_id": market_id,
            "ts_utc_ms": self.clock.utc_ms(),
        }
        self.state.gaps.append(record)
        self._emit(record, source=SOURCE_CONTROL, topic="gap", segment=segment, market_id=market_id)
        log_event(LOG, logging.WARNING, "data gap", run_id=self.run_id, **record)

    # --- discovery and rollover --------------------------------------------------------------

    async def _discovery_loop(self) -> None:
        while True:
            now = self.clock.utc_ms()
            try:
                result = await discover(
                    self.transport,
                    self.instrument,
                    now,
                    limit=4,
                    collection=self.settings.execution.fee_collection,
                )
            except Exception as exc:
                self._gap(f"discovery_failed:{type(exc).__name__}", f"{self.run_id}:discovery")
                result = None
            accepted: list[MarketSpec] = []
            if result is not None:
                for rejection in result.rejected:
                    self._emit(
                        {"event_type": "market_rejected", **to_jsonable(rejection)},
                        source=SOURCE_CONTROL,
                        topic="market_rejected",
                        segment=f"{self.run_id}:discovery",
                    )
                accepted = list(result.accepted)[:2]
                self._accept(accepted)
            self._apply_rollover(accepted, self.clock.utc_ms())
            self.state.rollover_failures = list(self.rollover.failures)
            await asyncio.sleep(DISCOVERY_INTERVAL_S)

    def _accept(self, specs: list[MarketSpec]) -> None:
        for spec in specs:
            known = self._specs.get(spec.market_id)
            if known is not None:
                # discover() already rejects changed rules. A tick change is normal venue behaviour
                # near price extremes and arrives on the stream; it is not a data gap.
                if known.fee != spec.fee or known.version != spec.version:
                    self._gap("market_terms_changed", f"{self.run_id}:discovery", spec.market_id)
                continue
            self._specs[spec.market_id] = spec
            self.state.markets_seen[spec.market_id] = spec.slug
            self._emit(
                {"event_type": "market_spec", "spec": to_jsonable(spec)},
                source=SOURCE_CONTROL,
                topic="market_spec",
                segment=f"{self.run_id}:discovery",
                market_id=spec.market_id,
            )
            self.books.register(spec.market_id, spec.yes_asset_id, spec.tick_size)
            self.books.register(spec.market_id, spec.no_asset_id, spec.tick_size)
            self._streams[spec.market_id] = asyncio.create_task(self._stream_loop(spec))

    def _apply_rollover(self, specs: list[MarketSpec], now: int) -> None:
        """The only caller of rollover.update: its events drive streams and resolution watching."""
        for event in self.rollover.update(specs, now):
            self._emit(
                {"event_type": "rollover", **to_jsonable(event)},
                source=SOURCE_CONTROL,
                topic="rollover",
                segment=f"{self.run_id}:rollover",
                market_id=event.market_id,
            )
            log_event(LOG, logging.INFO, "rollover", kind=event.kind, slug=event.slug)
            if event.kind == "retired":
                watcher = asyncio.create_task(self._resolution_watch(self._specs[event.market_id]))
                self._background.add(watcher)
                watcher.add_done_callback(self._background.discard)
        for spec in self.rollover.retired:
            stream = self._streams.get(spec.market_id)
            if stream is not None and now >= spec.end_utc_ms + RETIRE_GRACE_MS:
                stream.cancel()
                self._streams.pop(spec.market_id, None)

    # --- streams --------------------------------------------------------------------------------

    async def _stream_loop(self, spec: MarketSpec) -> None:
        attempt = 0
        assets = (spec.yes_asset_id, spec.no_asset_id)
        while True:
            attempt += 1
            segment = f"{self.run_id}:{spec.market_id}:{attempt}"
            dropped_seen = 0
            try:
                async for payload, dropped in self._market_stream(self.transport.client, assets):
                    if dropped > dropped_seen:
                        dropped_seen = dropped
                        self._gap("sdk_queue_dropped_events", segment, spec.market_id)
                        self.books.invalidate_all("sdk_queue_dropped_events")
                    self._on_market_payload(payload, spec, segment)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._gap(f"stream_error:{type(exc).__name__}", segment, spec.market_id)
                for asset in assets:
                    if asset in self.books.books:
                        self.books.books[asset].invalidate("stream_disconnected")
            else:
                self._gap("stream_ended", segment, spec.market_id)
            await asyncio.sleep(min(2 * attempt, 15))

    def _on_market_payload(self, payload: dict[str, Any], spec: MarketSpec, segment: str) -> None:
        # new_market announcements describe other markets; never attribute them to this one.
        own = payload.get("market") == spec.condition_id
        recv = self._emit(
            payload,
            source=SOURCE_MARKET,
            topic=str(payload.get("event_type")),
            segment=segment,
            market_id=spec.market_id if own else None,
            asset_id=payload.get("asset_id"),
            source_ts_ms=payload.get("timestamp"),
        )
        if not own:
            return
        result = self.books.apply(payload, recv)
        for asset_id, reason in result.invalidated:
            self.state.invalidations[reason] += 1
            owner = self._specs.get(self.books.books[asset_id].market_id)
            if owner is not None:
                self._schedule_resnapshot(asset_id, owner, reason)

    def _schedule_resnapshot(self, asset_id: str, spec: MarketSpec, reason: str) -> None:
        now = self.clock.utc_ms()
        # At most one REST recovery per asset per interval, however noisy the stream gets.
        if (
            asset_id in self._resnapshotting
            or now - self._last_resnapshot.get(asset_id, 0) < RESNAPSHOT_MIN_INTERVAL_MS
        ):
            return
        self._last_resnapshot[asset_id] = now
        self._resnapshotting.add(asset_id)
        task = asyncio.create_task(self._resnapshot(asset_id, spec, reason))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _resnapshot(self, asset_id: str, spec: MarketSpec, reason: str) -> None:
        try:
            payload = await self.transport.book_payload(asset_id)
            payload["resnapshot_reason"] = reason
            recv = self._emit(
                payload,
                source=SOURCE_REST,
                topic="resnapshot",
                segment=f"{self.run_id}:rest",
                market_id=spec.market_id,
                asset_id=asset_id,
                source_ts_ms=payload.get("timestamp"),
            )
            self.books.apply(payload, recv)
            self.state.resnapshots += 1
        except Exception as exc:
            self._gap(
                f"resnapshot_failed:{type(exc).__name__}", f"{self.run_id}:rest", spec.market_id
            )
        finally:
            self._resnapshotting.discard(asset_id)

    async def _rest_sampler(self) -> None:
        """Independent REST snapshots: not applied to books, used to validate rebuilds (FR-04)."""
        while True:
            await asyncio.sleep(30)
            active = self.rollover.active
            if active is None:
                continue
            for asset_id in (active.yes_asset_id, active.no_asset_id):
                try:
                    payload = await self.transport.book_payload(asset_id)
                except Exception as exc:
                    self._gap(f"rest_sample_failed:{type(exc).__name__}", f"{self.run_id}:rest")
                    continue
                self._emit(
                    payload,
                    source=SOURCE_REST,
                    topic="rest_book_sample",
                    segment=f"{self.run_id}:rest",
                    market_id=active.market_id,
                    asset_id=asset_id,
                    source_ts_ms=payload.get("timestamp"),
                )
                self.state.rest_samples += 1

    async def _resolution_watch(self, spec: MarketSpec) -> None:
        """Record the official outcome as soon as it is final. Old markets stay tracked until then.

        The winner is published at close, but ``finalPrice`` arrives minutes later. So the outcome
        is recorded first, then once more when the final price appears.
        """
        recorded_outcome = False
        for _ in range(RESOLUTION_MAX_POLLS):
            await asyncio.sleep(RESOLUTION_POLL_S)
            try:
                event = await self.transport.event_by_slug(spec.slug)
                payload = resolution_payload(event)
                winning_outcome_from_resolution(payload, spec)
            except ResolutionUnknownError:
                continue  # Not final yet.
            except Exception as exc:
                log_event(LOG, logging.WARNING, "resolution poll failed", error=type(exc).__name__)
                continue
            has_final_price = payload.get("final_price") is not None
            if not recorded_outcome or has_final_price:
                self._emit(
                    payload,
                    source=SOURCE_REST,
                    topic="market_resolution",
                    segment=f"{self.run_id}:rest",
                    market_id=spec.market_id,
                )
                self.state.resolutions[spec.market_id] = payload
                recorded_outcome = True
            if has_final_price:
                return
        if not recorded_outcome:
            self._gap("resolution_not_observed", f"{self.run_id}:rest", spec.market_id)

    async def _clock_probe_loop(self) -> None:
        """Record local clock offset so replay can separate clock error from network latency."""
        while True:
            try:
                probe = await measure_clock_offset(self.clock)
            except Exception as exc:
                self.state.clock_offset_ms = None
                log_event(LOG, logging.WARNING, "clock probe failed", error=type(exc).__name__)
            else:
                self.state.clock_offset_ms = probe["offset_ms"]
                self._emit(
                    {"event_type": "clock_probe", **probe},
                    source=SOURCE_CONTROL,
                    topic="clock_probe",
                    segment=f"{self.run_id}:clock",
                )
            await asyncio.sleep(60)

    async def _twap_loop(self) -> None:
        source = self._twap_source or RtdsTwapSource(
            self.transport.client, self.instrument.rtds_symbol
        )
        attempt = 0
        while True:
            attempt += 1
            segment = f"{self.run_id}:twap:{attempt}"
            try:
                async for payload in source.stream():
                    self.state.twap_status = f"available:{payload['feed']}"
                    self._emit(
                        payload,
                        source=SOURCE_TWAP,
                        topic=str(payload["event_type"]),
                        segment=segment,
                        source_ts_ms=payload.get("timestamp"),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.state.twap_status = f"unavailable:{type(exc).__name__}"
                self._gap(f"twap_error:{type(exc).__name__}", segment)
            await asyncio.sleep(min(5 * attempt, 60))

    async def _binance_loop(self) -> None:
        attempt = 0
        while True:
            attempt += 1
            segment = f"{self.run_id}:binance:{attempt}"
            try:
                self.state.binance_status = "connecting"
                async for payload in stream_spot_agg_trades("btcusdt"):
                    self.state.binance_status = "available:BTCUSDT_spot"
                    self._emit(
                        payload,
                        source=SOURCE_BINANCE,
                        topic="agg_trade",
                        segment=segment,
                        source_ts_ms=payload.get("timestamp"),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.state.binance_status = f"unavailable:{type(exc).__name__}"
                self._gap(f"binance_error:{type(exc).__name__}", segment)
            await asyncio.sleep(min(5 * attempt, 60))

    # --- periodic persistence -----------------------------------------------------------------

    def health_document(self) -> dict[str, Any]:
        now = self.clock.utc_ms()
        books = {
            asset: {
                "market_id": book.market_id,
                "validity": book.validity.value,
                "invalid_reason": book.invalid_reason,
                "age_ms": now - book.last_update_utc_ms if book.last_update_utc_ms else None,
                "snapshots": book.snapshot_count,
            }
            for asset, book in self.books.books.items()
            if book.market_id in {s.market_id for s in self._current_specs()}
        }
        active = self.rollover.active
        return {
            "run_id": self.run_id,
            "ts_utc_ms": now,
            "mode": self.settings.mode.value,
            "config_hash": self.settings.config_hash(),
            "active_market": active.slug if active else None,
            "active_rule_hash": active.rules_hash if active else None,
            "active_fee_version": active.fee.version if active else None,
            "events_by_source": dict(self.state.events_by_source),
            "source_age_ms": {k: now - v for k, v in self.state.last_event_utc_ms.items()},
            "gaps": len(self.state.gaps),
            "recent_gaps": self.state.gaps[-5:],
            "invalidations": dict(self.state.invalidations),
            "resnapshots": self.state.resnapshots,
            "rest_samples": self.state.rest_samples,
            "rows_written": self.writer.rows_written,
            "markets_seen": len(self.state.markets_seen),
            "markets_resolved": len(self.state.resolutions),
            "rollover_failures": self.state.rollover_failures,
            "twap_status": self.state.twap_status,
            "binance_status": self.state.binance_status,
            "clock_offset_ms": self.state.clock_offset_ms,
            "books": books,
        }

    def _current_specs(self) -> list[MarketSpec]:
        return ([self.rollover.active] if self.rollover.active else []) + self.rollover.prepared

    def _write_health(self) -> None:
        atomic_write_text(
            self.out_dir / "health.json",
            json.dumps(self.health_document(), indent=1, sort_keys=True),
        )

    async def _flush_loop(self) -> None:
        interval = self.settings.storage.flush_interval_seconds
        ticks_per_checkpoint = max(1, self.settings.storage.checkpoint_interval_seconds // interval)
        tick = 0
        while True:
            await asyncio.sleep(interval)
            tick += 1
            self.writer.flush()
            self._write_health()
            if tick % ticks_per_checkpoint == 0:
                self.checkpoints.write(
                    Checkpoint(
                        ts_utc_ms=self.clock.utc_ms(),
                        raw_event_offset=self._seq,
                        books=tuple(b.state() for b in self.books.books.values()),
                    )
                )

    async def run(self, duration_s: float | None = None) -> dict[str, Any]:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        # Anything between an earlier run and this one was not observed, so say so in the data.
        self._gap("recorder_started", f"{self.run_id}:control")
        tasks = [
            asyncio.create_task(self._discovery_loop()),
            asyncio.create_task(self._flush_loop()),
            asyncio.create_task(self._rest_sampler()),
            asyncio.create_task(self._clock_probe_loop()),
        ]
        if self.enable_twap:
            tasks.append(asyncio.create_task(self._twap_loop()))
        if self.enable_binance:
            tasks.append(asyncio.create_task(self._binance_loop()))
        try:
            if duration_s is None:
                await asyncio.gather(*tasks)
            else:
                done, _pending = await asyncio.wait(tasks, timeout=duration_s)
                for task in done:
                    task.result()
        finally:
            everything = [*tasks, *self._streams.values(), *self._background]
            for task in everything:
                task.cancel()
            for task in everything:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            self.writer.flush()
            self._write_health()
        return self.health_document()
