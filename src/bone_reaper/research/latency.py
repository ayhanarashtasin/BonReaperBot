"""Deterministic, seeded latency model and percentile helpers."""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LatencyProfile:
    submit_ms: int = 150
    cancel_ms: int = 150
    ack_ms: int = 100
    visible_extra_ms: int = 0
    jitter_ms: int = 50

    def scaled(self, factor: int) -> LatencyProfile:
        return LatencyProfile(
            submit_ms=self.submit_ms * factor,
            cancel_ms=self.cancel_ms * factor,
            ack_ms=self.ack_ms * factor,
            visible_extra_ms=self.visible_extra_ms * factor,
            jitter_ms=self.jitter_ms * factor,
        )


class LatencyModel:
    def __init__(self, profile: LatencyProfile, seed: int) -> None:
        self.profile = profile
        self._rng = random.Random(seed)  # noqa: S311 - simulation determinism, not security
        self.samples: dict[str, list[int]] = {"submit": [], "cancel": [], "ack": []}

    def _draw(self, base: int, name: str) -> int:
        value = base + self._rng.randint(0, self.profile.jitter_ms)
        self.samples[name].append(value)
        return value

    def submit(self) -> int:
        return self._draw(self.profile.submit_ms, "submit")

    def cancel(self) -> int:
        return self._draw(self.profile.cancel_ms, "cancel")

    def ack(self) -> int:
        return self._draw(self.profile.ack_ms, "ack")


def percentiles(values: Sequence[int]) -> dict[str, int | None]:
    if not values:
        return {"p50": None, "p95": None, "p99": None, "count": 0}
    ordered = sorted(values)

    def pick(q: int) -> int:
        return ordered[min(len(ordered) - 1, (q * len(ordered)) // 100)]

    return {"p50": pick(50), "p95": pick(95), "p99": pick(99), "count": len(ordered)}
