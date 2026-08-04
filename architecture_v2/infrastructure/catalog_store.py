"""Central account/catalog database.

The catalog is intentionally separate from account ledgers.  It owns stable
identity, presentation labels, and consumer state; it never stores exchange
fills.  That separation lets a wallet be renamed, muted, or removed from a
portfolio without rewriting historical facts.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from architecture_v2.domain.identity import require_identity
from architecture_v2.domain.policy import AccountLabel, AccountState


UTC = timezone.utc


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _label(value: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError("label must not be blank")
    return normalized


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


@dataclass(frozen=True, slots=True)
class CatalogAccount:
    account_id: str
    exchange: str
    label: str
    state: AccountState
    created_at: datetime
    updated_at: datetime


class CatalogStore:
    """Repository for the central account/catalog SQLite file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        return con

    def init(self) -> None:
        with self.connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS catalog_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS catalog_accounts (
                    account_id TEXT PRIMARY KEY,
                    exchange TEXT NOT NULL,
                    ingestion_enabled INTEGER NOT NULL CHECK (ingestion_enabled IN (0, 1)),
                    alerts_enabled INTEGER NOT NULL CHECK (alerts_enabled IN (0, 1)),
                    portfolio_included INTEGER NOT NULL CHECK (portfolio_included IN (0, 1)),
                    historical_visible INTEGER NOT NULL CHECK (historical_visible IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS account_labels (
                    account_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    valid_from TEXT NOT NULL,
                    valid_until TEXT,
                    changed_by TEXT NOT NULL,
                    change_reason TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (account_id, valid_from),
                    FOREIGN KEY (account_id) REFERENCES catalog_accounts(account_id)
                );
                CREATE INDEX IF NOT EXISTS ix_account_labels_lookup
                    ON account_labels(account_id, valid_from, valid_until);
                CREATE TABLE IF NOT EXISTS catalog_portfolios (
                    portfolio_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS catalog_portfolio_memberships (
                    portfolio_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    included INTEGER NOT NULL CHECK (included IN (0, 1)),
                    active_from TEXT NOT NULL,
                    active_until TEXT,
                    PRIMARY KEY (portfolio_id, account_id),
                    FOREIGN KEY (portfolio_id) REFERENCES catalog_portfolios(portfolio_id),
                    FOREIGN KEY (account_id) REFERENCES catalog_accounts(account_id)
                );
                INSERT INTO catalog_meta(key, value) VALUES ('schema_version', '1')
                ON CONFLICT(key) DO UPDATE SET value=excluded.value;
                """
            )

    @staticmethod
    def _account(row: sqlite3.Row) -> CatalogAccount:
        return CatalogAccount(
            account_id=row["account_id"],
            exchange=row["exchange"],
            label=row["label"],
            state=AccountState(
                ingestion_enabled=bool(row["ingestion_enabled"]),
                alerts_enabled=bool(row["alerts_enabled"]),
                portfolio_included=bool(row["portfolio_included"]),
                historical_visible=bool(row["historical_visible"]),
            ),
            created_at=_time(row["created_at"]),
            updated_at=_time(row["updated_at"]),
        )

    def register_account(
        self,
        account_id: str,
        *,
        exchange: str,
        label: str,
        state: AccountState | None = None,
        at: datetime | None = None,
        changed_by: str = "system",
        change_reason: str = "initial registration",
    ) -> CatalogAccount:
        account = require_identity(account_id, "account_id")
        venue = require_identity(exchange, "exchange").lower()
        presentation = _label(label)
        current = at or _now()
        chosen = state or AccountState()
        when = _iso(current)
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO catalog_accounts(
                    account_id, exchange, ingestion_enabled, alerts_enabled,
                    portfolio_included, historical_visible, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    exchange=excluded.exchange,
                    updated_at=excluded.updated_at
                """,
                (
                    account, venue, int(chosen.ingestion_enabled), int(chosen.alerts_enabled),
                    int(chosen.portfolio_included), int(chosen.historical_visible), when, when,
                ),
            )
            existing = con.execute(
                "SELECT 1 FROM account_labels WHERE account_id=? AND valid_until IS NULL",
                (account,),
            ).fetchone()
            if existing is None:
                con.execute(
                    """INSERT INTO account_labels(
                        account_id, label, valid_from, valid_until, changed_by, change_reason
                    ) VALUES (?, ?, ?, NULL, ?, ?)""",
                    (account, presentation, when, changed_by, change_reason),
                )
            elif self._current_label_con(con, account, current) != presentation:
                self._rename_con(
                    con, account, presentation, current, changed_by, change_reason,
                )
        return self.get_account(account)  # type: ignore[return-value]

    def _current_label_con(
        self, con: sqlite3.Connection, account_id: str, at: datetime,
    ) -> str | None:
        row = con.execute(
            """SELECT label FROM account_labels
               WHERE account_id=? AND valid_from <= ?
               AND (valid_until IS NULL OR valid_until > ?)
               ORDER BY valid_from DESC LIMIT 1""",
            (account_id, _iso(at), _iso(at)),
        ).fetchone()
        return row["label"] if row else None

    def _rename_con(
        self, con: sqlite3.Connection, account_id: str, label: str,
        at: datetime, changed_by: str, reason: str,
    ) -> None:
        when = _iso(at)
        con.execute(
            "UPDATE account_labels SET valid_until=? WHERE account_id=? AND valid_until IS NULL",
            (when, account_id),
        )
        con.execute(
            """INSERT INTO account_labels(
                account_id, label, valid_from, valid_until, changed_by, change_reason
            ) VALUES (?, ?, ?, NULL, ?, ?)""",
            (account_id, _label(label), when, changed_by, reason),
        )

    def rename_account(
        self, account_id: str, label: str, *, at: datetime | None = None,
        changed_by: str = "system", change_reason: str = "",
    ) -> None:
        account = require_identity(account_id, "account_id")
        current = at or _now()
        with self.connect() as con:
            if not con.execute("SELECT 1 FROM catalog_accounts WHERE account_id=?", (account,)).fetchone():
                raise ValueError(f"unknown account: {account}")
            old = self._current_label_con(con, account, current)
            if old == label:
                return
            self._rename_con(con, account, label, current, changed_by, change_reason)
            con.execute("UPDATE catalog_accounts SET updated_at=? WHERE account_id=?", (_iso(current), account))

    def set_state(self, account_id: str, *, at: datetime | None = None, **changes: bool) -> CatalogAccount:
        account = require_identity(account_id, "account_id")
        allowed = {"ingestion_enabled", "alerts_enabled", "portfolio_included", "historical_visible"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unknown account state field(s): {', '.join(sorted(unknown))}")
        if not changes:
            return self.get_account(account)  # type: ignore[return-value]
        if any(not isinstance(value, bool) for value in changes.values()):
            raise TypeError("account state values must be bool")
        current = at or _now()
        assignments = ", ".join(f"{key}=?" for key in sorted(changes))
        values = [int(changes[key]) for key in sorted(changes)] + [_iso(current), account]
        with self.connect() as con:
            cursor = con.execute(
                f"UPDATE catalog_accounts SET {assignments}, updated_at=? WHERE account_id=?",
                values,
            )
            if cursor.rowcount != 1:
                raise ValueError(f"unknown account: {account}")
        return self.get_account(account)  # type: ignore[return-value]

    def get_account(self, account_id: str) -> CatalogAccount | None:
        account = require_identity(account_id, "account_id")
        with self.connect() as con:
            row = con.execute(
                """SELECT a.*, COALESCE((SELECT label FROM account_labels l
                   WHERE l.account_id=a.account_id AND l.valid_until IS NULL
                   ORDER BY l.valid_from DESC LIMIT 1), a.account_id) AS label
                   FROM catalog_accounts a WHERE a.account_id=?""",
                (account,),
            ).fetchone()
        return self._account(row) if row else None

    def list_accounts(self, *, historical_visible: bool | None = None) -> tuple[CatalogAccount, ...]:
        sql = """SELECT a.*, COALESCE((SELECT label FROM account_labels l
                   WHERE l.account_id=a.account_id AND l.valid_until IS NULL
                   ORDER BY l.valid_from DESC LIMIT 1), a.account_id) AS label
                   FROM catalog_accounts a"""
        params: tuple[int, ...] = ()
        if historical_visible is not None:
            sql += " WHERE a.historical_visible=?"
            params = (int(historical_visible),)
        sql += " ORDER BY a.account_id"
        with self.connect() as con:
            return tuple(self._account(row) for row in con.execute(sql, params))

    def label_history(self, account_id: str) -> tuple[AccountLabel, ...]:
        account = require_identity(account_id, "account_id")
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM account_labels WHERE account_id=? ORDER BY valid_from",
                (account,),
            ).fetchall()
        return tuple(
            AccountLabel(
                account_id=row["account_id"], label=row["label"],
                valid_from=_time(row["valid_from"]),
                valid_until=_time(row["valid_until"]) if row["valid_until"] else None,
                changed_by=row["changed_by"], change_reason=row["change_reason"],
            )
            for row in rows
        )

    def label_at(self, account_id: str, at: datetime) -> str | None:
        account = require_identity(account_id, "account_id")
        when = _iso(at)
        with self.connect() as con:
            row = con.execute(
                """SELECT label FROM account_labels WHERE account_id=?
                   AND valid_from <= ? AND (valid_until IS NULL OR valid_until > ?)
                   ORDER BY valid_from DESC LIMIT 1""",
                (account, when, when),
            ).fetchone()
        return row["label"] if row else None

    def set_portfolio_inclusion(
        self, portfolio_id: str, account_id: str, *, included: bool,
        display_name: str | None = None, at: datetime | None = None,
    ) -> None:
        portfolio = require_identity(portfolio_id, "portfolio_id")
        account = require_identity(account_id, "account_id")
        current = at or _now()
        when = _iso(current)
        with self.connect() as con:
            if not con.execute("SELECT 1 FROM catalog_accounts WHERE account_id=?", (account,)).fetchone():
                raise ValueError(f"unknown account: {account}")
            con.execute(
                """INSERT INTO catalog_portfolios(portfolio_id, display_name, created_at, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(portfolio_id) DO UPDATE SET updated_at=excluded.updated_at""",
                (portfolio, display_name or portfolio, when, when),
            )
            con.execute(
                """INSERT INTO catalog_portfolio_memberships(
                   portfolio_id, account_id, included, active_from, active_until)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(portfolio_id, account_id) DO UPDATE SET
                     included=excluded.included, active_until=excluded.active_until""",
                (portfolio, account, int(included), when, None if included else when),
            )

    def included_accounts(self, portfolio_id: str) -> frozenset[str]:
        portfolio = require_identity(portfolio_id, "portfolio_id")
        with self.connect() as con:
            rows = con.execute(
                "SELECT account_id FROM catalog_portfolio_memberships WHERE portfolio_id=? AND included=1",
                (portfolio,),
            )
        return frozenset(row["account_id"] for row in rows)
