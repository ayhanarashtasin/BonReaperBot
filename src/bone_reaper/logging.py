"""Structured JSON logs. Secrets are redacted by key name and by value shape."""

from __future__ import annotations

import json
import logging as _stdlogging
import re
import sys
from typing import Any

_SECRET_KEY = re.compile(
    r"(private[_-]?key|secret|passphrase|seed|mnemonic|authorization|api[_-]?key|password)",
    re.IGNORECASE,
)
_PRIVATE_KEY_VALUE = re.compile(r"\b(0x)?[0-9a-fA-F]{64}\b")
_MNEMONIC_VALUE = re.compile(r"\b(?:[a-z]{3,8} ){11,23}[a-z]{3,8}\b")
REDACTED = "[REDACTED]"


def redact(value: Any, key: str | None = None) -> Any:
    if key is not None and _SECRET_KEY.search(key):
        return REDACTED
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _MNEMONIC_VALUE.sub(REDACTED, _PRIVATE_KEY_VALUE.sub(REDACTED, value))
    return value


class JsonFormatter(_stdlogging.Formatter):
    def format(self, record: _stdlogging.LogRecord) -> str:
        fields = getattr(record, "fields", {})
        doc = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
            **redact(fields if isinstance(fields, dict) else {}),
        }
        if record.exc_info and record.exc_info[1] is not None:
            doc["exc"] = redact(repr(record.exc_info[1]))
        return json.dumps(doc, default=str, sort_keys=True)


def configure(level: int = _stdlogging.INFO, stream: Any = None) -> None:
    handler = _stdlogging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = _stdlogging.getLogger("bone_reaper")
    root.handlers[:] = [handler]
    root.setLevel(level)
    root.propagate = False


def log_event(logger: _stdlogging.Logger, level: int, msg: str, **fields: Any) -> None:
    """Attach run_id, market_id, intent_id, reason_code and similar as top-level JSON fields."""
    logger.log(level, msg, extra={"fields": fields})
