"""Durable single-writer store (BR-601). Requires the optional ``postgres`` extra.

One process per wallet: ownership is a session-level advisory lock, released automatically if the
process dies. Intent, reservation and journal commit together; a fill and its ledger entries commit
together; the unique key on (trade_id, order_leg_key) makes duplicates harmless.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from ..types import FillRecord, LedgerEntry, QuoteIntent, to_jsonable

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 1


class StoreError(RuntimeError):
    """Any database failure. Callers must stop new risk: nothing proceeds without durability."""


class WalletLockedError(StoreError):
    """Another process already owns this wallet's writer lock."""


def _lock_key(wallet_id: str) -> int:
    digest = hashlib.sha256(f"bone-reaper:{wallet_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


class PostgresStore:
    def __init__(self, dsn: str, wallet_id: str) -> None:
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise StoreError("install the 'postgres' extra to use PostgresStore") from exc
        self._psycopg = psycopg
        self.wallet_id = wallet_id
        try:
            self._conn: Any = psycopg.connect(dsn, autocommit=True)
        except psycopg.Error as exc:
            raise StoreError(f"cannot connect: {type(exc).__name__}") from exc
        self._owns_lock = False

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._conn.close()

    def migrate(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute(SCHEMA_PATH.read_text("utf-8"))
            cur.execute(
                "INSERT INTO schema_migrations (version) VALUES (%s) ON CONFLICT DO NOTHING",
                (SCHEMA_VERSION,),
            )

    def try_acquire_writer(self) -> bool:
        with self._conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (_lock_key(self.wallet_id),))
            self._owns_lock = bool(cur.fetchone()[0])
        return self._owns_lock

    def require_writer(self) -> None:
        if not self.try_acquire_writer():
            raise WalletLockedError(f"wallet {self.wallet_id} already has a writer")

    def _check_writer(self) -> None:
        if not self._owns_lock:
            raise WalletLockedError("writes need the wallet writer lock")

    @contextlib.contextmanager
    def _tx(self) -> Iterator[Any]:
        self._check_writer()
        try:
            with self._conn.transaction(), self._conn.cursor() as cur:
                yield cur
        except self._psycopg.Error as exc:
            raise StoreError(f"transaction failed: {type(exc).__name__}") from exc

    # --- atomic operations ------------------------------------------------------------------

    def record_intent(
        self,
        intent: QuoteIntent,
        reservation_amount: Any,
        ts_utc_ms: int,
        *,
        fail_after: bool = False,
    ) -> None:
        """Intent, reservation and journal commit together or not at all."""
        with self._tx() as cur:
            cur.execute(
                """INSERT INTO order_intents (intent_id, wallet_id, created_utc_ms, market_id, side,
                   asset_id, quantity, price, strategy, purpose, risk_approval_id, status)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    intent.intent_id,
                    self.wallet_id,
                    ts_utc_ms,
                    intent.market_id,
                    intent.side.value,
                    intent.asset_id,
                    intent.size,
                    intent.price,
                    intent.strategy_id,
                    intent.purpose.value,
                    intent.risk_id,
                    "PREPARED",
                ),
            )
            cur.execute(
                """INSERT INTO cash_reservations (reservation_id, intent_id, asset, amount, state)
                   VALUES (%s,%s,%s,%s,'reserved')""",
                (f"res:{intent.intent_id}", intent.intent_id, "pUSD", reservation_amount),
            )
            self._journal(
                cur,
                "intent",
                ts_utc_ms,
                {"kind": "intent", "ts_utc_ms": ts_utc_ms, "intent": to_jsonable(intent)},
            )
            if fail_after:  # Fault-injection hook used by the atomicity test.
                raise self._psycopg.OperationalError("injected failure")

    def record_fill(self, fill: FillRecord, entries: Sequence[LedgerEntry]) -> bool:
        """Commit a fill and its accounting exactly once. Returns False for a duplicate."""
        with self._tx() as cur:
            cur.execute(
                """INSERT INTO fills (trade_id, order_leg_key, intent_id, asset_id, executed_shares,
                   net_shares, cash_delta, fee_cash, fee_shares, fee_asset, role, status, simulated,
                   ts_utc_ms) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (trade_id, order_leg_key) DO NOTHING""",
                (
                    fill.trade_id,
                    fill.leg_key,
                    fill.intent_id,
                    fill.asset_id,
                    fill.gross_shares,
                    fill.net_shares,
                    fill.cash_delta,
                    fill.fee_cash,
                    fill.fee_shares,
                    fill.fee_asset,
                    fill.role.value,
                    fill.status.value,
                    fill.simulated,
                    fill.ts_utc_ms,
                ),
            )
            if cur.rowcount == 0:
                return False
            for entry in entries:
                cur.execute(
                    """INSERT INTO ledger_entries (entry_id, cause_id, wallet_id, account, asset,
                       debit, credit, ts_utc_ms, settlement_status, reversal_of)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        entry.entry_id,
                        entry.cause_id,
                        self.wallet_id,
                        entry.account,
                        entry.asset,
                        entry.debit,
                        entry.credit,
                        entry.ts_utc_ms,
                        entry.settlement_status,
                        entry.reversal_of,
                    ),
                )
            self._journal(
                cur,
                "fill",
                fill.ts_utc_ms,
                {"kind": "fill", "ts_utc_ms": fill.ts_utc_ms, "fill": to_jsonable(fill)},
            )
            return True

    def _journal(self, cur: Any, kind: str, ts_utc_ms: int, body: Any) -> None:
        cur.execute(
            "INSERT INTO journal (wallet_id, kind, ts_utc_ms, body) VALUES (%s,%s,%s,%s)",
            (self.wallet_id, kind, ts_utc_ms, json.dumps(body, sort_keys=True)),
        )

    # --- Journal protocol, so the OMS can use this store directly ----------------------------

    def append(self, record: dict[str, Any]) -> None:
        with self._tx() as cur:
            self._journal(cur, str(record["kind"]), int(record["ts_utc_ms"]), record)

    def read_all(self) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT body FROM journal WHERE wallet_id = %s ORDER BY seq", (self.wallet_id,)
            )
            return [json.loads(row[0]) for row in cur.fetchall()]

    def count(self, table: str) -> int:
        allowed = {
            "order_intents",
            "cash_reservations",
            "fills",
            "ledger_entries",
            "journal",
        }
        if table not in allowed:
            raise ValueError(table)
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {table}")  # noqa: S608 - allowlisted name
            return int(cur.fetchone()[0])
