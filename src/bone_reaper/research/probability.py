"""Fair-value research: causal features, a transparent baseline, and a small supervised model.

Probabilities are research values, so plain floats are acceptable here; nothing in this module
touches money. A model only reaches quotes through ``fair_value.model_fair``, behind an explicit
flag and bounded sanitising. A better forecast is not a profitable strategy, and nothing here says
that it is.
"""

from __future__ import annotations

import itertools
import json
import math
import statistics
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from ..types import sha256_hex

FEATURE_NAMES = ("z_score", "distance_bps", "momentum_bps", "tau_fraction")
LOG_EPS = 1e-6
MIN_SIGMA = 1e-6


class CausalityError(ValueError):
    """A feature would use data from at or after the prediction time."""


@dataclass(frozen=True, slots=True)
class PricePoint:
    ts_ms: int
    price: float


@dataclass(frozen=True, slots=True)
class FeatureRow:
    market_slug: str
    market_end_ms: int
    decision_ms: int
    as_of_ms: int
    seconds_left: float
    z_score: float
    distance_bps: float
    momentum_bps: float
    tau_fraction: float
    label_up: int | None
    source: str

    def vector(self) -> list[float]:
        return [self.z_score, self.distance_bps, self.momentum_bps, self.tau_fraction]


def realized_sigma_per_sqrt_second(points: Sequence[PricePoint]) -> float | None:
    """Volatility of log returns, per square-root second, from strictly past observations."""
    if len(points) < 3:
        return None
    scaled = []
    for earlier, later in itertools.pairwise(points):
        seconds = (later.ts_ms - earlier.ts_ms) / 1000
        if seconds <= 0 or earlier.price <= 0 or later.price <= 0:
            continue
        scaled.append(math.log(later.price / earlier.price) / math.sqrt(seconds))
    if len(scaled) < 2:
        return None
    return max(statistics.pstdev(scaled), MIN_SIGMA)


def build_features(
    *,
    market_slug: str,
    market_end_ms: int,
    window_seconds: int,
    decision_ms: int,
    strike: float,
    history: Sequence[PricePoint],
    label_up: int | None,
    source: str,
    lookback_ms: int = 900_000,
) -> FeatureRow | None:
    """Features at ``decision_ms``. Refuses any observation at or after that moment.

    ``strike`` is fixed at the window start, so using it later in the window is not lookahead.
    Live it is the observed TWAP at the window start. The official figure appears only after close.
    """
    if decision_ms >= market_end_ms:
        raise CausalityError("decision time must be before the market closes")
    window = [p for p in history if decision_ms - lookback_ms <= p.ts_ms]
    if any(p.ts_ms > decision_ms for p in window):
        raise CausalityError("history contains observations after the decision time")
    if not window or strike <= 0:
        return None
    sigma = realized_sigma_per_sqrt_second(window)
    if sigma is None:
        return None
    latest = window[-1]
    seconds_left = (market_end_ms - decision_ms) / 1000
    distance = math.log(latest.price / strike)
    prior = [p for p in window if p.ts_ms <= latest.ts_ms - 60_000]
    momentum = math.log(latest.price / prior[-1].price) if prior else 0.0
    return FeatureRow(
        market_slug=market_slug,
        market_end_ms=market_end_ms,
        decision_ms=decision_ms,
        as_of_ms=latest.ts_ms,
        seconds_left=seconds_left,
        z_score=distance / (sigma * math.sqrt(seconds_left)),
        distance_bps=distance * 10_000,
        momentum_bps=momentum * 10_000,
        tau_fraction=seconds_left / window_seconds,
        label_up=label_up,
        source=source,
    )


def normal_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2))


def baseline_probability(row: FeatureRow) -> float:
    """Driftless random walk: P(final >= strike) given current distance and volatility."""
    return normal_cdf(row.z_score)


def _sigmoid(x: float) -> float:
    if x < -35:
        return 1e-15
    if x > 35:
        return 1 - 1e-15
    return 1 / (1 + math.exp(-x))


@dataclass(frozen=True, slots=True)
class LogisticModel:
    feature_names: tuple[str, ...]
    weights: tuple[float, ...]
    intercept: float
    means: tuple[float, ...]
    scales: tuple[float, ...]
    train_rows: int
    trained_through_ms: int

    def predict(self, row: FeatureRow) -> float:
        total = self.intercept
        for value, weight, mean, scale in zip(
            row.vector(), self.weights, self.means, self.scales, strict=True
        ):
            total += weight * (value - mean) / scale
        return _sigmoid(total)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @property
    def model_hash(self) -> str:
        return sha256_hex(self.to_json().encode())

    @classmethod
    def from_json(cls, text: str) -> LogisticModel:
        doc = json.loads(text)
        return cls(
            feature_names=tuple(doc["feature_names"]),
            weights=tuple(doc["weights"]),
            intercept=doc["intercept"],
            means=tuple(doc["means"]),
            scales=tuple(doc["scales"]),
            train_rows=doc["train_rows"],
            trained_through_ms=doc["trained_through_ms"],
        )


def fit_logistic(
    rows: Sequence[FeatureRow], l2: float = 1.0, iterations: int = 400, learning_rate: float = 0.3
) -> LogisticModel:
    """Deterministic full-batch gradient descent, with no randomness and no dependencies."""
    labelled = [r for r in rows if r.label_up is not None]
    if not labelled:
        raise ValueError("no labelled rows")
    width = len(FEATURE_NAMES)
    columns = list(zip(*(r.vector() for r in labelled), strict=True))
    means = [statistics.fmean(c) for c in columns]
    scales = [statistics.pstdev(c) or 1.0 for c in columns]
    standardized = [
        [(v - m) / s for v, m, s in zip(r.vector(), means, scales, strict=True)] for r in labelled
    ]
    labels = [float(r.label_up or 0) for r in labelled]
    weights = [0.0] * width
    intercept = 0.0
    count = len(labelled)
    for _ in range(iterations):
        gradient = [0.0] * width
        gradient_intercept = 0.0
        for x, y in zip(standardized, labels, strict=True):
            error = _sigmoid(intercept + sum(w * v for w, v in zip(weights, x, strict=True))) - y
            gradient_intercept += error
            for index in range(width):
                gradient[index] += error * x[index]
        intercept -= learning_rate * gradient_intercept / count
        for index in range(width):
            weights[index] -= learning_rate * (
                gradient[index] / count + l2 * weights[index] / count
            )
    return LogisticModel(
        feature_names=FEATURE_NAMES,
        weights=tuple(weights),
        intercept=intercept,
        means=tuple(means),
        scales=tuple(scales),
        train_rows=count,
        trained_through_ms=max(r.market_end_ms for r in labelled),
    )


def brier_score(pairs: Iterable[tuple[float, int]]) -> float:
    items = list(pairs)
    return sum((p - y) ** 2 for p, y in items) / len(items)


def log_loss(pairs: Iterable[tuple[float, int]]) -> float:
    items = list(pairs)
    total = 0.0
    for p, y in items:
        clipped = min(max(p, LOG_EPS), 1 - LOG_EPS)
        total -= y * math.log(clipped) + (1 - y) * math.log(1 - clipped)
    return total / len(items)


def reliability_table(
    pairs: Iterable[tuple[float, int]], buckets: int = 10
) -> list[dict[str, float | int]]:
    grouped: list[list[tuple[float, int]]] = [[] for _ in range(buckets)]
    for p, y in pairs:
        grouped[min(int(p * buckets), buckets - 1)].append((p, y))
    table: list[dict[str, float | int]] = []
    for index, items in enumerate(grouped):
        if items:
            table.append(
                {
                    "bucket_low": index / buckets,
                    "bucket_high": (index + 1) / buckets,
                    "count": len(items),
                    "mean_predicted": statistics.fmean(p for p, _ in items),
                    "observed_up_rate": statistics.fmean(y for _, y in items),
                }
            )
    return table


def expected_calibration_error(pairs: Sequence[tuple[float, int]], buckets: int = 10) -> float:
    total = len(pairs)
    return sum(
        int(row["count"])
        / total
        * abs(float(row["mean_predicted"]) - float(row["observed_up_rate"]))
        for row in reliability_table(pairs, buckets)
    )


@dataclass(frozen=True, slots=True)
class MarketSplit:
    train: tuple[str, ...]
    validation: tuple[str, ...]
    test: tuple[str, ...]


def chronological_market_split(
    rows: Sequence[FeatureRow],
    embargo_markets: int = 2,
    train: float = 0.6,
    validation: float = 0.2,
) -> MarketSplit:
    """Split whole markets by time, never rows, with embargoed markets between partitions."""
    ends: dict[str, int] = {}
    for row in rows:
        ends[row.market_slug] = row.market_end_ms
    ordered = [slug for slug, _ in sorted(ends.items(), key=lambda kv: (kv[1], kv[0]))]
    train_end = int(len(ordered) * train)
    validation_end = int(len(ordered) * (train + validation))
    return MarketSplit(
        train=tuple(ordered[:train_end]),
        validation=tuple(ordered[train_end + embargo_markets : validation_end]),
        test=tuple(ordered[validation_end + embargo_markets :]),
    )


def assert_no_leakage(split: MarketSplit, rows: Sequence[FeatureRow]) -> None:
    """Raises if any market is in two partitions or a later partition precedes an earlier one."""
    train, validation, test = set(split.train), set(split.validation), set(split.test)
    if train & validation or train & test or validation & test:
        raise CausalityError("market appears in more than one partition")
    ends = {r.market_slug: r.market_end_ms for r in rows}
    groups = [split.train, split.validation, split.test]
    for earlier, later in itertools.pairwise(groups):
        if earlier and later and max(ends[m] for m in earlier) >= min(ends[m] for m in later):
            raise CausalityError("partitions are not in chronological order")
    for row in rows:
        if row.as_of_ms > row.decision_ms or row.decision_ms >= row.market_end_ms:
            raise CausalityError(f"non-causal feature row in {row.market_slug}")


def evaluate(
    rows: Sequence[FeatureRow], predict: Callable[[FeatureRow], float], label: str
) -> dict[str, object]:
    pairs = [(predict(r), int(r.label_up)) for r in rows if r.label_up is not None]
    if not pairs:
        return {"model": label, "rows": 0}
    return {
        "model": label,
        "rows": len(pairs),
        "markets": len({r.market_slug for r in rows}),
        "brier": brier_score(pairs),
        "log_loss": log_loss(pairs),
        "ece": expected_calibration_error(pairs),
        "reliability": reliability_table(pairs),
    }


def reliability_svg(table: Sequence[dict[str, float | int]], title: str) -> str:
    """A small self-contained reliability plot, with no plotting dependency."""
    size = 320
    margin = 40
    span = size - 2 * margin

    def x(value: float) -> float:
        return margin + value * span

    def y(value: float) -> float:
        return size - margin - value * span

    dots = "".join(
        f'<circle cx="{x(float(r["mean_predicted"])):.1f}" '
        f'cy="{y(float(r["observed_up_rate"])):.1f}" '
        f'r="{2 + min(8.0, math.sqrt(int(r["count"])) / 4):.1f}" fill="#2a6fdb"/>'
        for r in table
    )
    safe_title = title.replace("&", "&amp;").replace("<", "&lt;")
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}">'
        f'<rect width="{size}" height="{size}" fill="white"/>'
        f'<line x1="{x(0)}" y1="{y(0)}" x2="{x(1)}" y2="{y(1)}" stroke="#999"/>'
        f'<rect x="{margin}" y="{margin}" width="{span}" height="{span}" '
        'fill="none" stroke="#333"/>'
        f"{dots}"
        f'<text x="{margin}" y="24" font-size="12" font-family="sans-serif">{safe_title}</text>'
        f'<text x="{margin}" y="{size - 10}" font-size="11" font-family="sans-serif">'
        "mean predicted P(Up), against observed Up rate</text></svg>"
    )


def save_model(model: LogisticModel, path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(model.to_json(), encoding="utf-8")
    return model.model_hash


def load_model(path: Path) -> LogisticModel:
    return LogisticModel.from_json(path.read_text("utf-8"))
