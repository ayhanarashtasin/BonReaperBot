"""Gather what the dashboard shows. Every function here only reads files.

Reports are passed through as written, so a figure on screen is the figure in the report. A missing
or unreadable file becomes an explicit ``available: false``, never a zero that looks like a result.
"""

from __future__ import annotations

import json
import re
from collections import Counter, deque
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .. import __version__
from ..execution.real import live_gate_reasons
from ..settings import Settings
from ..telemetry.alerts import evaluate_alerts
from ..telemetry.health import _inputs_from
from ..time import SystemClock

MAX_EQUITY_POINTS = 400
SHADOW_RECENT_RECORDS = 50
MEMO_PATH = "reports/readiness_memo.md"
COMPARE_PATH = "reports/compare/compare.json"
SCAN_PATH = "reports/scan_real/scan.json"
FORECAST_PATH = "reports/model/forecast_report.json"
AUDIT_PATH = "reports/model/model_audit.json"
COVERAGE_PATH = "reports/coverage_real.json"
PERFORMANCE_PATH = "reports/performance.json"

_VERDICT = re.compile(r"\*\*Verdict:\s*(?P<word>[A-Z]+)\.?\s*(?P<detail>.*?)\*\*")
_DATE = re.compile(r"\*\*Date:\*\*\s*(?P<date>\S+)")
_TONES = {"PASS": "good", "FAIL": "critical", "INCONCLUSIVE": "warning"}
_NOT_A_REPORT = {"compare", "model", "scan_real"}


class ReportCache:
    """Re-read a file only when it changes, and keep the last good copy through a failed read.

    A status file can be mid-replace at the moment of a read, which should not blank the page.
    """

    def __init__(self) -> None:
        self._entries: dict[Path, tuple[tuple[int, int], Any]] = {}

    def load(self, path: Path) -> Any | None:
        try:
            stat = path.stat()
            key = (stat.st_mtime_ns, stat.st_size)
            cached = self._entries.get(path)
            if cached is not None and cached[0] == key:
                return cached[1]
            doc = json.loads(path.read_bytes())
        except FileNotFoundError:
            self._entries.pop(path, None)
            return None
        except (OSError, ValueError):
            cached = self._entries.get(path)
            return cached[1] if cached is not None else None
        self._entries[path] = (key, doc)
        return doc


def _section(root: Path, relative: str, doc: Any | None) -> dict[str, Any]:
    if doc is None:
        return {"available": False, "path": relative}
    try:
        modified = (root / relative).stat().st_mtime_ns // 1_000_000
    except OSError:
        modified = None
    return {"available": True, "path": relative, "modified_utc_ms": modified, "doc": doc}


def _clean_cell(text: str) -> str:
    return text.replace("**", "").replace("`", "").strip()


def parse_readiness_memo(text: str) -> dict[str, Any]:
    """Read the verdict from the memo itself, so the page cannot disagree with the document."""
    verdict = _VERDICT.search(text)
    date = _DATE.search(text)
    questions = []
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not line.startswith("|") or len(cells) != 2 or not cells[0].endswith("?"):
            continue
        answer = _clean_cell(cells[1])
        word, _, rest = answer.partition(":") if ":" in answer.split(" ")[0] else ("", "", "")
        if not word:
            word, _, rest = answer.partition(" ")
        word = word.rstrip(".:")
        if word.startswith("FAIL"):
            word = "FAIL"
        questions.append(
            {
                "question": _clean_cell(cells[0]),
                "status": word,
                "tone": _TONES.get(word, "neutral"),
                "detail": re.sub(r"^\((.+?)\):", r"\g<1>:", rest.strip(" :")),
            }
        )
    return {
        "verdict": verdict.group("word") if verdict else None,
        "tone": _TONES.get(verdict.group("word"), "neutral") if verdict else "neutral",
        "statement": verdict.group("detail").strip() if verdict else None,
        "date": date.group("date") if date else None,
        "questions": questions,
    }


def _recorders(root: Path, settings: Settings, cache: ReportCache) -> list[dict[str, Any]]:
    candidates = [*root.glob("data/*/health.json"), *root.glob("reports/*/raw/health.json")]
    found: list[dict[str, Any]] = []
    for path in candidates:
        doc = cache.load(path)
        if not isinstance(doc, dict):
            continue
        found.append(
            {
                "name": path.parent.relative_to(root).as_posix(),
                "health": doc,
                "alerts": [asdict(a) for a in evaluate_alerts(_inputs_from(doc, settings))],
            }
        )
    found.sort(key=lambda r: int(r["health"].get("ts_utc_ms", 0)), reverse=True)
    return found


def _downsample(points: list[Any], limit: int = MAX_EQUITY_POINTS) -> list[Any]:
    if len(points) <= limit:
        return points
    step = len(points) / limit
    picked = [points[int(i * step)] for i in range(limit)]
    picked[-1] = points[-1]
    return picked


def _shadow_summary(path: Path) -> dict[str, Any] | None:
    """Counts over the whole file, keeping only the latest records, so size stays bounded."""
    if not path.exists():
        return None
    counts: Counter[str] = Counter()
    recent: deque[dict[str, Any]] = deque(maxlen=SHADOW_RECENT_RECORDS)
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue  # The writer may be part-way through the last line.
                counts[record.get("kind", "unknown")] += 1
                body = record.get("body") or {}
                recent.append(
                    {
                        "kind": record.get("kind"),
                        "ts_utc_ms": record.get("ts_utc_ms"),
                        "intent_id": body.get("intent_id"),
                        "outcome": body.get("outcome"),
                        "side": body.get("side"),
                        "price": body.get("price"),
                        "size": body.get("size"),
                    }
                )
    except OSError:
        return None
    return {"counts": dict(counts), "recent": list(reversed(recent))}


def _session_kind(directory: Path) -> str:
    if (directory / "shadow_records.jsonl").exists():
        return "shadow"
    if (directory / "raw").is_dir():
        return "paper"
    return "replay"


def _sessions(root: Path, cache: ReportCache) -> list[dict[str, Any]]:
    sessions = []
    for directory in sorted(p for p in (root / "reports").glob("*") if p.is_dir()):
        if directory.name in _NOT_A_REPORT:
            continue
        live = cache.load(directory / "live.json")
        metrics = cache.load(directory / "metrics.json")
        # While running, the snapshot is the newer of the two. After a run they agree.
        running = isinstance(live, dict) and bool(live.get("running"))
        shown = live.get("metrics") if running and isinstance(live, dict) else metrics
        if shown is None and isinstance(live, dict):
            shown = live.get("metrics")
        if not isinstance(shown, dict):
            continue
        curve: list[Any] = []
        if isinstance(live, dict):
            curve = live.get("equity_curve") or []
        elif (timeline := cache.load(directory / "timeline.json")) is not None:
            curve = [[p["ts_utc_ms"], p["equity"]] for p in timeline]
        modified = (directory / ("live.json" if running else "metrics.json")).stat().st_mtime_ns
        sessions.append(
            {
                "name": directory.name,
                "kind": _session_kind(directory),
                "running": running,
                "ts_utc_ms": live.get("ts_utc_ms") if isinstance(live, dict) else None,
                "started_utc_ms": live.get("started_utc_ms") if isinstance(live, dict) else None,
                "modified_utc_ms": modified // 1_000_000,
                "metrics": shown,
                "equity_curve": _downsample(curve),
                "active_market": live.get("active_market") if isinstance(live, dict) else None,
                "open_orders": live.get("open_orders") if isinstance(live, dict) else [],
                "shadow": _shadow_summary(directory / "shadow_records.jsonl"),
            }
        )
    sessions.sort(key=lambda s: (s["running"], s["modified_utc_ms"]), reverse=True)
    return sessions


def build_state(root: Path, settings: Settings, cache: ReportCache) -> dict[str, Any]:
    """The whole page's data, from local files only. Safe to call repeatedly."""

    def report(relative: str) -> dict[str, Any]:
        return _section(root, relative, cache.load(root / relative))

    memo_path = root / MEMO_PATH
    try:
        memo = _section(root, MEMO_PATH, parse_readiness_memo(memo_path.read_text("utf-8")))
    except (OSError, UnicodeDecodeError):
        memo = _section(root, MEMO_PATH, None)
    return {
        "meta": {
            "version": __version__,
            "now_utc_ms": SystemClock().utc_ms(),
            "root": str(root),
            "max_clock_skew_ms": settings.risk.max_clock_skew_ms,
            "simulated": True,
            "live_disabled_reasons": live_gate_reasons(settings, None),
        },
        "memo": memo,
        "recorders": _recorders(root, settings, cache),
        "sessions": _sessions(root, cache),
        "compare": report(COMPARE_PATH),
        "scan": report(SCAN_PATH),
        "forecast": report(FORECAST_PATH),
        "audit": report(AUDIT_PATH),
        "coverage": report(COVERAGE_PATH),
        "performance": report(PERFORMANCE_PATH),
    }
