"""Periodic book checkpoints that reference their raw event offset. They speed replay up; raw
events stay authoritative. Checkpoints are append-only JSONL."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..types import BookState, from_jsonable, to_jsonable


@dataclass(frozen=True, slots=True)
class Checkpoint:
    ts_utc_ms: int
    raw_event_offset: int
    books: tuple[BookState, ...]


class CheckpointWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, checkpoint: Checkpoint) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(to_jsonable(checkpoint), sort_keys=True) + "\n")


def read_checkpoints(path: Path) -> list[Checkpoint]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        doc: dict[str, Any] = json.loads(line)
        out.append(from_jsonable(Checkpoint, doc))
    return out
