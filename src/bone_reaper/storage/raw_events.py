"""Append-only Parquet recorder partitioned as ``source/YYYY-MM-DD/symbol/market_id``.

Parts are created exclusively and never rewritten. Corrections go in new manifests. A per-part
SHA-256 goes in a JSONL manifest so loaders can detect tampering or truncation.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from ..types import EventEnvelope

RAW_SCHEMA = pa.schema(
    [
        ("event_id", pa.string()),
        ("schema_version", pa.int32()),
        ("source", pa.string()),
        ("topic", pa.string()),
        ("market_id", pa.string()),
        ("asset_id", pa.string()),
        ("source_ts_ms", pa.int64()),
        ("received_at_ns", pa.int64()),
        ("received_utc_ms", pa.int64()),
        ("segment_id", pa.string()),
        ("seq", pa.int64()),
        ("payload_json", pa.binary()),
    ]
)
MANIFEST_NAME = "manifest.jsonl"


class RecorderError(RuntimeError):
    """Storage failure. Callers must treat data as discontinuous and stop strategies."""


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _date(utc_ms: int) -> str:
    return datetime.fromtimestamp(utc_ms / 1000, tz=UTC).strftime("%Y-%m-%d")


@dataclass(slots=True)
class RawEventWriter:
    root: Path
    symbol: str = "BTC"
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    max_buffer: int = 5000
    _buffers: dict[tuple[str, str, str], list[EventEnvelope]] = field(
        default_factory=lambda: defaultdict(list)
    )
    _part: int = 0
    rows_written: int = 0
    parts_written: int = 0

    def append(self, envelope: EventEnvelope) -> None:
        key = (envelope.source, _date(envelope.received_utc_ms), envelope.market_id or "_global")
        buffer = self._buffers[key]
        buffer.append(envelope)
        if len(buffer) >= self.max_buffer:
            self._flush_key(key)

    def flush(self) -> None:
        for key in list(self._buffers):
            self._flush_key(key)

    def _flush_key(self, key: tuple[str, str, str]) -> None:
        rows = self._buffers.get(key)
        if not rows:
            return
        source, date, market = key
        directory = self.root / source / date / self.symbol / market
        self._part += 1
        path = directory / f"part-{self.run_id}-{self._part:06d}.parquet"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as handle:  # exclusive: refuses to overwrite history
                pq.write_table(_to_table(rows), handle, compression="zstd")
            record = {
                "path": path.relative_to(self.root).as_posix(),
                "sha256": _file_sha256(path),
                "rows": len(rows),
                "run_id": self.run_id,
                "source": source,
                "market_id": market,
                "first_received_utc_ms": rows[0].received_utc_ms,
                "last_received_utc_ms": rows[-1].received_utc_ms,
            }
            with (self.root / MANIFEST_NAME).open("a", encoding="utf-8") as manifest:
                manifest.write(json.dumps(record, sort_keys=True) + "\n")
        except OSError as exc:
            raise RecorderError(f"cannot persist {path}: {exc}") from exc
        self.rows_written += len(rows)
        self.parts_written += 1
        rows.clear()


def _to_table(rows: list[EventEnvelope]) -> pa.Table:
    return pa.Table.from_pydict(
        {
            "event_id": [r.event_id for r in rows],
            "schema_version": [r.schema_version for r in rows],
            "source": [r.source for r in rows],
            "topic": [r.topic for r in rows],
            "market_id": [r.market_id for r in rows],
            "asset_id": [r.asset_id for r in rows],
            "source_ts_ms": [r.source_ts_ms for r in rows],
            "received_at_ns": [r.received_at_ns for r in rows],
            "received_utc_ms": [r.received_utc_ms for r in rows],
            "segment_id": [r.segment_id for r in rows],
            "seq": [r.seq for r in rows],
            "payload_json": [r.payload_json for r in rows],
        },
        schema=RAW_SCHEMA,
    )


@dataclass(frozen=True, slots=True)
class ManifestRecord:
    path: str
    sha256: str
    rows: int
    source: str
    market_id: str
    first_received_utc_ms: int
    last_received_utc_ms: int


def load_manifest(root: Path) -> list[ManifestRecord]:
    path = root / MANIFEST_NAME
    if not path.exists():
        raise RecorderError(f"no {MANIFEST_NAME} under {root}")
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        doc: dict[str, Any] = json.loads(line)
        records.append(
            ManifestRecord(
                path=doc["path"],
                sha256=doc["sha256"],
                rows=doc["rows"],
                source=doc["source"],
                market_id=doc["market_id"],
                first_received_utc_ms=doc["first_received_utc_ms"],
                last_received_utc_ms=doc["last_received_utc_ms"],
            )
        )
    return records


def verify_manifest(root: Path, records: Iterable[ManifestRecord]) -> list[str]:
    problems: list[str] = []
    for record in records:
        path = root / record.path
        if not path.exists():
            problems.append(f"missing:{record.path}")
        elif _file_sha256(path) != record.sha256:
            problems.append(f"checksum_mismatch:{record.path}")
    return problems


def _read_part(path: Path) -> list[EventEnvelope]:
    table = pq.read_table(path, schema=RAW_SCHEMA)
    out = []
    for row in table.to_pylist():
        out.append(
            EventEnvelope(
                event_id=row["event_id"],
                schema_version=row["schema_version"],
                source=row["source"],
                topic=row["topic"],
                market_id=row["market_id"],
                asset_id=row["asset_id"],
                source_ts_ms=row["source_ts_ms"],
                received_at_ns=row["received_at_ns"],
                received_utc_ms=row["received_utc_ms"],
                payload_json=row["payload_json"],
                segment_id=row["segment_id"],
                seq=row["seq"],
            )
        )
    return out


def read_events(
    root: Path, *, verify: bool = True, sources: Iterable[str] | None = None
) -> Iterator[EventEnvelope]:
    """All recorded events in stable arrival order: receive time, then segment and sequence."""
    records = load_manifest(root)
    if verify:
        problems = verify_manifest(root, records)
        if problems:
            raise RecorderError(f"manifest verification failed: {problems[:5]}")
    wanted = set(sources) if sources is not None else None
    events: list[EventEnvelope] = []
    for record in records:
        if wanted is None or record.source in wanted:
            events.extend(_read_part(root / record.path))
    events.sort(key=lambda e: (e.received_utc_ms, e.segment_id, e.seq, e.event_id))
    return iter(events)
