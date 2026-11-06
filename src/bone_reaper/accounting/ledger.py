"""Append-only double-entry ledger. Every batch balances per asset and commits exactly once.

Holdings are debit-normal: ``balance = debits - credits``. Guarded accounts can never go negative;
a batch that would break that is refused whole. History is corrected by reversal, never edited.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from ..types import SETTLEMENT_ASSET, ZERO, LedgerEntry

CASH_AVAILABLE = "cash:available"
CASH_RESERVED = "cash:reserved"
CASH_REDEEMABLE = "cash:redeemable"
SHARES_HELD = "shares:held"
SHARES_RESERVED = "shares:reserved"
SHARES_BURNED = "shares:burned"
FEES_CASH = "expense:fees"
FEES_SHARES = "expense:fee_shares"
VENUE = "external:venue"
SETTLEMENT = "external:settlement"
MERGE = "external:merge"
CAPITAL = "equity:capital"
ADJUSTMENT = "equity:reconciliation_adjustment"

GUARDED_ACCOUNTS = frozenset(
    {CASH_AVAILABLE, CASH_RESERVED, CASH_REDEEMABLE, SHARES_HELD, SHARES_RESERVED}
)


class LedgerError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Line:
    account: str
    asset: str
    debit: Decimal = ZERO
    credit: Decimal = ZERO

    def __post_init__(self) -> None:
        if self.debit < 0 or self.credit < 0:
            raise ValueError("debit and credit must be non-negative")


def transfer(asset: str, debit_account: str, credit_account: str, amount: Decimal) -> list[Line]:
    return [Line(debit_account, asset, debit=amount), Line(credit_account, asset, credit=amount)]


class Ledger:
    def __init__(self) -> None:
        self.entries: list[LedgerEntry] = []
        self._causes: set[str] = set()
        self._balances: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)

    def has_cause(self, cause_id: str) -> bool:
        return cause_id in self._causes

    def post(
        self,
        cause_id: str,
        lines: Iterable[Line],
        ts_utc_ms: int,
        *,
        settlement_status: str = "n/a",
        reversal_of: str | None = None,
    ) -> bool:
        """Atomically post a balanced batch. Returns False if this cause was already applied."""
        if cause_id in self._causes:
            return False
        batch = [line for line in lines if line.debit != 0 or line.credit != 0]
        per_asset: dict[str, Decimal] = defaultdict(lambda: ZERO)
        deltas: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
        for line in batch:
            per_asset[line.asset] += line.debit - line.credit
            deltas[(line.account, line.asset)] += line.debit - line.credit
        unbalanced = {asset: total for asset, total in per_asset.items() if total != 0}
        if unbalanced:
            raise LedgerError(f"unbalanced batch {cause_id}: {unbalanced}")
        for (account, asset), delta in deltas.items():
            if account in GUARDED_ACCOUNTS and self._balances[(account, asset)] + delta < 0:
                raise LedgerError(
                    f"{cause_id} would make {account}/{asset} negative "
                    f"({self._balances[(account, asset)]} + {delta})"
                )
        for index, line in enumerate(batch):
            self.entries.append(
                LedgerEntry(
                    entry_id=f"{cause_id}/{index}",
                    cause_id=cause_id,
                    account=line.account,
                    asset=line.asset,
                    debit=line.debit,
                    credit=line.credit,
                    ts_utc_ms=ts_utc_ms,
                    settlement_status=settlement_status,
                    reversal_of=reversal_of,
                )
            )
        for key, delta in deltas.items():
            self._balances[key] += delta
        self._causes.add(cause_id)
        return True

    def reverse(self, cause_id: str, ts_utc_ms: int) -> bool:
        """Post the exact opposite of an earlier batch. History itself is untouched."""
        reversal_id = f"reversal:{cause_id}"
        if reversal_id in self._causes:
            return False
        original = [e for e in self.entries if e.cause_id == cause_id]
        if not original:
            raise LedgerError(f"nothing to reverse for {cause_id}")
        lines = [Line(e.account, e.asset, debit=e.credit, credit=e.debit) for e in original]
        return self.post(reversal_id, lines, ts_utc_ms, reversal_of=cause_id)

    def balance(self, account: str, asset: str = SETTLEMENT_ASSET) -> Decimal:
        return self._balances[(account, asset)]

    def conservation_errors(self) -> list[str]:
        """Independent recomputation of every asset total and every guarded balance."""
        errors: list[str] = []
        per_asset: dict[str, Decimal] = defaultdict(lambda: ZERO)
        recomputed: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
        for entry in self.entries:
            per_asset[entry.asset] += entry.debit - entry.credit
            recomputed[(entry.account, entry.asset)] += entry.debit - entry.credit
        errors += [f"asset {a} out of balance: {t}" for a, t in per_asset.items() if t != 0]
        for key, value in recomputed.items():
            if self._balances[key] != value:
                errors.append(f"cached balance mismatch for {key}")
            if key[0] in GUARDED_ACCOUNTS and value < 0:
                errors.append(f"guarded account negative: {key}={value}")
        return errors
