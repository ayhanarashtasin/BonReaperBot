"""Market discovery through the official SDK's typed event pagination.

The 5-minute window comes from ``Event.schedule.start_time``. ``Market.state.start_date`` is the
listing time (about 24h earlier), and slug epochs are not relied on. See ADR-0003.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from polymarket import Event, Market, ProtocolVersion

from ..types import FeeCollection, FeeParams, MarketSpec, MarketStatus, MarketVersion
from .rules import Instrument, check_rules, rules_hash


class MarketRejectedError(ValueError):
    def __init__(self, slug: str, reasons: list[str]) -> None:
        super().__init__(f"{slug}: {', '.join(reasons)}")
        self.slug = slug
        self.reasons = reasons


@dataclass(frozen=True, slots=True)
class Rejection:
    slug: str
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    accepted: tuple[MarketSpec, ...]
    rejected: tuple[Rejection, ...]


class EventSource(Protocol):
    async def series_id(self, slug: str) -> int: ...
    async def open_events(self, series_id: int, now_utc_ms: int, limit: int) -> list[Event]: ...


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _asset_ids(market: Market, version: MarketVersion) -> tuple[str | None, str | None]:
    """Up maps to YES whatever order the venue lists the outcomes in. Never guesses an ID."""
    outcomes = {
        market.outcomes.yes.label.strip().lower(): market.outcomes.yes,
        market.outcomes.no.label.strip().lower(): market.outcomes.no,
    }
    up = outcomes.get("up")
    down = outcomes.get("down")
    if up is None or down is None:
        return None, None
    if version is MarketVersion.V2:
        return up.position_id, down.position_id
    return up.token_id, down.token_id


def fee_params_from_market(market: Market, collection: FeeCollection) -> FeeParams | None:
    trading = market.trading
    if trading.fees_enabled is None:
        return None
    if trading.fees_enabled is False:
        return FeeParams(
            enabled=False,
            rate=Decimal(0),
            exponent=Decimal(1),
            taker_only=True,
            rebate_rate=Decimal(0),
            fee_type=trading.fee_type or "none",
            collection=collection,
        )
    schedule = trading.fee_schedule
    if schedule is None:
        return None
    return FeeParams(
        enabled=True,
        rate=schedule.rate,
        exponent=Decimal(str(schedule.exponent)),
        taker_only=schedule.taker_only,
        rebate_rate=schedule.rebate_rate,
        fee_type=trading.fee_type or "unknown",
        collection=collection,
    )


def market_spec_from_event(
    event: Event,
    instrument: Instrument,
    now_utc_ms: int,
    collection: FeeCollection = FeeCollection.SHARES_ON_BUY,
) -> MarketSpec:
    """Validate one event into a MarketSpec or raise :class:`MarketRejectedError`."""
    slug = event.slug or event.id
    reasons: list[str] = []
    if len(event.markets) != 1:
        raise MarketRejectedError(slug, [f"expected_one_market_got_{len(event.markets)}"])
    market = event.markets[0]
    if instrument.series_slug not in {s.slug for s in event.series}:
        raise MarketRejectedError(slug, ["series_mismatch"])

    if market.version is None:
        raise MarketRejectedError(slug, ["unknown_market_version"])
    version = MarketVersion.V2 if market.version is ProtocolVersion.V2 else MarketVersion.CTF
    if market.state.neg_risk is not False:
        reasons.append("unsupported_neg_risk_or_unknown")
    if not market.condition_id:
        reasons.append("condition_id_missing")

    yes_id, no_id = _asset_ids(market, version)
    if yes_id is None or no_id is None:
        reasons.append("outcome_asset_ids_unavailable")

    start = event.schedule.start_time
    end = event.schedule.end_date or market.state.end_date
    if start is None or end is None:
        reasons.append("window_times_missing")
    elif end <= start:
        reasons.append("window_inverted")
    elif (end - start).total_seconds() != instrument.duration_minutes * 60:
        reasons.append("duration_mismatch")

    description = market.description or event.description
    reasons.extend(check_rules(description, market.resolution.source, instrument))

    tick = market.trading.minimum_tick_size
    min_size = market.trading.minimum_order_size
    if tick is None or tick <= 0:
        reasons.append("tick_size_missing")
    if min_size is None or min_size <= 0:
        reasons.append("min_order_size_missing")
    fee = fee_params_from_market(market, collection)
    if fee is None:
        reasons.append("fee_config_unknown")

    if reasons:
        raise MarketRejectedError(slug, reasons)
    # Narrowing for the type checker; every None was rejected above.
    if (
        start is None
        or end is None
        or description is None
        or yes_id is None
        or no_id is None
        or market.condition_id is None
        or tick is None
        or min_size is None
        or fee is None
    ):
        raise MarketRejectedError(slug, ["internal_validation_gap"])

    start_ms, end_ms = _ms(start), _ms(end)
    if market.state.closed:
        status = MarketStatus.CLOSED
    elif now_utc_ms < start_ms:
        status = MarketStatus.UPCOMING
    else:
        status = MarketStatus.ACTIVE
    return MarketSpec(
        market_id=market.id,
        condition_id=market.condition_id,
        event_id=event.id,
        slug=market.slug or slug,
        symbol=instrument.symbol,
        duration_minutes=instrument.duration_minutes,
        start_utc_ms=start_ms,
        end_utc_ms=end_ms,
        yes_label="Up",
        no_label="Down",
        yes_asset_id=yes_id,
        no_asset_id=no_id,
        version=version,
        tick_size=tick,
        min_order_shares=min_size,
        fee=fee,
        rules_text=description,
        rules_hash=rules_hash(description),
        oracle_source=instrument.oracle_source,
        status=status,
        accepting_orders=bool(market.state.accepting_orders),
    )


def filter_events(
    events: list[Event],
    instrument: Instrument,
    now_utc_ms: int,
    collection: FeeCollection = FeeCollection.SHARES_ON_BUY,
) -> DiscoveryResult:
    accepted: list[MarketSpec] = []
    rejected: list[Rejection] = []
    for event in events:
        try:
            spec = market_spec_from_event(event, instrument, now_utc_ms, collection)
        except MarketRejectedError as exc:
            rejected.append(Rejection(exc.slug, tuple(exc.reasons)))
            continue
        if spec.end_utc_ms <= now_utc_ms or spec.status is MarketStatus.CLOSED:
            continue
        if not spec.accepting_orders:
            rejected.append(Rejection(spec.slug, ("not_accepting_orders",)))
            continue
        accepted.append(spec)
    accepted.sort(key=lambda s: s.start_utc_ms)
    return DiscoveryResult(tuple(accepted), tuple(rejected))


async def discover(
    source: EventSource,
    instrument: Instrument,
    now_utc_ms: int,
    limit: int = 6,
    collection: FeeCollection = FeeCollection.SHARES_ON_BUY,
) -> DiscoveryResult:
    series_id = await source.series_id(instrument.series_slug)
    events = await source.open_events(series_id, now_utc_ms, limit)
    return filter_events(events, instrument, now_utc_ms, collection)
