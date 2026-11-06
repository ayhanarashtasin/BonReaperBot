"""Immutable domain contracts shared by replay, paper, shadow and (future) live adapters.

Money, prices and shares are always ``Decimal``. Asset IDs are always strings. Timestamps are
integer UTC milliseconds unless the field name says otherwise.
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import types as pytypes
import typing
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, TypeVar

SCHEMA_VERSION = 1
SETTLEMENT_ASSET = "pUSD"
ONE = Decimal(1)
ZERO = Decimal(0)

T = TypeVar("T")


class Mode(enum.StrEnum):
    READ_ONLY = "read_only"
    PAPER = "paper"
    SHADOW = "shadow"
    LIVE = "live"


class Outcome(enum.StrEnum):
    """Canonical outcome. YES is the outcome resolving true for 'Up'."""

    YES = "YES"
    NO = "NO"

    @property
    def other(self) -> Outcome:
        return Outcome.NO if self is Outcome.YES else Outcome.YES


class Side(enum.StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class MarketVersion(enum.StrEnum):
    CTF = "v1"
    V2 = "v2"


class MarketStatus(enum.StrEnum):
    ACTIVE = "active"
    UPCOMING = "upcoming"
    CLOSED = "closed"
    RESOLVED = "resolved"


class BookValidity(enum.StrEnum):
    UNINITIALIZED = "uninitialized"
    READY = "ready"
    SYNCING = "syncing"  # Venue-reported top disagrees in a way we cannot repair; not yet a gap.
    INVALID = "invalid"


class LiquidityRole(enum.StrEnum):
    MAKER = "maker"
    TAKER = "taker"


class FillStatus(enum.StrEnum):
    MATCHED = "matched"
    CONFIRMED = "confirmed"
    FAILED = "failed"


class OrderState(enum.StrEnum):
    INTENT = "INTENT"
    RISK_REJECTED = "RISK_REJECTED"
    PREPARED = "PREPARED"
    SUBMITTING = "SUBMITTING"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"


TERMINAL_ORDER_STATES = frozenset(
    {
        OrderState.RISK_REJECTED,
        OrderState.FILLED,
        OrderState.REJECTED,
        OrderState.CANCELED,
        OrderState.EXPIRED,
    }
)


class OrderType(enum.StrEnum):
    GTC = "GTC"
    FOK = "FOK"
    FAK = "FAK"


class IntentPurpose(enum.StrEnum):
    """Why an order exists. Keeps risk-reducing hedges apart from profit-seeking entries."""

    PASSIVE_QUOTE = "passive_quote"
    PAIR_ENTRY = "pair_entry"
    PROFITABLE_HEDGE = "profitable_hedge"
    RISK_REDUCING_HEDGE = "risk_reducing_hedge"
    UNWIND = "unwind"


class FeeCollection(enum.StrEnum):
    """How a taker BUY fee is collected.

    See ADR-0002: the docs state the fee in USDC and the SDK sizes market buys with the fee on
    top, while the PRD requires modelling fewer shares received. SHARES_ON_BUY is the
    conservative default because it never yields a better net cost than CASH_ON_TOP.
    """

    SHARES_ON_BUY = "shares_on_buy"
    CASH_ON_TOP = "cash_on_top"


class ReferenceSource(enum.StrEnum):
    CHAINLINK_TWAP = "chainlink_twap"
    BINANCE_SPOT = "binance_spot"
    BINANCE_FUTURES = "binance_futures"
    BINANCE_MARK = "binance_mark"


@dataclass(frozen=True, slots=True)
class CurrencyAmount:
    asset: str
    amount: Decimal

    def __add__(self, other: CurrencyAmount) -> CurrencyAmount:
        if self.asset != other.asset:
            raise ValueError(f"cannot add {self.asset} to {other.asset}")
        return CurrencyAmount(self.asset, self.amount + other.amount)


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    event_id: str
    schema_version: int
    source: str
    topic: str
    market_id: str | None
    asset_id: str | None
    source_ts_ms: int | None
    received_at_ns: int
    received_utc_ms: int
    payload_json: bytes
    segment_id: str = ""
    seq: int = 0

    def payload(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.payload_json)
        return loaded


@dataclass(frozen=True, slots=True)
class FeeParams:
    enabled: bool
    rate: Decimal
    exponent: Decimal
    taker_only: bool
    rebate_rate: Decimal
    fee_type: str
    collection: FeeCollection = FeeCollection.SHARES_ON_BUY

    def __post_init__(self) -> None:
        if self.rate < 0 or self.rebate_rate < 0:
            raise ValueError("fee rates must be non-negative")

    @property
    def version(self) -> str:
        return (
            f"{self.fee_type}:rate={self.rate}:exp={self.exponent}:"
            f"taker_only={self.taker_only}:{self.collection.value}"
        )


@dataclass(frozen=True, slots=True)
class MarketSpec:
    market_id: str
    condition_id: str
    event_id: str
    slug: str
    symbol: str
    duration_minutes: int
    start_utc_ms: int
    end_utc_ms: int
    yes_label: str
    no_label: str
    yes_asset_id: str
    no_asset_id: str
    version: MarketVersion
    tick_size: Decimal
    min_order_shares: Decimal
    fee: FeeParams
    rules_text: str
    rules_hash: str
    oracle_source: str
    status: MarketStatus
    accepting_orders: bool = True

    def __post_init__(self) -> None:
        if self.start_utc_ms >= self.end_utc_ms:
            raise ValueError("MarketSpec requires start < end")
        if self.yes_asset_id == self.no_asset_id:
            raise ValueError("MarketSpec requires distinct complementary outcome assets")
        if not isinstance(self.yes_asset_id, str) or not isinstance(self.no_asset_id, str):
            raise TypeError("asset IDs must be strings")
        if self.tick_size <= 0 or self.min_order_shares <= 0:
            raise ValueError("tick and minimum size must be positive")

    def asset_id(self, outcome: Outcome) -> str:
        return self.yes_asset_id if outcome is Outcome.YES else self.no_asset_id

    def outcome_of(self, asset_id: str) -> Outcome:
        if asset_id == self.yes_asset_id:
            return Outcome.YES
        if asset_id == self.no_asset_id:
            return Outcome.NO
        raise KeyError(f"asset {asset_id} is not part of market {self.market_id}")


@dataclass(frozen=True, slots=True)
class PriceLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True, slots=True)
class BookState:
    """Best level first on both sides: bids descending, asks ascending."""

    market_id: str
    asset_id: str
    bids: tuple[PriceLevel, ...]
    asks: tuple[PriceLevel, ...]
    tick_size: Decimal
    last_snapshot_utc_ms: int | None
    last_update_utc_ms: int | None
    validity: BookValidity
    book_hash: str | None = None
    invalid_reason: str | None = None

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None


@dataclass(frozen=True, slots=True)
class BinanceReference:
    """A Binance observation. Its source enum can never be CHAINLINK_TWAP."""

    instrument: str
    source: ReferenceSource
    price: Decimal
    exchange_ts_ms: int
    received_utc_ms: int

    def __post_init__(self) -> None:
        if self.source is ReferenceSource.CHAINLINK_TWAP:
            raise ValueError("Binance data must never be labelled as Chainlink")


@dataclass(frozen=True, slots=True)
class ReferenceState:
    symbol: str
    twap: Decimal | None
    window_seconds: int
    source: ReferenceSource
    feed: str
    source_ts_ms: int | None
    received_utc_ms: int | None
    start_strike: Decimal | None = None
    strike_provenance: str | None = None
    binance_refs: tuple[BinanceReference, ...] = ()

    def __post_init__(self) -> None:
        if self.twap is not None and self.source is not ReferenceSource.CHAINLINK_TWAP:
            raise ValueError("twap may only carry a Chainlink TWAP observation")


@dataclass(frozen=True, slots=True)
class DepthFill:
    price: Decimal
    gross_shares: Decimal


@dataclass(frozen=True, slots=True)
class FeeBreakdown:
    fee_cash: Decimal
    fee_shares: Decimal
    fee_usd_equivalent: Decimal


@dataclass(frozen=True, slots=True)
class LegPlan:
    outcome: Outcome
    asset_id: str
    levels: tuple[DepthFill, ...]
    gross_shares: Decimal
    net_shares: Decimal
    notional: Decimal
    fee: FeeBreakdown
    cash_spent: Decimal
    vwap: Decimal
    worst_price: Decimal


@dataclass(frozen=True, slots=True)
class AdverseCase:
    name: str
    edge: Decimal
    funding_required: Decimal
    note: str


@dataclass(frozen=True, slots=True)
class ArbCandidate:
    candidate_id: str
    market_id: str
    ts_utc_ms: int
    q_net: Decimal
    yes_leg: LegPlan
    no_leg: LegPlan
    capital: Decimal
    gross_edge: Decimal
    fee_usd_equivalent: Decimal
    indicative_edge: Decimal
    edge_after_reserve: Decimal
    roi: Decimal
    seconds_to_expiry: Decimal
    yes_book_age_ms: int
    no_book_age_ms: int
    passed: bool
    reasons: tuple[str, ...]
    fee_version: str
    adverse_cases: tuple[AdverseCase, ...] = ()
    classification: str = "candidate_executable"


@dataclass(frozen=True, slots=True)
class QuoteIntent:
    intent_id: str
    market_id: str
    asset_id: str
    outcome: Outcome
    side: Side
    price: Decimal
    size: Decimal
    post_only: bool
    order_type: OrderType
    expiry_cutoff_ms: int
    strategy_id: str
    purpose: IntentPurpose = IntentPurpose.PASSIVE_QUOTE
    risk_id: str | None = None
    created_utc_ms: int = 0

    def __post_init__(self) -> None:
        if not (ZERO < self.price < ONE):
            raise ValueError(f"price {self.price} outside (0, 1)")
        if self.size <= 0:
            raise ValueError("size must be positive")
        if self.post_only and self.order_type is not OrderType.GTC:
            raise ValueError("post-only orders must rest as GTC")

    @property
    def notional(self) -> Decimal:
        return self.price * self.size


@dataclass(frozen=True, slots=True)
class OrderRecord:
    intent: QuoteIntent
    state: OrderState
    exchange_order_id: str | None = None
    pending_cancel: bool = False
    matched_gross: Decimal = ZERO
    created_utc_ms: int = 0
    updated_utc_ms: int = 0

    @property
    def remaining(self) -> Decimal:
        return self.intent.size - self.matched_gross


@dataclass(frozen=True, slots=True)
class FillRecord:
    trade_id: str
    leg_key: str
    intent_id: str | None
    exchange_order_id: str | None
    market_id: str
    asset_id: str
    outcome: Outcome
    side: Side
    role: LiquidityRole
    price: Decimal
    gross_shares: Decimal
    net_shares: Decimal
    fee_cash: Decimal
    fee_shares: Decimal
    cash_delta: Decimal
    status: FillStatus
    simulated: bool
    ts_utc_ms: int
    fee_asset: str = SETTLEMENT_ASSET

    @property
    def identity(self) -> str:
        return f"{self.trade_id}#{self.leg_key}"


@dataclass(frozen=True, slots=True)
class InventorySnapshot:
    market_id: str
    cash_available: Decimal
    cash_reserved: Decimal
    yes_gross: Decimal
    yes_net: Decimal
    no_gross: Decimal
    no_net: Decimal
    paired: Decimal
    unpaired_yes: Decimal
    unpaired_no: Decimal
    cost_basis: Decimal
    mark_to_market: Decimal | None = None

    def __post_init__(self) -> None:
        if self.paired != min(self.yes_net, self.no_net):
            raise ValueError("paired must equal min(net_yes, net_no)")

    @property
    def locked_payout(self) -> Decimal:
        """Only matched net shares carry a locked payout."""
        return self.paired * ONE


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    entry_id: str
    cause_id: str
    account: str
    asset: str
    debit: Decimal
    credit: Decimal
    ts_utc_ms: int
    settlement_status: str = "n/a"
    reversal_of: str | None = None


@dataclass(frozen=True, slots=True)
class RiskDecision:
    request_id: str
    allowed: bool
    reasons: tuple[str, ...]
    limit_snapshot: tuple[tuple[str, str], ...]
    ts_utc_ms: int


@dataclass(frozen=True, slots=True)
class InputFile:
    path: str
    sha256: str
    rows: int = 0


@dataclass(frozen=True, slots=True)
class ReplayManifest:
    run_id: str
    inputs: tuple[InputFile, ...]
    coverage: tuple[tuple[str, str], ...]
    code_commit: str
    config_hash: str
    model_hash: str | None
    period_start_utc_ms: int | None
    period_end_utc_ms: int | None
    fee_versions: tuple[str, ...]
    seed: int
    sdk_version: str
    assumptions: tuple[str, ...] = field(default_factory=tuple)
    known_gaps: tuple[str, ...] = field(default_factory=tuple)


# --- Serialisation -----------------------------------------------------------------------------


def to_jsonable(value: Any) -> Any:
    """Convert domain objects to JSON-safe values without ever passing through float."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [to_jsonable(v) for v in value]
    if isinstance(value, float):
        raise TypeError("binary float refused in domain serialisation")
    return value


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        to_jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _coerce(hint: Any, value: Any) -> Any:
    origin = typing.get_origin(hint)
    if origin is typing.Union or origin is pytypes.UnionType:
        args = typing.get_args(hint)
        if value is None:
            return None
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) != 1:
            raise TypeError(f"unsupported union {hint}")
        return _coerce(non_none[0], value)
    if origin is tuple:
        args = typing.get_args(hint)
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_coerce(args[0], v) for v in value)
        return tuple(_coerce(a, v) for a, v in zip(args, value, strict=True))
    if hint is Decimal:
        return Decimal(str(value))
    if hint is bytes:
        return value.encode("utf-8")
    if isinstance(hint, type) and issubclass(hint, enum.Enum):
        return hint(value)
    if dataclasses.is_dataclass(hint) and isinstance(hint, type):
        return from_jsonable(hint, value)
    return value


def from_jsonable(cls: type[T], data: Mapping[str, Any]) -> T:
    """Rebuild a frozen dataclass from :func:`to_jsonable` output."""
    if not dataclasses.is_dataclass(cls):
        raise TypeError(f"{cls} is not a dataclass")
    hints = typing.get_type_hints(cls)
    kwargs = {
        f.name: _coerce(hints[f.name], data[f.name])
        for f in dataclasses.fields(cls)
        if f.name in data
    }
    return cls(**kwargs)
