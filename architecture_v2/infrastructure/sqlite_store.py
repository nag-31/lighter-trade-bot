from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from architecture_v2 import ACCOUNTING_VERSION
from architecture_v2.domain.accounting import project_account
from architecture_v2.domain.identity import require_identity, stable_uid
from architecture_v2.domain.models import (
    AccountProjection,
    Execution,
    ExecutionSide,
    Lifecycle,
    LifecycleStatus,
    PositionDirection,
    PositionSide,
    Realization,
)


MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
DEFAULT_PORTFOLIO_ID = "all"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _time(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("stored timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class ProjectionCheckpoint:
    account_id: str
    accounting_version: str
    last_execution_at: datetime
    last_execution_uid: str
    projected_at: datetime


@dataclass(frozen=True, slots=True)
class OutboxItem:
    sequence_id: int
    event_uid: str
    topic: str
    payload_json: str
    created_at: datetime
    attempts: int


class SqliteV2Store:
    """SQLite implementation of the isolated, account-partitioned V2 ledger."""

    _COUNTABLE = frozenset(
        {
            "v2_accounts",
            "v2_portfolios",
            "v2_portfolio_memberships",
            "v2_executions",
            "v2_realizations",
            "v2_lifecycles",
            "v2_projection_checkpoints",
            "v2_integration_outbox",
        }
    )

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA journal_mode = WAL")
        return con

    def init(self) -> None:
        with self.connect() as con:
            first = MIGRATIONS_DIR / "001_accounting.sql"
            con.executescript(first.read_text(encoding="utf-8"))
            columns = {row["name"] for row in con.execute("PRAGMA table_info(v2_lifecycles)")}
            if "holding_duration_ms" not in columns:
                con.execute("ALTER TABLE v2_lifecycles ADD COLUMN holding_duration_ms INTEGER")
            if "holding_duration_basis" not in columns:
                con.execute("ALTER TABLE v2_lifecycles ADD COLUMN holding_duration_basis TEXT NOT NULL DEFAULT 'unavailable'")
            con.execute(
                """UPDATE v2_lifecycles SET holding_duration_ms =
                   CAST(MAX(0, (julianday(closed_at)-julianday(opened_at))*86400000) AS INTEGER),
                   holding_duration_basis = 'exact'
                   WHERE closed_at IS NOT NULL AND holding_duration_ms IS NULL"""
            )
            con.executescript((MIGRATIONS_DIR / "002_holding_time.sql").read_text(encoding="utf-8"))
            con.execute(
                "INSERT INTO v2_schema_meta(key, value) VALUES('holding_time_schema', '2') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
            now = _now()
            con.execute(
                """
                INSERT INTO v2_portfolios(
                    portfolio_id, display_name, created_at, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(portfolio_id) DO NOTHING
                """,
                (DEFAULT_PORTFOLIO_ID, "All tracked accounts", now, now),
            )

    @staticmethod
    def _ensure_account(con: sqlite3.Connection, execution: Execution) -> None:
        now = _now()
        con.execute(
            """
            INSERT INTO v2_accounts(
                account_id, exchange, display_name, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
                exchange=excluded.exchange,
                updated_at=excluded.updated_at
            """,
            (
                execution.account_id,
                execution.exchange,
                execution.account_id,
                now,
                now,
            ),
        )
        # Later fills never silently re-enable an intentionally removed account.
        con.execute(
            """
            INSERT INTO v2_portfolio_memberships(
                portfolio_id, account_id, included, active_from, active_until
            ) VALUES (?, ?, 1, ?, NULL)
            ON CONFLICT(portfolio_id, account_id) DO NOTHING
            """,
            (DEFAULT_PORTFOLIO_ID, execution.account_id, now),
        )

    @staticmethod
    def _insert_execution(
        con: sqlite3.Connection,
        execution: Execution,
    ) -> bool:
        cursor = con.execute(
            """
            INSERT OR IGNORE INTO v2_executions(
                execution_uid, account_id, exchange, market_key,
                position_side, native_trade_id, occurred_at, side,
                quantity, price, fee, ingested_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                execution.execution_uid,
                execution.account_id,
                execution.exchange,
                execution.market_key,
                execution.position_side.value,
                execution.native_trade_id,
                execution.occurred_at.isoformat(),
                execution.side.value,
                str(execution.quantity),
                str(execution.price),
                str(execution.fee),
                _now(),
            ),
        )
        return cursor.rowcount == 1

    @staticmethod
    def _execution(row: sqlite3.Row) -> Execution:
        return Execution(
            execution_uid=row["execution_uid"],
            account_id=row["account_id"],
            exchange=row["exchange"],
            market_key=row["market_key"],
            position_side=PositionSide(row["position_side"]),
            native_trade_id=row["native_trade_id"],
            occurred_at=_time(row["occurred_at"]),  # type: ignore[arg-type]
            side=ExecutionSide(row["side"]),
            quantity=Decimal(row["quantity"]),
            price=Decimal(row["price"]),
            fee=Decimal(row["fee"]),
        )

    @classmethod
    def _list_executions_con(
        cls,
        con: sqlite3.Connection,
        account_ids: set[str] | frozenset[str] | None = None,
    ) -> list[Execution]:
        if account_ids is not None and not account_ids:
            return []
        sql = "SELECT * FROM v2_executions"
        params: tuple[str, ...] = ()
        if account_ids is not None:
            ordered_ids = tuple(sorted(account_ids))
            sql += f" WHERE account_id IN ({','.join('?' for _ in ordered_ids)})"
            params = ordered_ids
        sql += " ORDER BY occurred_at, execution_uid"
        return [cls._execution(row) for row in con.execute(sql, params)]

    @staticmethod
    def _replace_projection(
        con: sqlite3.Connection,
        projection: AccountProjection,
    ) -> None:
        con.execute(
            "DELETE FROM v2_realizations WHERE account_id=?",
            (projection.account_id,),
        )
        con.execute(
            "DELETE FROM v2_lifecycles WHERE account_id=?",
            (projection.account_id,),
        )
        for item in projection.realizations:
            con.execute(
                """
                INSERT INTO v2_realizations(
                    realization_uid, execution_uid, lifecycle_uid, account_id,
                    market_key, position_side, direction, occurred_at,
                    quantity, entry_price, exit_price, gross_pnl, fees,
                    funding, net_pnl, kind, accounting_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.realization_uid,
                    item.execution_uid,
                    item.lifecycle_uid,
                    item.account_id,
                    item.market_key,
                    item.position_side.value,
                    item.direction.value,
                    item.occurred_at.isoformat(),
                    str(item.quantity),
                    str(item.entry_price),
                    str(item.exit_price),
                    str(item.gross_pnl),
                    str(item.fees),
                    str(item.funding),
                    str(item.net_pnl),
                    item.kind,
                    ACCOUNTING_VERSION,
                ),
            )
        for item in projection.lifecycles:
            con.execute(
                """
                INSERT INTO v2_lifecycles(
                    lifecycle_uid, account_id, position_key, market_key,
                    position_side, direction, opened_at, closed_at, status,
                    holding_duration_ms, holding_duration_basis,
                    entry_vwap, exit_vwap, max_quantity, closed_quantity,
                    gross_pnl, fees, funding, realized_pnl,
                    execution_uids_json, realization_uids_json,
                    accounting_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.lifecycle_uid,
                    item.account_id,
                    item.position_key,
                    item.market_key,
                    item.position_side.value,
                    item.direction.value,
                    item.opened_at.isoformat(),
                    item.closed_at.isoformat() if item.closed_at else None,
                    item.status.value,
                    item.holding_duration_ms,
                    item.holding_duration_basis,
                    str(item.entry_vwap),
                    str(item.exit_vwap) if item.exit_vwap is not None else None,
                    str(item.max_quantity),
                    str(item.closed_quantity),
                    str(item.gross_pnl),
                    str(item.fees),
                    str(item.funding),
                    str(item.realized_pnl),
                    json.dumps(item.execution_uids, separators=(",", ":")),
                    json.dumps(item.realization_uids, separators=(",", ":")),
                    ACCOUNTING_VERSION,
                ),
            )

    def ingest_execution(self, execution: Execution) -> bool:
        """Atomically append one execution, rebuild its account, and enqueue feed."""
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._ensure_account(con, execution)
            if not self._insert_execution(con, execution):
                row = con.execute(
                    """
                    SELECT * FROM v2_executions WHERE execution_uid=?
                    """,
                    (execution.execution_uid,),
                ).fetchone()
                if row is None or self._execution(row) != execution:
                    raise ValueError(
                        f"execution UID collision: {execution.execution_uid}"
                    )
                return False
            account_executions = self._list_executions_con(
                con, {execution.account_id}
            )
            projection = project_account(
                execution.account_id, account_executions
            )
            self._replace_projection(con, projection)
            last = projection.executions[-1]
            now = _now()
            con.execute(
                """
                INSERT INTO v2_projection_checkpoints(
                    account_id, accounting_version, last_execution_at,
                    last_execution_uid, projected_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    accounting_version=excluded.accounting_version,
                    last_execution_at=excluded.last_execution_at,
                    last_execution_uid=excluded.last_execution_uid,
                    projected_at=excluded.projected_at
                """,
                (
                    execution.account_id,
                    ACCOUNTING_VERSION,
                    last.occurred_at.isoformat(),
                    last.execution_uid,
                    now,
                ),
            )
            event_uid = stable_uid(
                "projection_event",
                execution.execution_uid,
                ACCOUNTING_VERSION,
            )
            payload = json.dumps(
                {
                    "account_id": execution.account_id,
                    "execution_uid": execution.execution_uid,
                    "accounting_version": ACCOUNTING_VERSION,
                    "last_execution_uid": last.execution_uid,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            con.execute(
                """
                INSERT INTO v2_integration_outbox(
                    event_uid, topic, payload_json, created_at
                ) VALUES (?, 'account_projection_updated', ?, ?)
                """,
                (event_uid, payload, now),
            )
        return True

    def list_executions(
        self,
        *,
        account_ids: set[str] | frozenset[str] | None = None,
    ) -> list[Execution]:
        with self.connect() as con:
            return self._list_executions_con(con, account_ids)

    def list_realizations(
        self,
        *,
        account_ids: set[str] | frozenset[str] | None = None,
    ) -> list[Realization]:
        if account_ids is not None and not account_ids:
            return []
        sql = "SELECT * FROM v2_realizations"
        params: tuple[str, ...] = ()
        if account_ids is not None:
            ordered = tuple(sorted(account_ids))
            sql += f" WHERE account_id IN ({','.join('?' for _ in ordered)})"
            params = ordered
        sql += " ORDER BY occurred_at, realization_uid"
        with self.connect() as con:
            return [
                Realization(
                    realization_uid=row["realization_uid"],
                    execution_uid=row["execution_uid"],
                    lifecycle_uid=row["lifecycle_uid"],
                    account_id=row["account_id"],
                    market_key=row["market_key"],
                    position_side=PositionSide(row["position_side"]),
                    direction=PositionDirection(row["direction"]),
                    occurred_at=_time(row["occurred_at"]),  # type: ignore[arg-type]
                    quantity=Decimal(row["quantity"]),
                    entry_price=Decimal(row["entry_price"]),
                    exit_price=Decimal(row["exit_price"]),
                    gross_pnl=Decimal(row["gross_pnl"]),
                    fees=Decimal(row["fees"]),
                    funding=Decimal(row["funding"]),
                    net_pnl=Decimal(row["net_pnl"]),
                    kind=row["kind"],
                )
                for row in con.execute(sql, params)
            ]

    def list_lifecycles(
        self,
        *,
        account_ids: set[str] | frozenset[str] | None = None,
    ) -> list[Lifecycle]:
        if account_ids is not None and not account_ids:
            return []
        sql = "SELECT * FROM v2_lifecycles"
        params: tuple[str, ...] = ()
        if account_ids is not None:
            ordered = tuple(sorted(account_ids))
            sql += f" WHERE account_id IN ({','.join('?' for _ in ordered)})"
            params = ordered
        sql += " ORDER BY opened_at, lifecycle_uid"
        with self.connect() as con:
            return [
                Lifecycle(
                    lifecycle_uid=row["lifecycle_uid"],
                    account_id=row["account_id"],
                    position_key=row["position_key"],
                    market_key=row["market_key"],
                    position_side=PositionSide(row["position_side"]),
                    direction=PositionDirection(row["direction"]),
                    opened_at=_time(row["opened_at"]),  # type: ignore[arg-type]
                    closed_at=_time(row["closed_at"]),
                    holding_duration_ms=row["holding_duration_ms"],
                    holding_duration_basis=row["holding_duration_basis"] or "unavailable",
                    status=LifecycleStatus(row["status"]),
                    entry_vwap=Decimal(row["entry_vwap"]),
                    exit_vwap=(
                        Decimal(row["exit_vwap"])
                        if row["exit_vwap"] is not None
                        else None
                    ),
                    max_quantity=Decimal(row["max_quantity"]),
                    closed_quantity=Decimal(row["closed_quantity"]),
                    gross_pnl=Decimal(row["gross_pnl"]),
                    fees=Decimal(row["fees"]),
                    funding=Decimal(row["funding"]),
                    realized_pnl=Decimal(row["realized_pnl"]),
                    execution_uids=tuple(json.loads(row["execution_uids_json"])),
                    realization_uids=tuple(
                        json.loads(row["realization_uids_json"])
                    ),
                )
                for row in con.execute(sql, params)
            ]

    def set_membership(
        self,
        portfolio_id: str,
        account_id: str,
        *,
        included: bool,
    ) -> None:
        portfolio = require_identity(portfolio_id, "portfolio_id")
        account = require_identity(account_id, "account_id")
        now = _now()
        with self.connect() as con:
            exists = con.execute(
                "SELECT 1 FROM v2_accounts WHERE account_id=?", (account,)
            ).fetchone()
            if not exists:
                raise ValueError(f"unknown account: {account}")
            con.execute(
                """
                INSERT INTO v2_portfolios(
                    portfolio_id, display_name, created_at, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(portfolio_id) DO UPDATE SET updated_at=excluded.updated_at
                """,
                (portfolio, portfolio, now, now),
            )
            con.execute(
                """
                INSERT INTO v2_portfolio_memberships(
                    portfolio_id, account_id, included, active_from, active_until
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(portfolio_id, account_id) DO UPDATE SET
                    included=excluded.included,
                    active_until=excluded.active_until
                """,
                (
                    portfolio,
                    account,
                    int(included),
                    now,
                    None if included else now,
                ),
            )

    def list_included_accounts(self, portfolio_id: str) -> set[str]:
        with self.connect() as con:
            return {
                row["account_id"]
                for row in con.execute(
                    """
                    SELECT account_id FROM v2_portfolio_memberships
                    WHERE portfolio_id=? AND included=1
                    """,
                    (portfolio_id,),
                )
            }

    def get_checkpoint(self, account_id: str) -> ProjectionCheckpoint | None:
        with self.connect() as con:
            row = con.execute(
                """
                SELECT * FROM v2_projection_checkpoints WHERE account_id=?
                """,
                (account_id,),
            ).fetchone()
        if not row:
            return None
        return ProjectionCheckpoint(
            account_id=row["account_id"],
            accounting_version=row["accounting_version"],
            last_execution_at=_time(row["last_execution_at"]),  # type: ignore[arg-type]
            last_execution_uid=row["last_execution_uid"],
            projected_at=_time(row["projected_at"]),  # type: ignore[arg-type]
        )

    def pending_outbox(self, *, limit: int = 100) -> list[OutboxItem]:
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT sequence_id, event_uid, topic, payload_json,
                       created_at, attempts
                FROM v2_integration_outbox
                WHERE delivered_at IS NULL
                ORDER BY sequence_id
                LIMIT ?
                """,
                (max(0, int(limit)),),
            ).fetchall()
        return [
            OutboxItem(
                sequence_id=row["sequence_id"],
                event_uid=row["event_uid"],
                topic=row["topic"],
                payload_json=row["payload_json"],
                created_at=_time(row["created_at"]),  # type: ignore[arg-type]
                attempts=row["attempts"],
            )
            for row in rows
        ]

    def mark_outbox_delivered(self, event_uid: str) -> bool:
        with self.connect() as con:
            cursor = con.execute(
                """
                UPDATE v2_integration_outbox
                SET delivered_at=?, attempts=attempts+1
                WHERE event_uid=? AND delivered_at IS NULL
                """,
                (_now(), event_uid),
            )
            return cursor.rowcount == 1

    def count(self, table: str) -> int:
        if table not in self._COUNTABLE:
            raise ValueError("unsupported table")
        with self.connect() as con:
            return int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
