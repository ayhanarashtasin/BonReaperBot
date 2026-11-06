"""Kill switch: stops new risk and triggers cancel attempts. It does not liquidate positions.

Transient reasons (stale feed, near expiry) clear when the condition clears. Latched reasons (loss
cap, reconciliation mismatch) need an explicit operator reset and never clear automatically.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class KillReason(enum.StrEnum):
    STALE_FEED = "stale_feed"
    UNKNOWN_ORDER = "unknown_order"
    LOSS_CAP = "loss_cap"
    RECONCILIATION_MISMATCH = "reconciliation_mismatch"
    CLOCK_SKEW = "clock_skew"
    NEAR_EXPIRY = "near_expiry"
    DATA_GAP = "data_gap"
    STORAGE_FAILURE = "storage_failure"
    OPERATOR = "operator"


LATCHED = frozenset(
    {
        KillReason.LOSS_CAP,
        KillReason.RECONCILIATION_MISMATCH,
        KillReason.STORAGE_FAILURE,
        KillReason.OPERATOR,
    }
)


@dataclass(slots=True)
class KillSwitch:
    _active: dict[KillReason, int] = field(default_factory=dict)
    history: list[tuple[int, str, str]] = field(default_factory=list)

    def engage(self, reason: KillReason, ts_utc_ms: int) -> None:
        if reason not in self._active:
            self._active[reason] = ts_utc_ms
            self.history.append((ts_utc_ms, "engage", reason.value))

    def clear(self, reason: KillReason, ts_utc_ms: int) -> None:
        """Clear a transient condition. Latched reasons ignore this."""
        if reason in LATCHED:
            return
        if self._active.pop(reason, None) is not None:
            self.history.append((ts_utc_ms, "clear", reason.value))

    def operator_reset(self, ts_utc_ms: int, operator: str) -> None:
        if not operator.strip():
            raise ValueError("operator reset needs an operator identity")
        self.history.append((ts_utc_ms, f"operator_reset:{operator}", ",".join(self.reasons)))
        self._active.clear()

    def set_condition(self, reason: KillReason, active: bool, ts_utc_ms: int) -> None:
        if active:
            self.engage(reason, ts_utc_ms)
        else:
            self.clear(reason, ts_utc_ms)

    @property
    def active(self) -> bool:
        return bool(self._active)

    @property
    def reasons(self) -> list[str]:
        return sorted(r.value for r in self._active)

    @property
    def latched(self) -> bool:
        return any(r in LATCHED for r in self._active)

    def has(self, reason: KillReason) -> bool:
        return reason in self._active

    def blocks_new_risk(self, ignoring: frozenset[KillReason] = frozenset()) -> list[str]:
        return sorted(r.value for r in self._active if r not in ignoring)
