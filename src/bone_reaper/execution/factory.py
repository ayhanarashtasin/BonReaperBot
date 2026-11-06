"""The only place an execution adapter is constructed (BR-007)."""

from __future__ import annotations

from collections.abc import Callable

from ..connectors.geoblock import GeoResult
from ..settings import Settings
from ..types import Mode
from .ports import ExecutionPort
from .real import build_real_execution


class NoExecutionInReadOnlyError(RuntimeError):
    """Read-only processes have no order-submission adapter at all."""


def build_execution(
    settings: Settings,
    geo: GeoResult | None = None,
    *,
    paper_factory: Callable[[], ExecutionPort] | None = None,
    shadow_factory: Callable[[], ExecutionPort] | None = None,
) -> ExecutionPort:
    if settings.mode is Mode.READ_ONLY:
        raise NoExecutionInReadOnlyError("read_only mode constructs no execution adapter")
    if settings.mode is Mode.PAPER:
        if paper_factory is None:
            raise ValueError("paper mode needs a simulated exchange factory")
        return _require_simulated(paper_factory())
    if settings.mode is Mode.SHADOW:
        if shadow_factory is None:
            raise ValueError("shadow mode needs a shadow factory")
        return _require_simulated(shadow_factory())
    return build_real_execution(settings, geo)


def _require_simulated(port: ExecutionPort) -> ExecutionPort:
    if port.is_real:
        raise RuntimeError("non-live modes refuse any adapter that claims to be real")
    return port
