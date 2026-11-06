"""Reproducibility manifest and report writers (FR-20)."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path
from typing import Any

import polymarket

from ..settings import Settings
from ..storage.raw_events import load_manifest
from ..types import InputFile, ReplayManifest, sha256_hex, to_jsonable

ASSUMPTIONS = (
    "Queue position is estimated, not observed: orders join behind visible size at their price.",
    "Unexplained size removal is bracketed by scenario; the feed cannot attribute cancels.",
    "Trade-through of a resting price is treated as a full fill of the order.",
    "Latency is a configured model with seeded jitter, not a measurement of this bot's real path.",
    "The bot's own simulated orders do not change other participants' behaviour.",
    "Maker rebates are excluded. Fees use the market's published schedule, rounded to 5 decimals.",
    "ADR-0002: BUY fees are modelled in shares received, the conservative treatment.",
    "Outcomes come only from official venue resolution fields recorded after close.",
)


def git_commit() -> str:
    git = shutil.which("git")
    if git is None:
        return "git_unavailable"
    try:
        head = subprocess.run(  # noqa: S603 - fixed arguments, no shell
            [git, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, check=True
        ).stdout.strip()
        dirty = subprocess.run(  # noqa: S603 - fixed arguments, no shell
            [git, "status", "--porcelain", "--", "src", "config"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "not_a_git_checkout"
    return f"{head}+dirty" if dirty else head


def build_manifest(
    run_id: str,
    input_dir: Path,
    settings: Settings,
    seed: int,
    *,
    coverage: Iterable[tuple[str, str]] = (),
    period: tuple[int | None, int | None] = (None, None),
    fee_versions: Iterable[str] = (),
    model_hash: str | None = None,
    known_gaps: Iterable[str] = (),
) -> ReplayManifest:
    records = load_manifest(input_dir)
    return ReplayManifest(
        run_id=run_id,
        inputs=tuple(InputFile(r.path, r.sha256, r.rows) for r in records),
        coverage=tuple(coverage),
        code_commit=git_commit(),
        config_hash=settings.config_hash(),
        model_hash=model_hash,
        period_start_utc_ms=period[0],
        period_end_utc_ms=period[1],
        fee_versions=tuple(sorted(set(fee_versions))),
        seed=seed,
        sdk_version=polymarket.__version__,
        assumptions=ASSUMPTIONS,
        known_gaps=tuple(known_gaps),
    )


def manifest_input_hash(manifest: ReplayManifest) -> str:
    joined = "".join(f"{i.path}:{i.sha256};" for i in manifest.inputs)
    return sha256_hex(joined.encode())


def write_json(path: Path, doc: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_jsonable(doc), indent=1, sort_keys=True) + "\n", encoding="utf-8")


def money(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, str):
        value = Decimal(value)
    return f"{value:.4f}"
