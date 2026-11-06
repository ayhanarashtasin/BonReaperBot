"""Strict YAML configuration. Unknown keys, binary floats and impossible limits are rejected."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, model_validator

from .types import FeeCollection, Mode, canonical_json, sha256_hex


class ConfigError(ValueError):
    """Raised for any invalid configuration. The CLI maps this to a nonzero exit."""


def _exact_decimal(value: Any) -> Any:
    if isinstance(value, float):
        raise ValueError(f"money/price value {value!r} must be a quoted string, not a YAML float")
    if isinstance(value, bool):
        raise ValueError("boolean is not a decimal")
    if isinstance(value, int | str):
        return Decimal(str(value))
    return value


ExactDecimal = Annotated[Decimal, BeforeValidator(_exact_decimal)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=False)


class MarketsConfig(_Strict):
    symbols: tuple[str, ...] = ("BTC",)
    durations_minutes: tuple[int, ...] = (5,)
    max_concurrent: int = Field(default=1, ge=1, le=4)


class StrategiesConfig(_Strict):
    scan_pair: bool = True
    passive_quotes: bool = False
    probability_model: bool = False
    min_net_edge_usd: ExactDecimal = Decimal("0.50")
    min_net_roi: ExactDecimal = Decimal("0.01")
    allow_taker_taker: bool = False
    allow_negative_edge_risk_hedge: bool = False
    max_hedge_loss_usd: ExactDecimal = Decimal("0")
    refresh_min_ticks: int = Field(default=2, ge=1)
    refresh_min_age_ms: int = Field(default=2000, ge=0)
    scan_sizes: tuple[ExactDecimal, ...] = (
        Decimal("5"),
        Decimal("10"),
        Decimal("25"),
        Decimal("50"),
        Decimal("100"),
    )
    execution_reserve_per_share: ExactDecimal = Decimal("0.002")
    quote_size_shares: ExactDecimal = Decimal("10")
    quote_margin: ExactDecimal = Decimal("0.01")
    pair_buffer: ExactDecimal = Decimal("0.02")
    min_quote_price: ExactDecimal = Decimal("0.05")
    max_quote_price: ExactDecimal = Decimal("0.95")
    model_path: str | None = None


class RiskConfig(_Strict):
    max_order_notional_usd: ExactDecimal = Decimal("10")
    max_unpaired_notional_usd: ExactDecimal = Decimal("20")
    max_total_gross_notional_usd: ExactDecimal = Decimal("100")
    max_daily_realized_loss_usd: ExactDecimal = Decimal("10")
    max_open_orders: int = Field(default=4, ge=1)
    stop_new_quotes_seconds_before_close: int = Field(default=20, ge=0)
    max_book_age_ms: int = Field(default=1000, ge=1)
    max_reference_age_ms: int = Field(default=2000, ge=1)
    max_user_stream_silence_ms: int = Field(default=5000, ge=1)
    max_unexplained_reconciliation_usd: ExactDecimal = Decimal("0.01")
    max_clock_skew_ms: int = Field(default=1000, ge=1)


class ExecutionConfig(_Strict):
    maker_post_only: bool = True
    prefer_order_type: Literal["GTC"] = "GTC"
    cancel_on_disconnect: bool = True
    retry_unknown_order_submit: Literal[False] = False
    reconcile_on_reconnect: bool = True
    fee_collection: FeeCollection = FeeCollection.SHARES_ON_BUY


class StorageConfig(_Strict):
    raw: Literal["parquet"] = "parquet"
    ledger: Literal["postgresql", "sqlite", "memory"] = "memory"
    raw_dir: str = "data/raw"
    journal_path: str = "data/journal"
    postgres_dsn_env: str = "BONE_REAPER_POSTGRES_DSN"
    flush_interval_seconds: int = Field(default=5, ge=1)
    checkpoint_interval_seconds: int = Field(default=30, ge=1)


class PaperConfig(_Strict):
    starting_cash_usd: ExactDecimal = Decimal("100")
    seed: int = 7
    queue_scenario: Literal["pessimistic", "central", "optimistic"] = "pessimistic"
    submit_latency_ms: int = Field(default=150, ge=0)
    cancel_latency_ms: int = Field(default=150, ge=0)
    ack_latency_ms: int = Field(default=100, ge=0)
    visible_latency_ms: int = Field(default=0, ge=0)
    latency_jitter_ms: int = Field(default=50, ge=0)
    resolution_delay_ms: int = Field(default=60000, ge=0)


class Settings(_Strict):
    mode: Mode = Mode.READ_ONLY
    live_enabled: bool = False
    live_operator_approval: bool = False
    markets: MarketsConfig = MarketsConfig()
    strategies: StrategiesConfig = StrategiesConfig()
    risk: RiskConfig = RiskConfig()
    execution: ExecutionConfig = ExecutionConfig()
    storage: StorageConfig = StorageConfig()
    paper: PaperConfig = PaperConfig()

    @model_validator(mode="after")
    def _validate(self) -> Settings:
        r = self.risk
        s = self.strategies
        problems: list[str] = []
        for name in (
            "max_order_notional_usd",
            "max_unpaired_notional_usd",
            "max_total_gross_notional_usd",
            "max_daily_realized_loss_usd",
        ):
            if getattr(r, name) <= 0:
                problems.append(f"risk.{name} must be positive")
        if r.max_order_notional_usd > r.max_total_gross_notional_usd:
            problems.append("risk.max_order_notional_usd exceeds max_total_gross_notional_usd")
        if r.max_unpaired_notional_usd > r.max_total_gross_notional_usd:
            problems.append("risk.max_unpaired_notional_usd exceeds max_total_gross_notional_usd")
        if r.max_unexplained_reconciliation_usd < 0:
            problems.append("risk.max_unexplained_reconciliation_usd must be non-negative")
        if s.min_net_edge_usd < 0:
            problems.append("strategies.min_net_edge_usd must be non-negative")
        if not Decimal(0) <= s.min_net_roi < Decimal(1):
            problems.append("strategies.min_net_roi must be within [0, 1)")
        if not s.scan_sizes or any(q <= 0 for q in s.scan_sizes):
            problems.append("strategies.scan_sizes must be positive")
        if s.quote_size_shares <= 0:
            problems.append("strategies.quote_size_shares must be positive")
        if not Decimal(0) < s.min_quote_price < s.max_quote_price < Decimal(1):
            problems.append("strategies quote price bounds must satisfy 0 < min < max < 1")
        if s.allow_negative_edge_risk_hedge and s.max_hedge_loss_usd <= 0:
            problems.append("allow_negative_edge_risk_hedge requires a positive max_hedge_loss_usd")
        if not s.allow_negative_edge_risk_hedge and s.max_hedge_loss_usd != 0:
            problems.append("max_hedge_loss_usd is set but allow_negative_edge_risk_hedge is false")
        if s.probability_model and not s.model_path:
            problems.append("probability_model requires strategies.model_path")
        if self.mode is Mode.READ_ONLY:
            if s.passive_quotes or s.allow_taker_taker:
                problems.append("read_only mode cannot enable passive_quotes or allow_taker_taker")
        if self.mode is not Mode.LIVE and (self.live_enabled or self.live_operator_approval):
            problems.append(f"live flags are incompatible with mode={self.mode.value}")
        if self.mode is Mode.LIVE and not (self.live_enabled and self.live_operator_approval):
            problems.append("mode=live requires live_enabled and live_operator_approval")
        if self.paper.starting_cash_usd <= 0:
            problems.append("paper.starting_cash_usd must be positive")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def config_hash(self) -> str:
        return sha256_hex(canonical_json(self.model_dump(mode="python")))


def load_settings(path: str | Path) -> Settings:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    return parse_settings(raw if raw is not None else {})


def parse_settings(raw: dict[str, Any]) -> Settings:
    try:
        return Settings.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration: {exc}") from exc


def allowed_network_actions(mode: Mode) -> dict[str, list[str]]:
    """Printed by every command so the operator can see what a mode may and may not do."""
    allowed = ["public market data (read)", "geoblock check (read)"]
    forbidden = ["order submission", "order cancellation on a real account", "wallet funding"]
    if mode in (Mode.PAPER, Mode.SHADOW):
        allowed.append("simulated order lifecycle (local only)")
    if mode is Mode.LIVE:
        forbidden.append("all live actions: real adapter is disabled (LIVE_DISABLED, BR-704)")
    return {"allowed": allowed, "forbidden": forbidden}
