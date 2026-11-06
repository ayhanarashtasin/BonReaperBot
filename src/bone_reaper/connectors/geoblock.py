"""Geographic eligibility check. Fail closed; never route around a restriction.

Docs: https://docs.polymarket.com/api-reference/geoblock
The HTTP client is built with ``trust_env=False`` so that environment proxy settings cannot
silently change the location being tested. There is deliberately no proxy or VPN option here.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

GEOBLOCK_URL = "https://polymarket.com/api/geoblock"


class GeoStatus(enum.StrEnum):
    ALLOWED = "allowed"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class GeoResult:
    """The caller's IP address is intentionally not retained (PRD section 8)."""

    status: GeoStatus
    country: str | None
    region: str | None
    detail: str

    @property
    def may_open_real_positions(self) -> bool:
        return self.status is GeoStatus.ALLOWED


Fetch = Callable[[], tuple[int, Any]]


def _http_fetch() -> tuple[int, Any]:
    with httpx.Client(timeout=10.0, trust_env=False, follow_redirects=False) as client:
        response = client.get(GEOBLOCK_URL)
        return response.status_code, response.json()


def check_geoblock(fetch: Fetch = _http_fetch) -> GeoResult:
    try:
        status_code, body = fetch()
    except Exception as exc:
        return GeoResult(GeoStatus.UNKNOWN, None, None, f"check_failed:{type(exc).__name__}")
    if status_code != 200 or not isinstance(body, dict):
        return GeoResult(GeoStatus.UNKNOWN, None, None, f"bad_response:{status_code}")
    blocked = body.get("blocked")
    country = body.get("country") if isinstance(body.get("country"), str) else None
    region = body.get("region") if isinstance(body.get("region"), str) else None
    if blocked is True:
        return GeoResult(GeoStatus.BLOCKED, country, region, "venue_reports_blocked")
    if blocked is False:
        return GeoResult(GeoStatus.ALLOWED, country, region, "venue_reports_allowed")
    return GeoResult(GeoStatus.UNKNOWN, country, region, "blocked_field_missing_or_invalid")
