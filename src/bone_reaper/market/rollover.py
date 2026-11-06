"""Market rollover: one active strategy market, a prepared next one, and retired tracking."""

from __future__ import annotations

import enum
from collections.abc import Iterable
from dataclasses import dataclass, field

from ..types import MarketSpec


class Lifecycle(enum.StrEnum):
    PREPARED = "prepared"
    ACTIVE = "active"
    RETIRED = "retired"


@dataclass(frozen=True, slots=True)
class RolloverEvent:
    kind: str
    market_id: str
    slug: str
    ts_utc_ms: int


@dataclass(slots=True)
class _Tracked:
    spec: MarketSpec
    lifecycle: Lifecycle


@dataclass(slots=True)
class RolloverController:
    """Retired markets stay tracked for settlement but can never be re-activated."""

    quote_cutoff_seconds: int = 20
    prepare_ahead: int = 1
    _markets: dict[str, _Tracked] = field(default_factory=dict)
    _asset_owner: dict[str, str] = field(default_factory=dict)
    events: list[RolloverEvent] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def update(self, specs: Iterable[MarketSpec], now_utc_ms: int) -> list[RolloverEvent]:
        new_events: list[RolloverEvent] = []
        for spec in sorted(specs, key=lambda s: s.start_utc_ms):
            if spec.market_id in self._markets:
                continue
            if not self._claim_assets(spec):
                continue
            self._markets[spec.market_id] = _Tracked(spec, Lifecycle.PREPARED)
            new_events.append(RolloverEvent("prepared", spec.market_id, spec.slug, now_utc_ms))

        for tracked in sorted(self._markets.values(), key=lambda t: t.spec.start_utc_ms):
            spec = tracked.spec
            if tracked.lifecycle is Lifecycle.RETIRED:
                continue
            if now_utc_ms >= spec.end_utc_ms:
                tracked.lifecycle = Lifecycle.RETIRED
                new_events.append(RolloverEvent("retired", spec.market_id, spec.slug, now_utc_ms))
            elif spec.start_utc_ms <= now_utc_ms and tracked.lifecycle is Lifecycle.PREPARED:
                tracked.lifecycle = Lifecycle.ACTIVE
                new_events.append(RolloverEvent("activated", spec.market_id, spec.slug, now_utc_ms))
        self.events.extend(new_events)
        return new_events

    def _claim_assets(self, spec: MarketSpec) -> bool:
        """Refuse any market reusing an asset of a different market (stale-asset protection)."""
        for asset in (spec.yes_asset_id, spec.no_asset_id):
            owner = self._asset_owner.get(asset)
            if owner is not None and owner != spec.market_id:
                self.failures.append(f"stale_asset_reuse:{spec.slug}:{asset}")
                return False
        self._asset_owner[spec.yes_asset_id] = spec.market_id
        self._asset_owner[spec.no_asset_id] = spec.market_id
        return True

    @property
    def active(self) -> MarketSpec | None:
        live = [t.spec for t in self._markets.values() if t.lifecycle is Lifecycle.ACTIVE]
        return max(live, key=lambda s: s.start_utc_ms) if live else None

    @property
    def prepared(self) -> list[MarketSpec]:
        waiting = [t.spec for t in self._markets.values() if t.lifecycle is Lifecycle.PREPARED]
        return sorted(waiting, key=lambda s: s.start_utc_ms)[: self.prepare_ahead]

    @property
    def retired(self) -> list[MarketSpec]:
        return [t.spec for t in self._markets.values() if t.lifecycle is Lifecycle.RETIRED]

    def subscription_assets(self) -> tuple[str, ...]:
        """Assets to stream: the active market plus the prepared next one."""
        specs = ([self.active] if self.active else []) + self.prepared
        return tuple(asset for s in specs for asset in (s.yes_asset_id, s.no_asset_id))

    def may_quote(self, market_id: str, now_utc_ms: int) -> bool:
        active = self.active
        if active is None or active.market_id != market_id:
            return False
        return now_utc_ms < active.end_utc_ms - self.quote_cutoff_seconds * 1000
