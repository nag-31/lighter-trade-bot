"""Append-only physical ledger files, one SQLite database per account."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from architecture_v2.domain.models import Execution, ExecutionSide, PositionSide
from architecture_v2.domain.policy import RunMode


_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")
UTC = timezone.utc


def account_ledger_path(root: str | Path, account_id: str) -> Path:
    safe = _SAFE.sub("_", account_id.strip()).strip("._") or "account"
    return Path(root) / "accounts" / f"{safe}.db"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class AccountLedgerStore:
    """Repository that never updates or deletes exchange facts."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def path(self, account_id: str) -> Path:
        return account_ledger_path(self.root, account_id)

    def connect(self, account_id: str) -> sqlite3.Connection:
        path = self.path(account_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        return con

    def init_account(self, account_id: str, *, exchange: str, label: str = "") -> Path:
        with self.connect(account_id) as con:
            now = _now()
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS ledger_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS account_meta (
                    account_id TEXT PRIMARY KEY, exchange TEXT NOT NULL,
                    label TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS exchange_fills (
                    execution_uid TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    market_key TEXT NOT NULL,
                    position_side TEXT NOT NULL,
                    native_trade_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    price TEXT NOT NULL,
                    fee TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_exchange_fills_time
                    ON exchange_fills(occurred_at, execution_uid);
                CREATE TABLE IF NOT EXISTS fill_observations (
                    observation_id TEXT PRIMARY KEY,
                    execution_uid TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    run_mode TEXT NOT NULL DEFAULT 'LIVE',
                    payload_hash TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    FOREIGN KEY (execution_uid) REFERENCES exchange_fills(execution_uid)
                );
                CREATE INDEX IF NOT EXISTS ix_fill_observations_uid
                    ON fill_observations(execution_uid, observed_at);
                """
            )
            con.execute(
                """INSERT INTO account_meta(account_id, exchange, label, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(account_id) DO UPDATE SET exchange=excluded.exchange,
                     label=CASE WHEN excluded.label <> '' THEN excluded.label ELSE account_meta.label END,
                     updated_at=excluded.updated_at""",
                (account_id, exchange, label or account_id, now, now),
            )
            con.execute(
                """INSERT INTO ledger_meta(key, value) VALUES ('schema_version', '2')
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value"""
            )
            observation_columns = {
                row["name"] for row in con.execute("PRAGMA table_info(fill_observations)")
            }
            if "run_mode" not in observation_columns:
                con.execute(
                    "ALTER TABLE fill_observations ADD COLUMN "
                    "run_mode TEXT NOT NULL DEFAULT 'LIVE'"
                )
        return self.path(account_id)

    def append(
        self,
        execution: Execution,
        *,
        raw_payload: Any | None = None,
        observed_at: datetime | None = None,
        mode: RunMode = RunMode.LIVE,
    ) -> bool:
        """Append a fact idempotently; changed payloads become observations."""
        self.init_account(execution.account_id, exchange=execution.exchange)
        payload = raw_payload if raw_payload is not None else {
            "execution_uid": execution.execution_uid,
            "account_id": execution.account_id,
            "exchange": execution.exchange,
            "market_key": execution.market_key,
            "position_side": execution.position_side.value,
            "native_trade_id": execution.native_trade_id,
            "occurred_at": execution.occurred_at.isoformat(),
            "side": execution.side.value,
            "quantity": str(execution.quantity),
            "price": str(execution.price),
            "fee": str(execution.fee),
        }
        raw_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        observed = (observed_at or datetime.now(UTC)).astimezone(UTC).isoformat()
        observation_id = hashlib.sha256(
            f"{execution.execution_uid}|{_hash(payload)}".encode("utf-8")
        ).hexdigest()
        with self.connect(execution.account_id) as con:
            row = con.execute(
                "SELECT * FROM exchange_fills WHERE execution_uid=?",
                (execution.execution_uid,),
            ).fetchone()
            if row:
                existing = self._execution(row)
                if existing != execution:
                    raise ValueError(f"execution UID collision: {execution.execution_uid}")
                con.execute(
                    """INSERT OR IGNORE INTO fill_observations(
                       observation_id, execution_uid, observed_at, run_mode,
                       payload_hash, raw_json)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        observation_id, execution.execution_uid, observed,
                        mode.value, _hash(payload), raw_json,
                    ),
                )
                return False
            con.execute(
                """INSERT INTO exchange_fills(
                   execution_uid, account_id, exchange, market_key, position_side,
                   native_trade_id, occurred_at, side, quantity, price, fee,
                   raw_json, first_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    execution.execution_uid, execution.account_id, execution.exchange,
                    execution.market_key, execution.position_side.value, execution.native_trade_id,
                    execution.occurred_at.isoformat(), execution.side.value,
                    str(execution.quantity), str(execution.price), str(execution.fee),
                    raw_json, observed,
                ),
            )
            con.execute(
                """INSERT INTO fill_observations(
                   observation_id, execution_uid, observed_at, run_mode,
                   payload_hash, raw_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    observation_id, execution.execution_uid, observed,
                    mode.value, _hash(payload), raw_json,
                ),
            )
        return True

    @staticmethod
    def _execution(row: sqlite3.Row) -> Execution:
        return Execution(
            execution_uid=row["execution_uid"], account_id=row["account_id"],
            exchange=row["exchange"], market_key=row["market_key"],
            position_side=PositionSide(row["position_side"]),
            native_trade_id=row["native_trade_id"],
            occurred_at=datetime.fromisoformat(row["occurred_at"].replace("Z", "+00:00")).astimezone(UTC),
            side=ExecutionSide(row["side"]), quantity=Decimal(row["quantity"]),
            price=Decimal(row["price"]), fee=Decimal(row["fee"]),
        )

    def list_executions(self, account_id: str) -> tuple[Execution, ...]:
        with self.connect(account_id) as con:
            rows = con.execute("SELECT * FROM exchange_fills ORDER BY occurred_at, execution_uid").fetchall()
        return tuple(self._execution(row) for row in rows)

    def integrity_check(self, account_id: str) -> str:
        with self.connect(account_id) as con:
            return str(con.execute("PRAGMA integrity_check").fetchone()[0])

    def input_snapshot_hash(self, account_id: str) -> str:
        with self.connect(account_id) as con:
            rows = con.execute(
                """SELECT execution_uid, account_id, exchange, market_key, position_side,
                   native_trade_id, occurred_at, side, quantity, price, fee
                   FROM exchange_fills ORDER BY occurred_at, execution_uid"""
            ).fetchall()
        return _hash([tuple(row) for row in rows])
