"""Real order adapter: deliberately absent.

P7 (BR-701 to BR-705) is blocked until a separate explicit authorization, confirmed eligibility
and a passed G6. Until then every path returns LIVE_DISABLED and nothing in this package imports
an order-placing SDK client. Enabling flags in a config file does not change that.
"""

from __future__ import annotations

from ..connectors.geoblock import GeoResult
from ..settings import Settings
from .ports import ExecutionPort

LIVE_DISABLED = "LIVE_DISABLED"


class LiveDisabledError(RuntimeError):
    def __init__(self, reasons: list[str]) -> None:
        super().__init__(f"{LIVE_DISABLED}: {', '.join(reasons)}")
        self.reasons = reasons


def live_gate_reasons(settings: Settings, geo: GeoResult | None) -> list[str]:
    """Every reason a real order could not be sent. Never empty in this release."""
    reasons = ["real_adapter_not_implemented_p7_blocked"]
    if settings.mode.value != "live":
        reasons.append("mode_not_live")
    if not settings.live_enabled:
        reasons.append("live_enabled_false")
    if not settings.live_operator_approval:
        reasons.append("operator_approval_absent")
    if geo is None:
        reasons.append("geoblock_not_checked")
    elif not geo.may_open_real_positions:
        reasons.append(f"geoblock_{geo.status.value}")
    return reasons


def build_real_execution(settings: Settings, geo: GeoResult | None) -> ExecutionPort:
    raise LiveDisabledError(live_gate_reasons(settings, geo))
