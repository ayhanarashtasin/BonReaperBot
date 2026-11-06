"""Explicitly named stub (tasks.md rule 5): authenticated account access is not built.

BR-701 requires separate authorization. Anything needing authenticated account data must fail
loudly here instead of silently working with substitute data.
"""

from __future__ import annotations


class SecureClientUnavailableError(RuntimeError):
    pass


def build_secure_client() -> None:
    raise SecureClientUnavailableError(
        "authenticated client is blocked pending explicit authorization (BR-701)"
    )
