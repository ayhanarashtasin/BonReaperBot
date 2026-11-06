"""Explicitly named stub (tasks.md rule 5): the authenticated user stream is BR-703 (blocked)."""

from __future__ import annotations

from .polymarket_secure import SecureClientUnavailableError


def open_user_stream() -> None:
    raise SecureClientUnavailableError("user order/fill stream is blocked pending BR-703")
