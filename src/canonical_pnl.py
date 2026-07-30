"""Canonical per-account P&L ledger and portfolio aggregation.

This module is the accounting boundary for Trade Tracker:

* each wallet/pool/exchange account has a permanent ``account_id``;
* ledger entries are append-only and partitioned by that account;
* portfolio membership is independent from ledger history;
* removing an account disables membership without deleting its ledger;
* the portfolio projector composes account projections instead of rebuilding
  accounting rules separately for every dashboard.

The first schema version adapts the existing ``closed_trades`` realization
records into the canonical ledger.  Component-level fees and funding remain
unknown when the legacy record did not preserve them; unknown is never changed
to zero.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


DEFAULT_PORTFOLIO_ID = "default"
SCHEMA_VERSION = "1"
ZERO = Decimal("0")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _legacy_account_id(source: str, exchange: str) -> str:
    """Return a deterministic temporary ID for rows predating source IDs."""
    identity = f"{exchange.lower()}|{source.strip().lower()}".encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()[:20]
    return f"legacy:{exchange.lower() or 'unknown'}:{digest}"


def canonical_account_id(
    record: Mapping[str, Any],
    aliases: Mapping[tuple[str, str], str] | None = None,
) -> str:
    """Resolve a stable account ID without treating a display label as truth."""
    source_id = _clean(record.get("source_id"))
    if source_id:
        return source_id
    source = _clean(record.get("source"))
    exchange = _clean(record.get("exchange")).lower()
    alias_map = aliases or {}
    return (
        alias_map.get((source, exchange))
        or alias_map.get((source, ""))
        or _legacy_account_id(source, exchange)
    )


def init_canonical_schema(connection: sqlite3.Connection) -> None:
    """Create the additive canonical-ledger schema in an existing database."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS canonical_accounts (
            account_id      TEXT PRIMARY KEY,
            exchange        TEXT NOT NULL DEFAULT '',
            display_name    TEXT NOT NULL DEFAULT '',
            account_kind    TEXT NOT NULL DEFAULT 'trading',
            metadata_json   TEXT NOT NULL DEFAULT '{}',
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS canonical_portfolios (
            portfolio_id    TEXT PRIMARY KEY,
            display_name    TEXT NOT NULL,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS canonical_portfolio_members (
            portfolio_id    TEXT NOT NULL,
            account_id      TEXT NOT NULL,
            included        INTEGER NOT NULL CHECK (included IN (0, 1)),
            added_at        TEXT NOT NULL,
            removed_at      TEXT,
            PRIMARY KEY (portfolio_id, account_id),
            FOREIGN KEY (portfolio_id)
                REFERENCES canonical_portfolios(portfolio_id),
            FOREIGN KEY (account_id)
                REFERENCES canonical_accounts(account_id)
        );

        CREATE TABLE IF NOT EXISTS canonical_ledger_entries (
            sequence_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id         TEXT NOT NULL UNIQUE,
            account_id       TEXT NOT NULL,
            entry_kind       TEXT NOT NULL,
            occurred_at      TEXT NOT NULL,
            exchange         TEXT NOT NULL DEFAULT '',
            symbol           TEXT NOT NULL DEFAULT '',
            position_side    TEXT NOT NULL DEFAULT '',
            native_event_id  TEXT,
            source_table     TEXT NOT NULL,
            source_row_id    TEXT NOT NULL,
            payload_json     TEXT NOT NULL,
            ingested_at      TEXT NOT NULL,
            FOREIGN KEY (account_id)
                REFERENCES canonical_accounts(account_id),
            UNIQUE (source_table, source_row_id, entry_kind)
        );

        CREATE INDEX IF NOT EXISTS ix_canonical_ledger_account_time
            ON canonical_ledger_entries(account_id, occurred_at, sequence_id);
        CREATE INDEX IF NOT EXISTS ix_canonical_ledger_native_event
            ON canonical_ledger_entries(account_id, native_event_id)
            WHERE native_event_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS ix_canonical_members_included
            ON canonical_portfolio_members(portfolio_id, included, account_id);
        """
    )
    now = _utc_now()
    connection.execute(
        """
        INSERT INTO canonical_portfolios(
            portfolio_id, display_name, created_at, updated_at
        ) VALUES (?, ?, ?, ?)
        ON CONFLICT(portfolio_id) DO NOTHING
        """,
        (DEFAULT_PORTFOLIO_ID, "All tracked accounts", now, now),
    )
    connection.execute(
        """
        INSERT INTO schema_meta(key, value)
        VALUES('canonical_pnl_schema_version', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (SCHEMA_VERSION,),
    )


def _entry_id(source_table: str, source_row_id: str, entry_kind: str) -> str:
    raw = f"{source_table}|{source_row_id}|{entry_kind}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _upsert_account(
    connection: sqlite3.Connection,
    *,
    account_id: str,
    exchange: str,
    display_name: str,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    now = _utc_now()
    connection.execute(
        """
        INSERT INTO canonical_accounts(
            account_id, exchange, display_name, metadata_json,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(account_id) DO UPDATE SET
            exchange=CASE
                WHEN excluded.exchange <> '' THEN excluded.exchange
                ELSE canonical_accounts.exchange
            END,
            display_name=CASE
                WHEN excluded.display_name <> '' THEN excluded.display_name
                ELSE canonical_accounts.display_name
            END,
            metadata_json=excluded.metadata_json,
            updated_at=excluded.updated_at
        """,
        (
            account_id,
            exchange,
            display_name,
            json.dumps(dict(metadata or {}), sort_keys=True, separators=(",", ":")),
            now,
            now,
        ),
    )
    # New accounts participate by default.  A deliberately removed account is
    # not silently re-enabled by later ledger ingestion.
    connection.execute(
        """
        INSERT INTO canonical_portfolio_members(
            portfolio_id, account_id, included, added_at, removed_at
        ) VALUES (?, ?, 1, ?, NULL)
        ON CONFLICT(portfolio_id, account_id) DO NOTHING
        """,
        (DEFAULT_PORTFOLIO_ID, account_id, now),
    )


def write_closed_trade_ledger_entry(
    connection: sqlite3.Connection,
    row_id: int | str,
    record: Mapping[str, Any],
    aliases: Mapping[tuple[str, str], str] | None = None,
) -> bool:
    """Append one legacy realization to the canonical account ledger."""
    init_canonical_schema(connection)
    account_id = canonical_account_id(record, aliases)
    exchange = _clean(record.get("exchange")).lower()
    source_name = _clean(record.get("source"))
    _upsert_account(
        connection,
        account_id=account_id,
        exchange=exchange,
        display_name=source_name or account_id,
        metadata={"identity_origin": "source_id" if record.get("source_id") else "legacy_alias"},
    )
    payload = dict(record)
    payload["source_id"] = account_id
    payload["account_id"] = account_id
    has_components = any(
        record.get(field) is not None
        for field in ("gross_pnl", "fees", "fee", "funding", "funding_pnl")
    )
    payload["accounting_basis"] = (
        "component_breakdown" if has_components else "legacy_reported_pnl"
    )
    payload["gross_pnl"] = record.get("gross_pnl")
    payload["fees"] = (
        record.get("fees")
        if record.get("fees") is not None
        else record.get("fee")
    )
    payload["funding"] = (
        record.get("funding")
        if record.get("funding") is not None
        else record.get("funding_pnl")
    )
    payload["net_pnl"] = (
        record.get("net_pnl")
        if record.get("net_pnl") is not None
        else record.get("pnl")
    )
    source_row_id = str(row_id)
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO canonical_ledger_entries(
            entry_id, account_id, entry_kind, occurred_at, exchange,
            symbol, position_side, native_event_id, source_table,
            source_row_id, payload_json, ingested_at
        ) VALUES (?, ?, 'trade_realization', ?, ?, ?, ?, ?, 'closed_trades', ?, ?, ?)
        """,
        (
            _entry_id("closed_trades", source_row_id, "trade_realization"),
            account_id,
            _clean(record.get("ts")) or _utc_now(),
            exchange,
            _clean(record.get("market_symbol")),
            _clean(record.get("position_side") or "BOTH"),
            _clean(record.get("event_uid") or record.get("native_trade_id")) or None,
            source_row_id,
            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str),
            _utc_now(),
        ),
    )
    return cursor.rowcount == 1


def retract_closed_trade_rows(
    connection: sqlite3.Connection,
    row_ids: Iterable[int | str],
    *,
    reason: str,
) -> int:
    """Append retractions for legacy rows before a repair deletes/rebuilds them."""
    init_canonical_schema(connection)
    inserted = 0
    for row_id in row_ids:
        source_row_id = str(row_id)
        original = connection.execute(
            """
            SELECT entry_id, account_id, occurred_at, exchange, symbol,
                   position_side, native_event_id
            FROM canonical_ledger_entries
            WHERE source_table='closed_trades'
              AND source_row_id=?
              AND entry_kind='trade_realization'
            """,
            (source_row_id,),
        ).fetchone()
        # Repair scripts may run before the dashboard's one-time backfill.
        # Adapt the row first so even that path remains append-only and auditable.
        if original is None:
            cursor = connection.execute(
                "SELECT * FROM closed_trades WHERE id=?",
                (source_row_id,),
            )
            legacy_row = cursor.fetchone()
            if legacy_row is not None:
                columns = [item[0] for item in cursor.description]
                write_closed_trade_ledger_entry(
                    connection,
                    source_row_id,
                    dict(zip(columns, legacy_row)),
                )
                original = connection.execute(
                    """
                    SELECT entry_id, account_id, occurred_at, exchange, symbol,
                           position_side, native_event_id
                    FROM canonical_ledger_entries
                    WHERE source_table='closed_trades'
                      AND source_row_id=?
                      AND entry_kind='trade_realization'
                    """,
                    (source_row_id,),
                ).fetchone()
        if original is None:
            continue
        original_entry_id = str(original[0])
        retraction_row_id = f"{source_row_id}:{original_entry_id}"
        payload = json.dumps(
            {"retracts_entry_id": original_entry_id, "reason": reason},
            sort_keys=True,
            separators=(",", ":"),
        )
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO canonical_ledger_entries(
                entry_id, account_id, entry_kind, occurred_at, exchange,
                symbol, position_side, native_event_id, source_table,
                source_row_id, payload_json, ingested_at
            ) VALUES (?, ?, 'retraction', ?, ?, ?, ?, ?, 'closed_trades_retraction', ?, ?, ?)
            """,
            (
                _entry_id(
                    "closed_trades_retraction", retraction_row_id, "retraction"
                ),
                original[1],
                _utc_now(),
                original[3],
                original[4],
                original[5],
                original[6],
                retraction_row_id,
                payload,
                _utc_now(),
            ),
        )
        inserted += int(cursor.rowcount == 1)
    return inserted


def _backfill_sync(
    path: Path,
    aliases: Mapping[tuple[str, str], str] | None,
) -> dict[str, int]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        init_canonical_schema(connection)
        rows = connection.execute(
            "SELECT * FROM closed_trades ORDER BY id"
        ).fetchall()
        inserted = sum(
            write_closed_trade_ledger_entry(
                connection, row["id"], dict(row), aliases
            )
            for row in rows
        )
        connection.commit()
        return {"scanned": len(rows), "inserted": inserted}
    finally:
        connection.close()


async def backfill_canonical_ledger(
    path: Path,
    aliases: Mapping[tuple[str, str], str] | None = None,
) -> dict[str, int]:
    """Idempotently adapt all current closed-trade rows into the ledger."""
    return await asyncio.to_thread(_backfill_sync, path, aliases)


def _sync_membership_sync(
    path: Path,
    accounts: Sequence[Mapping[str, Any]],
    portfolio_id: str,
) -> dict[str, int]:
    connection = sqlite3.connect(path)
    try:
        init_canonical_schema(connection)
        now = _utc_now()
        connection.execute(
            """
            INSERT INTO canonical_portfolios(
                portfolio_id, display_name, created_at, updated_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(portfolio_id) DO UPDATE SET updated_at=excluded.updated_at
            """,
            (portfolio_id, portfolio_id, now, now),
        )
        active_ids: list[str] = []
        for account in accounts:
            account_id = _clean(account.get("account_id") or account.get("id"))
            if not account_id:
                raise ValueError("every portfolio account requires a permanent account_id")
            active_ids.append(account_id)
            _upsert_account(
                connection,
                account_id=account_id,
                exchange=_clean(account.get("exchange")).lower(),
                display_name=_clean(account.get("display_name") or account.get("name"))
                or account_id,
                metadata={"identity_origin": "configured_source"},
            )
            connection.execute(
                """
                INSERT INTO canonical_portfolio_members(
                    portfolio_id, account_id, included, added_at, removed_at
                ) VALUES (?, ?, 1, ?, NULL)
                ON CONFLICT(portfolio_id, account_id) DO UPDATE SET
                    included=1, removed_at=NULL
                """,
                (portfolio_id, account_id, now),
            )

        if active_ids:
            placeholders = ",".join("?" for _ in active_ids)
            cursor = connection.execute(
                f"""
                UPDATE canonical_portfolio_members
                SET included=0, removed_at=?
                WHERE portfolio_id=? AND included=1
                  AND account_id NOT IN ({placeholders})
                """,
                (now, portfolio_id, *active_ids),
            )
        else:
            cursor = connection.execute(
                """
                UPDATE canonical_portfolio_members
                SET included=0, removed_at=?
                WHERE portfolio_id=? AND included=1
                """,
                (now, portfolio_id),
            )
        connection.commit()
        return {"included": len(active_ids), "removed": max(cursor.rowcount, 0)}
    finally:
        connection.close()


async def sync_portfolio_membership(
    path: Path,
    accounts: Sequence[Mapping[str, Any]],
    *,
    portfolio_id: str = DEFAULT_PORTFOLIO_ID,
) -> dict[str, int]:
    """Make portfolio membership match configuration without deleting history."""
    return await asyncio.to_thread(
        _sync_membership_sync, path, accounts, portfolio_id
    )


def _load_realizations_sync(
    path: Path,
    portfolio_id: str,
    limit: int | None,
) -> list[dict[str, Any]]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        init_canonical_schema(connection)
        rows = connection.execute(
            """
            SELECT e.sequence_id, e.entry_id, e.entry_kind, e.payload_json
            FROM canonical_ledger_entries e
            JOIN canonical_portfolio_members m
              ON m.account_id=e.account_id
             AND m.portfolio_id=?
             AND m.included=1
            ORDER BY e.sequence_id
            """,
            (portfolio_id,),
        ).fetchall()
        connection.commit()
    finally:
        connection.close()

    retracted: set[str] = set()
    realizations: list[tuple[int, str, dict[str, Any]]] = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        if row["entry_kind"] == "retraction":
            target = _clean(payload.get("retracts_entry_id"))
            if target:
                retracted.add(target)
        elif row["entry_kind"] == "trade_realization":
            realizations.append((row["sequence_id"], row["entry_id"], payload))

    active = [
        (sequence_id, payload)
        for sequence_id, entry_id, payload in realizations
        if entry_id not in retracted
    ]
    active.sort(key=lambda item: item[0], reverse=True)
    if limit is not None:
        active = active[:limit]
    return [payload for _, payload in active]


async def load_canonical_realizations(
    path: Path,
    limit: int | None = None,
    *,
    portfolio_id: str = DEFAULT_PORTFOLIO_ID,
) -> list[dict[str, Any]]:
    return await asyncio.to_thread(
        _load_realizations_sync, path, portfolio_id, limit
    )


@dataclass(frozen=True)
class AccountProjection:
    account_id: str
    trades: tuple[dict[str, Any], ...]
    known_net_pnl: Decimal
    unknown_pnl_trades: int


@dataclass(frozen=True)
class PortfolioProjection:
    portfolio_id: str
    accounts: tuple[AccountProjection, ...]
    trades: tuple[dict[str, Any], ...]
    known_net_pnl: Decimal
    unknown_pnl_trades: int


def project_portfolio(
    realization_rows: Iterable[dict[str, Any]],
    *,
    portfolio_id: str = DEFAULT_PORTFOLIO_ID,
) -> PortfolioProjection:
    """Project accounts independently, then compose the portfolio view."""
    from .stats import aggregate_round_trips

    by_account: dict[str, list[dict[str, Any]]] = {}
    for row in realization_rows:
        account_id = _clean(row.get("account_id") or row.get("source_id"))
        if not account_id:
            account_id = canonical_account_id(row)
        by_account.setdefault(account_id, []).append(row)

    account_projections: list[AccountProjection] = []
    portfolio_trades: list[dict[str, Any]] = []
    for account_id in sorted(by_account):
        trades = aggregate_round_trips(by_account[account_id])
        known = ZERO
        unknown = 0
        for trade in trades:
            pnl = _decimal(trade.get("pnl"))
            if pnl is None:
                unknown += 1
            else:
                known += pnl
            trade["account_id"] = account_id
        account_projections.append(
            AccountProjection(account_id, tuple(trades), known, unknown)
        )
        portfolio_trades.extend(trades)

    portfolio_trades.sort(
        key=lambda row: _clean(row.get("ts")),
        reverse=True,
    )
    return PortfolioProjection(
        portfolio_id=portfolio_id,
        accounts=tuple(account_projections),
        trades=tuple(portfolio_trades),
        known_net_pnl=sum(
            (account.known_net_pnl for account in account_projections), ZERO
        ),
        unknown_pnl_trades=sum(
            account.unknown_pnl_trades for account in account_projections
        ),
    )


class CanonicalPnlService:
    """Single read handler shared by dashboards, reports, and future APIs."""

    def __init__(
        self,
        path: Path,
        *,
        portfolio_id: str = DEFAULT_PORTFOLIO_ID,
    ) -> None:
        self.path = path
        self.portfolio_id = portfolio_id

    async def realizations(self, limit: int | None = None) -> list[dict[str, Any]]:
        return await load_canonical_realizations(
            self.path, limit, portfolio_id=self.portfolio_id
        )

    async def projection(self) -> PortfolioProjection:
        rows = await self.realizations(None)
        return project_portfolio(rows, portfolio_id=self.portfolio_id)
