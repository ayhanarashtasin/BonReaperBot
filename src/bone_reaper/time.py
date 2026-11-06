"""Clocks and skew detection. Source, UTC receive and monotonic arrival stay separate fields."""

from __future__ import annotations

import time as _stdtime
from dataclasses import dataclass
from typing import Protocol


class Clock(Protocol):
    def utc_ms(self) -> int: ...
    def monotonic_ns(self) -> int: ...


class SystemClock:
    def utc_ms(self) -> int:
        return _stdtime.time_ns() // 1_000_000

    def monotonic_ns(self) -> int:
        return _stdtime.monotonic_ns()


class FakeClock:
    """Deterministic clock for tests and replay."""

    def __init__(self, utc_ms: int = 0, monotonic_ns: int = 0) -> None:
        self._utc_ms = utc_ms
        self._mono_ns = monotonic_ns

    def utc_ms(self) -> int:
        return self._utc_ms

    def monotonic_ns(self) -> int:
        return self._mono_ns

    def advance_ms(self, delta_ms: int) -> None:
        if delta_ms < 0:
            raise ValueError("monotonic time cannot go backwards")
        self._utc_ms += delta_ms
        self._mono_ns += delta_ms * 1_000_000

    def set_utc_ms(self, utc_ms: int) -> None:
        """Move wall-clock only, as an NTP step would. Monotonic time is unaffected."""
        self._utc_ms = utc_ms


@dataclass(frozen=True, slots=True)
class ArrivalStamp:
    source_ts_ms: int | None
    source_ts_estimated: bool
    received_utc_ms: int
    received_at_ns: int


def stamp(clock: Clock, source_ts_ms: int | None) -> ArrivalStamp:
    """Never substitutes receive time for a missing source time without flagging it."""
    return ArrivalStamp(
        source_ts_ms=source_ts_ms,
        source_ts_estimated=source_ts_ms is None,
        received_utc_ms=clock.utc_ms(),
        received_at_ns=clock.monotonic_ns(),
    )


@dataclass(frozen=True, slots=True)
class SkewReading:
    lag_ms: int
    unreasonable: bool
    reason: str | None


class SkewMonitor:
    """Tracks receive-minus-source lag. A source ahead of us means the local clock is wrong."""

    def __init__(self, max_future_ms: int = 500, max_lag_ms: int = 5000) -> None:
        self.max_future_ms = max_future_ms
        self.max_lag_ms = max_lag_ms
        self.last: SkewReading | None = None
        self.unreasonable_count = 0

    def observe(self, source_ts_ms: int, received_utc_ms: int) -> SkewReading:
        lag = received_utc_ms - source_ts_ms
        reason: str | None = None
        if lag < -self.max_future_ms:
            reason = "source_timestamp_in_future"
        elif lag > self.max_lag_ms:
            reason = "source_timestamp_too_old"
        reading = SkewReading(lag, reason is not None, reason)
        if reason is not None:
            self.unreasonable_count += 1
        self.last = reading
        return reading


class ReorderDetector:
    """Flags per-key source timestamps that go backwards. Missing timestamps are never ordered."""

    def __init__(self) -> None:
        self._last: dict[str, int] = {}
        self.reordered = 0

    def observe(self, key: str, source_ts_ms: int | None) -> bool:
        if source_ts_ms is None:
            return False
        previous = self._last.get(key)
        if previous is not None and source_ts_ms < previous:
            self.reordered += 1
            return True
        self._last[key] = source_ts_ms
        return False
