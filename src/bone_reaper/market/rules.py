"""Instrument registry and resolution-rule verification.

An instrument is supported only if its exact rule text, oracle and series have been reviewed. A
slug or title alone never makes a market eligible. Changing any verified constant needs an ADR.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..types import sha256_hex


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str
    duration_minutes: int
    series_slug: str
    recurrence: str
    oracle_source: str
    twap_symbol: str
    rtds_symbol: str
    rules_hash: str
    verified: bool


def normalize_rules(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def rules_hash(text: str) -> str:
    return sha256_hex(normalize_rules(text).encode("utf-8"))


# Hash of the normalized resolution text reviewed on 2026-11-06 (reports/sdk_audit.md).
BTC_5M_RULES_HASH = "0958a68b74e364754cdf988749053ccce1592942c2cd7639420109897fd778f4"

INSTRUMENTS: dict[tuple[str, int], Instrument] = {
    ("BTC", 5): Instrument(
        symbol="BTC",
        duration_minutes=5,
        series_slug="btc-up-or-down-5m",
        recurrence="5m",
        oracle_source="https://data.chain.link/streams/btc-usd-twap-60s-streams",
        twap_symbol="btcusd",
        rtds_symbol="btc/usd",
        rules_hash=BTC_5M_RULES_HASH,
        verified=True,
    ),
}


class UnsupportedInstrumentError(ValueError):
    pass


def get_instrument(symbol: str, duration_minutes: int) -> Instrument:
    instrument = INSTRUMENTS.get((symbol.upper(), duration_minutes))
    if instrument is None or not instrument.verified:
        raise UnsupportedInstrumentError(
            f"{symbol} {duration_minutes}m has no verified rules; it cannot be enabled by "
            "changing a slug (PRD section 4)"
        )
    return instrument


REQUIRED_RULE_PHRASES = ("chainlink", "twap", "greater than or equal to", "price at the beginning")


def check_rules(description: str | None, oracle: str | None, instrument: Instrument) -> list[str]:
    """Return rejection reasons. An empty list means the rule text matches the reviewed one."""
    reasons: list[str] = []
    if not description or not description.strip():
        return ["rules_missing"]
    lowered = normalize_rules(description).lower()
    for phrase in REQUIRED_RULE_PHRASES:
        if phrase not in lowered:
            reasons.append(f"rules_missing_phrase:{phrase}")
    if oracle != instrument.oracle_source:
        reasons.append("oracle_source_mismatch")
    if rules_hash(description) != instrument.rules_hash:
        reasons.append("rules_hash_changed")
    return reasons
