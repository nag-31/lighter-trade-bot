"""SQLite store and analytics for the Crypto Scientist Command Center."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from holding_time import holding_duration_ms


HORIZONS_MINUTES = (60, 360, 1440, 10080)
REASON_PRESETS = {
    "Setup": (
        "Breakout", "Breakdown", "Trend continuation", "Mean reversion",
        "Range trade", "Momentum scalp",
    ),
    "Trigger": (
        "Volume confirmation", "Support / resistance reaction",
        "Liquidation cascade", "Funding / open-interest signal",
        "On-chain flow", "Multi-source alert", "News or catalyst",
    ),
    "Market context": (
        "Risk-on regime", "Risk-off regime", "High volatility",
        "Low liquidity", "Correlated market move", "Protocol incident",
    ),
    "Execution": (
        "Planned entry", "Scale-in", "Hedge", "Re-entry",
        "Portfolio rebalance",
    ),
    "Psychology": (
        "High conviction", "FOMO", "Revenge trade", "Boredom trade",
        "Fear-driven decision",
    ),
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    return (value or utc_now()).astimezone(timezone.utc).isoformat()


def parse_time(value: str | int | float) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)


def fingerprint(*parts: Any) -> str:
    raw = "|".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class CommandStore:
    def __init__(self, path: Path, journal_path: Path | None = None):
        self.path = Path(path)
        self.journal_path = Path(
            journal_path or self.path.with_name("trading_journal.db")
        )

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=20)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("ATTACH DATABASE ? AS journal", (str(self.journal_path),))
        return con

    def init(self) -> None:
        with self.connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT NOT NULL UNIQUE,
                    source TEXT NOT NULL,
                    source_ref TEXT,
                    detector TEXT,
                    event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    ingested_at TEXT NOT NULL,
                    symbol TEXT,
                    direction TEXT NOT NULL DEFAULT 'neutral',
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    severity TEXT NOT NULL DEFAULT 'medium',
                    confidence REAL,
                    status TEXT NOT NULL DEFAULT 'new',
                    is_simulation INTEGER NOT NULL DEFAULT 0,
                    baseline_value REAL,
                    observed_value REAL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_signals_time ON signals(occurred_at DESC);
                CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status, occurred_at DESC);

                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    thesis TEXT NOT NULL,
                    direction TEXT NOT NULL DEFAULT 'neutral',
                    entry REAL,
                    invalidation REAL,
                    target REAL,
                    max_risk_usd REAL,
                    confidence INTEGER NOT NULL DEFAULT 50,
                    status TEXT NOT NULL DEFAULT 'planned',
                    notes TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(signal_id) REFERENCES signals(id) ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions(status, updated_at DESC);

                CREATE TABLE IF NOT EXISTS journal_reasons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reason_key TEXT NOT NULL UNIQUE,
                    category TEXT NOT NULL,
                    label TEXT NOT NULL,
                    is_custom INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_journal_reasons_category
                ON journal_reasons(category, label);

                CREATE TABLE IF NOT EXISTS decision_reasons (
                    decision_id INTEGER NOT NULL,
                    reason_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(decision_id, reason_id),
                    FOREIGN KEY(decision_id) REFERENCES decisions(id) ON DELETE CASCADE,
                    FOREIGN KEY(reason_id) REFERENCES journal_reasons(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT NOT NULL UNIQUE,
                    source TEXT NOT NULL,
                    native_trade_id TEXT,
                    occurred_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry REAL,
                    exit REAL,
                    size REAL,
                    notional REAL,
                    pnl REAL,
                    pnl_pct REAL,
                    is_win INTEGER,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_trades_time ON trades(occurred_at DESC);

                CREATE TABLE IF NOT EXISTS trade_lifecycles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lifecycle_key TEXT NOT NULL UNIQUE,
                    source TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT,
                    status TEXT NOT NULL,
                    entry_vwap REAL,
                    exit_vwap REAL,
                    max_size REAL,
                    closed_size REAL,
                    notional REAL,
                    pnl REAL,
                    pnl_pct REAL,
                    is_win INTEGER,
                    fill_count INTEGER NOT NULL DEFAULT 0,
                    entry_batch_count INTEGER NOT NULL DEFAULT 0,
                    exit_batch_count INTEGER NOT NULL DEFAULT 0,
                    partial_exit_count INTEGER NOT NULL DEFAULT 0,
                    management_style TEXT NOT NULL DEFAULT '',
                    holding_duration_ms INTEGER,
                    holding_duration_basis TEXT NOT NULL DEFAULT 'unavailable',
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_trade_lifecycles_time
                ON trade_lifecycles(opened_at DESC);
                CREATE INDEX IF NOT EXISTS idx_trade_lifecycles_holding
                ON trade_lifecycles(status, holding_duration_ms, closed_at);
                CREATE INDEX IF NOT EXISTS idx_trade_lifecycles_analysis
                ON trade_lifecycles(source, symbol, side, status, closed_at);

                CREATE TABLE IF NOT EXISTS trade_executions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lifecycle_id INTEGER NOT NULL,
                    execution_key TEXT NOT NULL UNIQUE,
                    batch_key TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    action TEXT NOT NULL,
                    batch_label TEXT NOT NULL,
                    price REAL,
                    size REAL NOT NULL,
                    pnl REAL,
                    position_before REAL,
                    position_after REAL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(lifecycle_id) REFERENCES trade_lifecycles(id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_trade_executions_lifecycle
                ON trade_executions(lifecycle_id, occurred_at);

                CREATE TABLE IF NOT EXISTS current_positions (
                    position_key TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    size REAL NOT NULL,
                    entry REAL,
                    unrealized_pnl REAL,
                    liquidation_price REAL,
                    updated_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS trade_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    decision_id INTEGER NOT NULL,
                    trade_id INTEGER NOT NULL,
                    linked_at TEXT NOT NULL,
                    UNIQUE(decision_id, trade_id),
                    FOREIGN KEY(decision_id) REFERENCES decisions(id) ON DELETE CASCADE,
                    FOREIGN KEY(trade_id) REFERENCES trades(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS lifecycle_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    decision_id INTEGER NOT NULL,
                    lifecycle_id INTEGER NOT NULL,
                    linked_at TEXT NOT NULL,
                    UNIQUE(decision_id, lifecycle_id),
                    FOREIGN KEY(decision_id) REFERENCES decisions(id) ON DELETE CASCADE,
                    FOREIGN KEY(lifecycle_id) REFERENCES trade_lifecycles(id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id INTEGER NOT NULL,
                    horizon_minutes INTEGER NOT NULL,
                    due_at TEXT NOT NULL,
                    captured_at TEXT,
                    baseline_price REAL,
                    outcome_price REAL,
                    return_pct REAL,
                    signed_return_pct REAL,
                    mfe_pct REAL,
                    mae_pct REAL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(signal_id, horizon_minutes),
                    FOREIGN KEY(signal_id) REFERENCES signals(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT OR IGNORE INTO settings(key, value, updated_at)
                VALUES ('daily_risk_budget', '500', CURRENT_TIMESTAMP);
                INSERT OR IGNORE INTO settings(key, value, updated_at)
                VALUES ('max_open_decisions', '5', CURRENT_TIMESTAMP);

                CREATE TABLE IF NOT EXISTS sync_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    imported_signals INTEGER NOT NULL DEFAULT 0,
                    imported_trades INTEGER NOT NULL DEFAULT 0,
                    completed_outcomes INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                );
                """
            )
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS journal.decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    thesis TEXT NOT NULL,
                    direction TEXT NOT NULL DEFAULT 'neutral',
                    entry REAL,
                    invalidation REAL,
                    target REAL,
                    max_risk_usd REAL,
                    confidence INTEGER NOT NULL DEFAULT 50,
                    status TEXT NOT NULL DEFAULT 'planned',
                    notes TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS journal.idx_decisions_status
                ON decisions(status, updated_at DESC);

                CREATE TABLE IF NOT EXISTS journal.journal_reasons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reason_key TEXT NOT NULL UNIQUE,
                    category TEXT NOT NULL,
                    label TEXT NOT NULL,
                    is_custom INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS journal.idx_journal_reasons_category
                ON journal_reasons(category, label);

                CREATE TABLE IF NOT EXISTS journal.decision_reasons (
                    decision_id INTEGER NOT NULL,
                    reason_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(decision_id, reason_id),
                    FOREIGN KEY(decision_id) REFERENCES decisions(id) ON DELETE CASCADE,
                    FOREIGN KEY(reason_id) REFERENCES journal_reasons(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS journal.trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT NOT NULL UNIQUE,
                    source TEXT NOT NULL,
                    native_trade_id TEXT,
                    occurred_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry REAL,
                    exit REAL,
                    size REAL,
                    notional REAL,
                    pnl REAL,
                    pnl_pct REAL,
                    is_win INTEGER,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS journal.idx_trades_time
                ON trades(occurred_at DESC);

                CREATE TABLE IF NOT EXISTS journal.trade_lifecycles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lifecycle_key TEXT NOT NULL UNIQUE,
                    source TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT,
                    status TEXT NOT NULL,
                    entry_vwap REAL,
                    exit_vwap REAL,
                    max_size REAL,
                    closed_size REAL,
                    notional REAL,
                    pnl REAL,
                    pnl_pct REAL,
                    is_win INTEGER,
                    fill_count INTEGER NOT NULL DEFAULT 0,
                    entry_batch_count INTEGER NOT NULL DEFAULT 0,
                    exit_batch_count INTEGER NOT NULL DEFAULT 0,
                    partial_exit_count INTEGER NOT NULL DEFAULT 0,
                    management_style TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS journal.idx_trade_lifecycles_time
                ON trade_lifecycles(opened_at DESC);

                CREATE TABLE IF NOT EXISTS journal.trade_executions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lifecycle_id INTEGER NOT NULL,
                    execution_key TEXT NOT NULL UNIQUE,
                    batch_key TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    action TEXT NOT NULL,
                    batch_label TEXT NOT NULL,
                    price REAL,
                    size REAL NOT NULL,
                    pnl REAL,
                    position_before REAL,
                    position_after REAL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(lifecycle_id) REFERENCES trade_lifecycles(id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS journal.idx_trade_executions_lifecycle
                ON trade_executions(lifecycle_id, occurred_at);

                CREATE TABLE IF NOT EXISTS journal.current_positions (
                    position_key TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    size REAL NOT NULL,
                    entry REAL,
                    unrealized_pnl REAL,
                    liquidation_price REAL,
                    updated_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS journal.sync_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    imported_trades INTEGER NOT NULL DEFAULT 0,
                    imported_positions INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS journal.trade_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    decision_id INTEGER NOT NULL,
                    trade_id INTEGER NOT NULL,
                    linked_at TEXT NOT NULL,
                    UNIQUE(decision_id, trade_id),
                    FOREIGN KEY(decision_id) REFERENCES decisions(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS journal.lifecycle_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    decision_id INTEGER NOT NULL,
                    lifecycle_id INTEGER NOT NULL,
                    linked_at TEXT NOT NULL,
                    UNIQUE(decision_id, lifecycle_id),
                    FOREIGN KEY(decision_id) REFERENCES decisions(id) ON DELETE CASCADE
                );
                """
            )
            # Additive lifecycle-duration migration for existing databases.
            for schema in ("main", "journal"):
                columns = {
                    row["name"] for row in con.execute(
                        f"PRAGMA {schema}.table_info(trade_lifecycles)"
                    ).fetchall()
                }
                if "holding_duration_ms" not in columns:
                    con.execute(f"ALTER TABLE {schema}.trade_lifecycles ADD COLUMN holding_duration_ms INTEGER")
                if "holding_duration_basis" not in columns:
                    con.execute(f"ALTER TABLE {schema}.trade_lifecycles ADD COLUMN holding_duration_basis TEXT NOT NULL DEFAULT 'unavailable'")
                con.execute(f"CREATE INDEX IF NOT EXISTS {schema}.idx_trade_lifecycles_holding ON trade_lifecycles(status, holding_duration_ms, closed_at)")
                con.execute(f"CREATE INDEX IF NOT EXISTS {schema}.idx_trade_lifecycles_analysis ON trade_lifecycles(source, symbol, side, status, closed_at)")
                con.execute(
                    f"UPDATE {schema}.trade_lifecycles SET holding_duration_ms = "
                    "CAST(MAX(0, (julianday(closed_at)-julianday(opened_at))*86400000) AS INTEGER), "
                    "holding_duration_basis = CASE WHEN json_extract(metadata_json, '$.inferred_open')=1 "
                    "THEN 'inferred_lower_bound' ELSE 'exact' END "
                    "WHERE closed_at IS NOT NULL AND holding_duration_ms IS NULL"
                )
            for table in (
                "decisions", "journal_reasons", "decision_reasons",
                "trades", "trade_lifecycles", "trade_executions",
                "current_positions", "trade_links", "lifecycle_links",
            ):
                journal_count = con.execute(
                    f"SELECT COUNT(*) AS n FROM journal.{table}"
                ).fetchone()["n"]
                if not journal_count:
                    con.execute(
                        f"INSERT OR IGNORE INTO journal.{table} SELECT * FROM main.{table}"
                    )
            signal_columns = {
                row["name"] for row in con.execute("PRAGMA table_info(signals)").fetchall()
            }
            if "is_simulation" not in signal_columns:
                con.execute(
                    "ALTER TABLE signals ADD COLUMN is_simulation INTEGER NOT NULL DEFAULT 0"
                )
            con.execute(
                """
                UPDATE signals SET is_simulation=1
                WHERE LOWER(COALESCE(detector,'')) LIKE 'sample-%'
                   OR LOWER(COALESCE(detector,'')) LIKE 'test-%'
                   OR LOWER(COALESCE(detector,'')) IN ('sample','test','test-fire')
                """
            )
            for category, labels in REASON_PRESETS.items():
                for label in labels:
                    con.execute(
                        """
                        INSERT OR IGNORE INTO journal.journal_reasons(
                            reason_key, category, label, is_custom, active, created_at
                        ) VALUES (?, ?, ?, 0, 1, ?)
                        """,
                        (self._reason_key(category, label), category, label, iso()),
                    )

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value or {}, sort_keys=True, separators=(",", ":"), default=str)

    @staticmethod
    def _reason_key(category: str, label: str) -> str:
        normalized_category = " ".join(category.lower().split())
        normalized_label = " ".join(label.lower().split())
        return f"{normalized_category}:{normalized_label}"

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        for key in ("metadata_json",):
            if key in item:
                try:
                    item["metadata"] = json.loads(item.pop(key) or "{}")
                except (TypeError, json.JSONDecodeError):
                    item["metadata"] = {}
        return item

    def upsert_signal(self, signal: dict[str, Any]) -> tuple[int, bool]:
        with self.connect() as con:
            existing = con.execute(
                "SELECT id FROM signals WHERE fingerprint = ?", (signal["fingerprint"],)
            ).fetchone()
            if existing:
                return int(existing["id"]), False
            cur = con.execute(
                """
                INSERT INTO signals(
                    fingerprint, source, source_ref, detector, event_type,
                    occurred_at, ingested_at, symbol, direction, title, summary,
                    severity, confidence, status, is_simulation,
                    baseline_value, observed_value,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, ?)
                """,
                (
                    signal["fingerprint"], signal["source"], signal.get("source_ref"),
                    signal.get("detector"), signal.get("event_type", "signal"),
                    signal["occurred_at"], iso(), signal.get("symbol"),
                    signal.get("direction", "neutral"), signal["title"],
                    signal.get("summary", ""), signal.get("severity", "medium"),
                    signal.get("confidence"), int(bool(signal.get("is_simulation"))),
                    signal.get("baseline_value"),
                    signal.get("observed_value"), self._json(signal.get("metadata")),
                ),
            )
            signal_id = int(cur.lastrowid)
            occurred = parse_time(signal["occurred_at"])
            for minutes in HORIZONS_MINUTES:
                con.execute(
                    """
                    INSERT OR IGNORE INTO outcomes(signal_id, horizon_minutes, due_at)
                    VALUES (?, ?, ?)
                    """,
                    (signal_id, minutes, iso(occurred + timedelta(minutes=minutes))),
                )
            if signal.get("baseline_value") not in (None, 0) and signal.get("observed_value") is not None:
                raw = (
                    (float(signal["observed_value"]) / float(signal["baseline_value"])) - 1
                ) * 100
                con.execute(
                    """
                    INSERT OR IGNORE INTO outcomes(
                        signal_id, horizon_minutes, due_at, captured_at,
                        baseline_price, outcome_price, return_pct, signed_return_pct,
                        status, metadata_json
                    ) VALUES (?, 0, ?, ?, ?, ?, ?, ?, 'complete', ?)
                    """,
                    (
                        signal_id, signal["occurred_at"], iso(),
                        signal["baseline_value"], signal["observed_value"], raw, raw,
                        self._json({"kind": "observed_impact"}),
                    ),
                )
            return signal_id, True

    def upsert_trade(self, trade: dict[str, Any]) -> tuple[int, bool]:
        with self.connect() as con:
            existing = con.execute(
                "SELECT id FROM journal.trades WHERE fingerprint = ?", (trade["fingerprint"],)
            ).fetchone()
            if existing:
                con.execute(
                    """
                    UPDATE journal.trades SET native_trade_id=?, occurred_at=?, symbol=?, side=?,
                        entry=?, exit=?, size=?, notional=?, pnl=?, pnl_pct=?, is_win=?,
                        metadata_json=? WHERE id=?
                    """,
                    (
                        trade.get("native_trade_id"), trade["occurred_at"], trade["symbol"],
                        trade.get("side", "unknown"), trade.get("entry"), trade.get("exit"),
                        trade.get("size"), trade.get("notional"), trade.get("pnl"),
                        trade.get("pnl_pct"), trade.get("is_win"),
                        self._json(trade.get("metadata")), int(existing["id"]),
                    ),
                )
                return int(existing["id"]), False
            cur = con.execute(
                """
                INSERT INTO journal.trades(
                    fingerprint, source, native_trade_id, occurred_at, symbol, side,
                    entry, exit, size, notional, pnl, pnl_pct, is_win, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade["fingerprint"], trade["source"], trade.get("native_trade_id"),
                    trade["occurred_at"], trade["symbol"], trade.get("side", "unknown"),
                    trade.get("entry"), trade.get("exit"), trade.get("size"),
                    trade.get("notional"), trade.get("pnl"), trade.get("pnl_pct"),
                    trade.get("is_win"), self._json(trade.get("metadata")),
                ),
            )
            return int(cur.lastrowid), True

    def purge_signal_source(self, source: str) -> int:
        """Remove a derived signal copy after its source moves to another app."""
        normalized = str(source or "").strip()
        if not normalized:
            raise ValueError("source is required")
        with self.connect() as con:
            row = con.execute(
                "SELECT COUNT(*) AS n FROM signals WHERE source=?", (normalized,)
            ).fetchone()
            con.execute("DELETE FROM signals WHERE source=?", (normalized,))
            return int(row["n"] if row else 0)

    def replace_trade_lifecycles(self, lifecycles: Iterable[dict[str, Any]]) -> int:
        items = list(lifecycles)
        with self.connect() as con:
            linked_execution_keys: dict[int, tuple[int, set[str]]] = {}
            for row in con.execute(
                """
                SELECT ll.id AS link_id, ll.decision_id, e.execution_key
                FROM journal.lifecycle_links ll
                JOIN journal.trade_executions e ON e.lifecycle_id=ll.lifecycle_id
                """
            ).fetchall():
                decision_id, keys = linked_execution_keys.setdefault(
                    int(row["link_id"]), (int(row["decision_id"]), set())
                )
                keys.add(str(row["execution_key"]))
            con.execute("UPDATE journal.trade_lifecycles SET status='superseded'")
            con.execute("DELETE FROM journal.trade_executions")
            for item in items:
                metadata = {
                    "inferred_open": bool(item.get("inferred_open")),
                    "entry_repaired": bool(item.get("entry_repaired")),
                    "batches": item.get("batches") or [],
                }
                con.execute(
                    """
                    INSERT INTO journal.trade_lifecycles(
                        lifecycle_key, source, symbol, side, opened_at, closed_at,
                        status, entry_vwap, exit_vwap, max_size, closed_size,
                        notional, pnl, pnl_pct, is_win, fill_count,
                        entry_batch_count, exit_batch_count, partial_exit_count,
                        management_style, holding_duration_ms, holding_duration_basis,
                        metadata_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(lifecycle_key) DO UPDATE SET
                        source=excluded.source, symbol=excluded.symbol,
                        side=excluded.side, opened_at=excluded.opened_at,
                        closed_at=excluded.closed_at, status=excluded.status,
                        entry_vwap=excluded.entry_vwap, exit_vwap=excluded.exit_vwap,
                        max_size=excluded.max_size, closed_size=excluded.closed_size,
                        notional=excluded.notional, pnl=excluded.pnl,
                        pnl_pct=excluded.pnl_pct, is_win=excluded.is_win,
                        fill_count=excluded.fill_count,
                        entry_batch_count=excluded.entry_batch_count,
                        exit_batch_count=excluded.exit_batch_count,
                        partial_exit_count=excluded.partial_exit_count,
                        management_style=excluded.management_style,
                        holding_duration_ms=excluded.holding_duration_ms,
                        holding_duration_basis=excluded.holding_duration_basis,
                        metadata_json=excluded.metadata_json
                    """,
                    (
                        item["lifecycle_key"], item["source"], item["symbol"],
                        item.get("side", "unknown"), item["opened_at"],
                        item.get("closed_at"), item["status"], item.get("entry_vwap"),
                        item.get("exit_vwap"), item.get("max_size"),
                        item.get("closed_size"), item.get("notional"), item.get("pnl"),
                        item.get("pnl_pct"), item.get("is_win"),
                        int(item.get("fill_count") or 0),
                        int(item.get("entry_batch_count") or 0),
                        int(item.get("exit_batch_count") or 0),
                        int(item.get("partial_exit_count") or 0),
                        item.get("management_style") or "",
                        item.get("holding_duration_ms") if item.get("holding_duration_ms") is not None
                        else holding_duration_ms(item.get("opened_at"), item.get("closed_at")),
                        item.get("holding_duration_basis") or (
                            "inferred_lower_bound" if item.get("inferred_open") and item.get("closed_at")
                            else "exact" if item.get("closed_at") else "unavailable"
                        ),
                        self._json(metadata),
                    ),
                )
                lifecycle_id = int(
                    con.execute(
                        "SELECT id FROM journal.trade_lifecycles WHERE lifecycle_key=?",
                        (item["lifecycle_key"],),
                    ).fetchone()["id"]
                )
                for execution in item.get("executions") or []:
                    con.execute(
                        """
                        INSERT INTO journal.trade_executions(
                            lifecycle_id, execution_key, batch_key, occurred_at,
                            action, batch_label, price, size, pnl,
                            position_before, position_after, metadata_json
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            lifecycle_id, execution["execution_key"],
                            execution.get("batch_key") or "",
                            execution["occurred_at"], execution["action"],
                            execution.get("batch_label") or execution["action"],
                            execution.get("price"), execution.get("size") or 0,
                            execution.get("pnl"), execution.get("position_before"),
                            execution.get("position_after"),
                            self._json(
                                {
                                    "native_trade_id": execution.get("native_trade_id"),
                                    "transaction_id": execution.get("transaction_id"),
                                    "event_kind": execution.get("event_kind"),
                                    "event_id": execution.get("event_id"),
                                    "pnl_basis": execution.get("pnl_basis"),
                                    "position_entry": execution.get("position_entry"),
                                }
                            ),
                        ),
                    )
            for link_id, (decision_id, old_keys) in linked_execution_keys.items():
                if not old_keys:
                    continue
                placeholders = ",".join("?" for _ in old_keys)
                target = con.execute(
                    f"""
                    SELECT e.lifecycle_id, COUNT(*) AS overlap
                    FROM journal.trade_executions e
                    JOIN journal.trade_lifecycles l ON l.id=e.lifecycle_id
                    WHERE e.execution_key IN ({placeholders})
                      AND l.status!='superseded'
                    GROUP BY e.lifecycle_id
                    ORDER BY overlap DESC, e.lifecycle_id DESC
                    LIMIT 1
                    """,
                    list(old_keys),
                ).fetchone()
                if not target:
                    continue
                target_id = int(target["lifecycle_id"])
                duplicate = con.execute(
                    """
                    SELECT id FROM journal.lifecycle_links
                    WHERE decision_id=? AND lifecycle_id=? AND id!=?
                    """,
                    (decision_id, target_id, link_id),
                ).fetchone()
                if duplicate:
                    con.execute(
                        "DELETE FROM journal.lifecycle_links WHERE id=?", (link_id,)
                    )
                else:
                    con.execute(
                        """
                        UPDATE journal.lifecycle_links SET lifecycle_id=?
                        WHERE id=?
                        """,
                        (target_id, link_id),
                    )
        return len(items)

    def list_signals(
        self, *, status: str | None = None, source: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if status and status != "all":
            where.append("s.status = ?")
            params.append(status)
        if source and source != "all":
            where.append("s.source = ?")
            params.append(source)
        clause = "WHERE " + " AND ".join(where) if where else ""
        params.append(max(1, min(limit, 500)))
        with self.connect() as con:
            rows = con.execute(
                f"""
                SELECT s.*,
                       ((julianday('now') - julianday(s.occurred_at)) * 24.0) AS age_hours,
                       CASE
                         WHEN s.is_simulation=1 THEN 'simulation'
                         WHEN s.occurred_at >= ? THEN 'live'
                         WHEN s.occurred_at >= ? THEN 'recent'
                         ELSE 'archive'
                       END AS freshness,
                       (
                         CASE s.severity WHEN 'critical' THEN 40 WHEN 'high' THEN 30
                           WHEN 'medium' THEN 20 ELSE 10 END
                         + COALESCE(s.confidence,0.5) * 20
                         + CASE WHEN s.occurred_at >= ? THEN 30
                                WHEN s.occurred_at >= ? THEN 15 ELSE 0 END
                         - CASE WHEN s.is_simulation=1 THEN 100 ELSE 0 END
                       ) AS priority_score,
                       d.id AS decision_id, d.status AS decision_status,
                       d.thesis AS decision_thesis,
                       (SELECT signed_return_pct FROM outcomes o
                        WHERE o.signal_id=s.id AND o.horizon_minutes=1440
                          AND o.status='complete') AS return_24h,
                       (SELECT COUNT(*) FROM outcomes o
                        WHERE o.signal_id=s.id AND o.status='complete') AS outcome_count
                FROM signals s
                LEFT JOIN journal.decisions d ON d.id = (
                    SELECT id FROM journal.decisions WHERE signal_id=s.id
                    ORDER BY id DESC LIMIT 1
                )
                {clause}
                ORDER BY
                  s.is_simulation ASC,
                  CASE s.status WHEN 'new' THEN 0 WHEN 'reviewing' THEN 1 ELSE 2 END,
                  priority_score DESC,
                  s.occurred_at DESC
                LIMIT ?
                """,
                [
                    iso(utc_now() - timedelta(hours=6)),
                    iso(utc_now() - timedelta(days=7)),
                    iso(utc_now() - timedelta(hours=6)),
                    iso(utc_now() - timedelta(days=7)),
                    *params,
                ],
            ).fetchall()
            return [self._row(row) for row in rows]  # type: ignore[list-item]

    def get_signal(self, signal_id: int) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
            item = self._row(row)
            if item:
                item["outcomes"] = [
                    self._row(outcome)
                    for outcome in con.execute(
                        "SELECT * FROM outcomes WHERE signal_id=? ORDER BY horizon_minutes",
                        (signal_id,),
                    ).fetchall()
                ]
            return item

    def set_signal_status(self, signal_id: int, status: str) -> bool:
        if status not in {"new", "reviewing", "acted", "ignored", "dismissed"}:
            raise ValueError("invalid signal status")
        with self.connect() as con:
            cur = con.execute("UPDATE signals SET status=? WHERE id=?", (status, signal_id))
            return cur.rowcount > 0

    def create_decision(self, payload: dict[str, Any]) -> dict[str, Any]:
        thesis = str(payload.get("thesis", "")).strip()
        if not thesis:
            raise ValueError("thesis is required")
        now = iso()
        with self.connect() as con:
            cur = con.execute(
                """
                INSERT INTO journal.decisions(
                    signal_id, created_at, updated_at, thesis, direction, entry,
                    invalidation, target, max_risk_usd, confidence, status, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload.get("signal_id"), now, now, thesis,
                    payload.get("direction", "neutral"), payload.get("entry"),
                    payload.get("invalidation"), payload.get("target"),
                    payload.get("max_risk_usd"), int(payload.get("confidence", 50)),
                    payload.get("status", "planned"), str(payload.get("notes", "")),
                ),
            )
            if payload.get("signal_id"):
                con.execute(
                    "UPDATE signals SET status='reviewing' WHERE id=?",
                    (payload["signal_id"],),
                )
            decision_id = int(cur.lastrowid)
            self._replace_decision_reasons(
                con, decision_id, payload.get("reason_ids", [])
            )
        return self.get_decision(decision_id) or {}

    def update_decision(self, decision_id: int, payload: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {
            "thesis", "direction", "entry", "invalidation", "target",
            "max_risk_usd", "confidence", "status", "notes",
        }
        fields = [key for key in payload if key in allowed]
        has_reasons = "reason_ids" in payload
        if not fields and not has_reasons:
            return self.get_decision(decision_id)
        if payload.get("status") and payload["status"] not in {
            "planned", "active", "closed", "invalidated", "skipped"
        }:
            raise ValueError("invalid decision status")
        assignments = ", ".join(f"{key}=?" for key in fields) + ", updated_at=?"
        values = [payload[key] for key in fields] + [iso(), decision_id]
        with self.connect() as con:
            if fields:
                cur = con.execute(
                    f"UPDATE journal.decisions SET {assignments} WHERE id=?", values
                )
                if not cur.rowcount:
                    return None
            elif not con.execute(
                "SELECT 1 FROM journal.decisions WHERE id=?", (decision_id,)
            ).fetchone():
                return None
            if has_reasons:
                self._replace_decision_reasons(
                    con, decision_id, payload.get("reason_ids", [])
                )
            if payload.get("status") in {"active", "closed"}:
                row = con.execute(
                    "SELECT signal_id FROM journal.decisions WHERE id=?", (decision_id,)
                ).fetchone()
                if row and row["signal_id"]:
                    con.execute(
                        "UPDATE signals SET status='acted' WHERE id=?", (row["signal_id"],)
                    )
        return self.get_decision(decision_id)

    def get_decision(self, decision_id: int) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute(
                """
                SELECT d.*, s.title AS signal_title, s.symbol AS signal_symbol,
                       s.occurred_at AS signal_time
                FROM journal.decisions d LEFT JOIN signals s ON s.id=d.signal_id
                WHERE d.id=?
                """,
                (decision_id,),
            ).fetchone()
            item = self._row(row)
            if item:
                item["reasons"] = [
                    dict(reason)
                    for reason in con.execute(
                        """
                        SELECT r.id, r.category, r.label, r.is_custom
                        FROM journal.decision_reasons dr JOIN journal.journal_reasons r
                          ON r.id=dr.reason_id
                        WHERE dr.decision_id=?
                        ORDER BY r.category, r.label
                        """,
                        (decision_id,),
                    ).fetchall()
                ]
                item["trades"] = [
                    self._row(link)
                    for link in con.execute(
                        """
                        SELECT t.*, tl.linked_at FROM journal.trade_links tl
                        JOIN journal.trades t ON t.id=tl.trade_id
                        WHERE tl.decision_id=? ORDER BY t.occurred_at
                        """,
                        (decision_id,),
                    ).fetchall()
                ]
                lifecycle_rows = con.execute(
                    """
                    SELECT l.*, ll.linked_at FROM journal.lifecycle_links ll
                    JOIN journal.trade_lifecycles l ON l.id=ll.lifecycle_id
                    WHERE ll.decision_id=? ORDER BY l.opened_at
                    """,
                    (decision_id,),
                ).fetchall()
                for lifecycle_row in lifecycle_rows:
                    lifecycle = self._row(lifecycle_row)
                    if lifecycle:
                        lifecycle["entry"] = lifecycle.get("entry_vwap")
                        lifecycle["exit"] = lifecycle.get("exit_vwap")
                        lifecycle["occurred_at"] = (
                            lifecycle.get("closed_at") or lifecycle.get("opened_at")
                        )
                        lifecycle["is_lifecycle"] = True
                        item["trades"].append(lifecycle)
            return item

    def list_decisions(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT d.*, s.title AS signal_title, s.symbol AS signal_symbol,
                       s.source AS signal_source,
                       (SELECT GROUP_CONCAT(r.label, ' · ')
                        FROM journal.decision_reasons dr JOIN journal.journal_reasons r
                          ON r.id=dr.reason_id
                        WHERE dr.decision_id=d.id) AS reason_labels,
                       (SELECT GROUP_CONCAT(r.id, ',')
                        FROM journal.decision_reasons dr JOIN journal.journal_reasons r
                          ON r.id=dr.reason_id
                        WHERE dr.decision_id=d.id) AS reason_ids,
                       (
                         (SELECT COUNT(*) FROM journal.trade_links tl WHERE tl.decision_id=d.id)
                         + (SELECT COUNT(*) FROM journal.lifecycle_links ll
                            WHERE ll.decision_id=d.id)
                       ) AS linked_trades,
                       (
                         COALESCE((SELECT SUM(t.pnl) FROM journal.trade_links tl
                           JOIN journal.trades t ON t.id=tl.trade_id
                           WHERE tl.decision_id=d.id),0)
                         + COALESCE((SELECT SUM(l.pnl) FROM journal.lifecycle_links ll
                           JOIN journal.trade_lifecycles l ON l.id=ll.lifecycle_id
                           WHERE ll.decision_id=d.id),0)
                       ) AS realized_pnl
                FROM journal.decisions d
                LEFT JOIN signals s ON s.id=d.signal_id
                ORDER BY d.updated_at DESC LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
            items = [self._row(row) for row in rows]
            for item in items:
                if not item:
                    continue
                linked = con.execute(
                    """
                    SELECT l.source, l.symbol, l.side, l.status
                    FROM journal.lifecycle_links ll
                    JOIN journal.trade_lifecycles l ON l.id=ll.lifecycle_id
                    WHERE ll.decision_id=? AND l.status!='superseded'
                    """,
                    (item["id"],),
                ).fetchall()
                open_links = [
                    row for row in linked if row["status"] not in {"closed", "reversed"}
                ]
                unrealized = 0.0
                has_live_mark = False
                for link in open_links:
                    position = con.execute(
                        """
                        SELECT unrealized_pnl FROM journal.current_positions
                        WHERE source=? AND symbol=? AND side=?
                        ORDER BY updated_at DESC LIMIT 1
                        """,
                        (link["source"], link["symbol"], link["side"]),
                    ).fetchone()
                    if position and position["unrealized_pnl"] is not None:
                        unrealized += float(position["unrealized_pnl"])
                        has_live_mark = True
                item["effective_status"] = (
                    "active" if open_links else "closed"
                ) if linked else item["status"]
                item["unrealized_pnl"] = (
                    unrealized if open_links and has_live_mark else None
                )
                realized = float(item.get("realized_pnl") or 0)
                item["display_pnl"] = (
                    realized + unrealized
                    if not open_links or has_live_mark or realized
                    else None
                )
                item["reason_ids"] = [
                    int(value)
                    for value in str(item.get("reason_ids") or "").split(",")
                    if value
                ]
            return [item for item in items if item]

    def list_reasons(self) -> dict[str, Any]:
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT id, category, label, is_custom
                FROM journal.journal_reasons WHERE active=1
                ORDER BY
                  CASE category
                    WHEN 'Setup' THEN 1 WHEN 'Trigger' THEN 2
                    WHEN 'Market context' THEN 3 WHEN 'Execution' THEN 4
                    WHEN 'Psychology' THEN 5 ELSE 6
                  END,
                  is_custom, label
                """
            ).fetchall()
        categories: list[dict[str, Any]] = []
        by_category: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_category.setdefault(row["category"], []).append(dict(row))
        for category, reasons in by_category.items():
            categories.append({"name": category, "reasons": reasons})
        return {"categories": categories, "total": len(rows)}

    def create_reason(self, category: str, label: str) -> dict[str, Any]:
        category = " ".join(str(category).split())
        label = " ".join(str(label).split())
        if category not in REASON_PRESETS:
            raise ValueError("choose a valid reason category")
        if len(label) < 2 or len(label) > 60:
            raise ValueError("reason must be between 2 and 60 characters")
        key = self._reason_key(category, label)
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO journal.journal_reasons(
                    reason_key, category, label, is_custom, active, created_at
                ) VALUES (?, ?, ?, 1, 1, ?)
                ON CONFLICT(reason_key) DO UPDATE SET active=1
                """,
                (key, category, label, iso()),
            )
            row = con.execute(
                """
                SELECT id, category, label, is_custom
                FROM journal.journal_reasons WHERE reason_key=?
                """,
                (key,),
            ).fetchone()
        return dict(row)

    def _replace_decision_reasons(
        self, con: sqlite3.Connection, decision_id: int, reason_ids: Iterable[Any]
    ) -> None:
        normalized: list[int] = []
        for value in reason_ids or []:
            try:
                reason_id = int(value)
            except (TypeError, ValueError):
                raise ValueError("reason_ids must contain integers") from None
            if reason_id not in normalized:
                normalized.append(reason_id)
        if len(normalized) > 12:
            raise ValueError("choose at most 12 reasons")
        if normalized:
            placeholders = ",".join("?" for _ in normalized)
            found = {
                int(row["id"])
                for row in con.execute(
                    f"""
                    SELECT id FROM journal.journal_reasons
                    WHERE active=1 AND id IN ({placeholders})
                    """,
                    normalized,
                ).fetchall()
            }
            if found != set(normalized):
                raise ValueError("one or more reasons are unavailable")
        con.execute("DELETE FROM journal.decision_reasons WHERE decision_id=?", (decision_id,))
        for reason_id in normalized:
            con.execute(
                """
                INSERT INTO journal.decision_reasons(decision_id, reason_id, created_at)
                VALUES (?, ?, ?)
                """,
                (decision_id, reason_id, iso()),
            )

    def list_trades(self, limit: int = 300) -> list[dict[str, Any]]:
        with self.connect() as con:
            lifecycle_rows = con.execute(
                """
                SELECT l.*,
                       (
                         SELECT ll.decision_id
                         FROM journal.lifecycle_links ll
                         WHERE ll.lifecycle_id=l.id
                         ORDER BY ll.linked_at DESC, ll.id DESC LIMIT 1
                       ) AS decision_id,
                       (
                         SELECT d.thesis
                         FROM journal.lifecycle_links ll
                         JOIN journal.decisions d ON d.id=ll.decision_id
                         WHERE ll.lifecycle_id=l.id
                         ORDER BY ll.linked_at DESC, ll.id DESC LIMIT 1
                       ) AS decision_thesis
                FROM journal.trade_lifecycles l
                WHERE l.status!='superseded'
                ORDER BY COALESCE(l.closed_at,l.opened_at) DESC LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
            if lifecycle_rows:
                result: list[dict[str, Any]] = []
                for row in lifecycle_rows:
                    item = self._row(row)
                    if not item:
                        continue
                    item["entry"] = item.get("entry_vwap")
                    item["exit"] = item.get("exit_vwap")
                    item["size"] = item.get("max_size")
                    item["occurred_at"] = item.get("closed_at") or item.get("opened_at")
                    if item.get("closed_at") is None:
                        from holding_time import parse_timestamp
                        item["holding_duration_ms"] = max(
                            0,
                            int((utc_now() - parse_timestamp(item["opened_at"])).total_seconds() * 1000),
                        )
                        item["holding_as_of"] = iso()
                    item["is_lifecycle"] = True
                    item["batches"] = item.get("metadata", {}).get("batches", [])
                    item["executions"] = [
                        self._row(execution)
                        for execution in con.execute(
                            """
                            SELECT * FROM journal.trade_executions
                            WHERE lifecycle_id=? ORDER BY occurred_at,id
                            """,
                            (item["id"],),
                        ).fetchall()
                    ]
                    result.append(item)
                return result
            rows = con.execute(
                """
                SELECT t.*, tl.decision_id, d.thesis AS decision_thesis
                FROM journal.trades t
                LEFT JOIN journal.trade_links tl ON tl.trade_id=t.id
                LEFT JOIN journal.decisions d ON d.id=tl.decision_id
                ORDER BY t.occurred_at DESC LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
            result = [self._row(row) for row in rows]
            for item in result:
                if item:
                    item["is_lifecycle"] = False
                    item["batches"] = []
                    item["executions"] = []
            return [item for item in result if item]

    def lifecycle_evaluation(self) -> dict[str, Any]:
        from .evals import evaluate_lifecycles

        lifecycles = [
            item for item in self.list_trades(limit=500)
            if item.get("is_lifecycle")
        ]
        return evaluate_lifecycles(lifecycles)

    def replace_positions(self, positions: Iterable[dict[str, Any]]) -> int:
        items = list(positions)
        with self.connect() as con:
            con.execute("DELETE FROM journal.current_positions")
            for item in items:
                con.execute(
                    """
                    INSERT INTO journal.current_positions(
                        position_key, source, symbol, side, size, entry,
                        unrealized_pnl, liquidation_price, updated_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["position_key"], item["source"], item["symbol"],
                        item["side"], item["size"], item.get("entry"),
                        item.get("unrealized_pnl"), item.get("liquidation_price"),
                        item["updated_at"], self._json(item.get("metadata")),
                    ),
                )
        return len(items)

    def list_positions(self) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT *, ABS(size * COALESCE(entry,0)) AS notional
                FROM journal.current_positions ORDER BY ABS(size * COALESCE(entry,0)) DESC
                """
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                item = self._row(row)
                if item is None:
                    continue
                size = float(item.get("size") or 0)
                entry = float(item.get("entry") or 0)
                live_pnl = item.get("unrealized_pnl")
                side = str(item.get("side") or "").lower()
                # For linear perpetuals, an exchange unrealized mark lets us
                # derive the current mark without exposing contract quantity.
                # If no live mark exists, keep current_price null and retain
                # the entry-based notional only as a fallback value.
                current_price = None
                if size and entry and live_pnl is not None:
                    direction = 1 if side == "long" else -1
                    current_price = entry + float(live_pnl) / (size * direction)
                item["current_price"] = current_price
                item["position_value"] = (
                    abs(size * current_price) if current_price is not None else None
                )
                result.append(item)
            return result

    def link_trade(self, decision_id: int, trade_id: int) -> dict[str, Any]:
        with self.connect() as con:
            if not con.execute("SELECT 1 FROM journal.decisions WHERE id=?", (decision_id,)).fetchone():
                raise KeyError("decision not found")
            if not con.execute("SELECT 1 FROM journal.trades WHERE id=?", (trade_id,)).fetchone():
                raise KeyError("trade not found")
            con.execute(
                """
                INSERT OR IGNORE INTO journal.trade_links(decision_id, trade_id, linked_at)
                VALUES (?, ?, ?)
                """,
                (decision_id, trade_id, iso()),
            )
        return self.get_decision(decision_id) or {}

    def link_lifecycle(self, decision_id: int, lifecycle_id: int) -> dict[str, Any]:
        with self.connect() as con:
            if not con.execute("SELECT 1 FROM journal.decisions WHERE id=?", (decision_id,)).fetchone():
                raise KeyError("decision not found")
            if not con.execute(
                "SELECT 1 FROM journal.trade_lifecycles WHERE id=?", (lifecycle_id,)
            ).fetchone():
                raise KeyError("trade lifecycle not found")
            con.execute(
                """
                INSERT OR IGNORE INTO journal.lifecycle_links(decision_id,lifecycle_id,linked_at)
                VALUES (?,?,?)
                """,
                (decision_id, lifecycle_id, iso()),
            )
        return self.get_decision(decision_id) or {}

    def complete_outcome(
        self,
        signal_id: int,
        horizon: int,
        *,
        baseline: float,
        outcome: float,
        mfe: float | None,
        mae: float | None,
        captured_at: str,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        if baseline == 0:
            return False
        raw = ((outcome / baseline) - 1) * 100
        with self.connect() as con:
            signal = con.execute(
                "SELECT direction FROM signals WHERE id=?", (signal_id,)
            ).fetchone()
            if not signal:
                return False
            direction = signal["direction"]
            signed = -raw if direction == "short" else raw
            if direction == "neutral":
                signed = None
            cur = con.execute(
                """
                UPDATE outcomes SET captured_at=?, baseline_price=?, outcome_price=?,
                    return_pct=?, signed_return_pct=?, mfe_pct=?, mae_pct=?,
                    status='complete', metadata_json=?
                WHERE signal_id=? AND horizon_minutes=? AND status!='complete'
                """,
                (
                    captured_at, baseline, outcome, raw, signed, mfe, mae,
                    self._json(metadata), signal_id, horizon,
                ),
            )
            return cur.rowcount > 0

    def pending_outcomes(self) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT o.*, s.occurred_at, s.symbol, s.direction, s.source,
                       s.metadata_json AS signal_metadata_json
                FROM outcomes o JOIN signals s ON s.id=o.signal_id
                WHERE o.status='pending' AND o.due_at <= ?
                ORDER BY o.due_at LIMIT 1000
                """,
                (iso(),),
            ).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                try:
                    item["signal_metadata"] = json.loads(
                        item.pop("signal_metadata_json") or "{}"
                    )
                except json.JSONDecodeError:
                    item["signal_metadata"] = {}
                items.append(item)
            return items

    def settings(self) -> dict[str, Any]:
        with self.connect() as con:
            rows = con.execute("SELECT key, value FROM settings").fetchall()
        result: dict[str, Any] = {}
        for row in rows:
            value: Any = row["value"]
            try:
                value = float(value)
                if value.is_integer():
                    value = int(value)
            except (ValueError, AttributeError):
                pass
            result[row["key"]] = value
        return result

    def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        allowed = {"daily_risk_budget", "max_open_decisions"}
        with self.connect() as con:
            for key, value in values.items():
                if key not in allowed:
                    continue
                con.execute(
                    """
                    INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                                   updated_at=excluded.updated_at
                    """,
                    (key, str(value), iso()),
                )
        return self.settings()

    def summary(self) -> dict[str, Any]:
        settings = self.settings()
        with self.connect() as con:
            signal_counts = {
                row["status"]: row["count"]
                for row in con.execute(
                    """
                    SELECT status, COUNT(*) AS count FROM signals
                    WHERE is_simulation=0 GROUP BY status
                    """
                )
            }
            live_cutoff = iso(utc_now() - timedelta(hours=6))
            recent_cutoff = iso(utc_now() - timedelta(days=7))
            signal_quality = con.execute(
                """
                SELECT
                  SUM(CASE WHEN is_simulation=0 THEN 1 ELSE 0 END) AS production_count,
                  SUM(CASE WHEN is_simulation=1 THEN 1 ELSE 0 END) AS simulation_count,
                  SUM(CASE WHEN is_simulation=0 AND occurred_at>=? THEN 1 ELSE 0 END) AS live_count,
                  SUM(CASE WHEN is_simulation=0 AND occurred_at>=? THEN 1 ELSE 0 END) AS actionable_count,
                  SUM(CASE WHEN is_simulation=0 AND occurred_at<? THEN 1 ELSE 0 END) AS archived_count,
                  MAX(CASE WHEN is_simulation=0 THEN occurred_at END) AS latest_production_at
                FROM signals
                """,
                (live_cutoff, recent_cutoff, recent_cutoff),
            ).fetchone()
            decision_counts = {
                row["status"]: row["count"]
                for row in con.execute(
                    "SELECT status, COUNT(*) AS count FROM journal.decisions GROUP BY status"
                )
            }
            lifecycle_count = int(
                con.execute(
                    "SELECT COUNT(*) FROM journal.trade_lifecycles WHERE status!='superseded'"
                ).fetchone()[0]
            )
            trade_table = (
                "journal.trade_lifecycles" if lifecycle_count else "journal.trades"
            )
            trade_where = "WHERE status!='superseded'" if lifecycle_count else ""
            trade = con.execute(
                f"""
                SELECT COUNT(*) AS count, COALESCE(SUM(pnl),0) AS pnl,
                       COALESCE(AVG(CASE WHEN is_win IS NOT NULL THEN is_win END),0) AS win_rate
                FROM {trade_table} {trade_where}
                """
            ).fetchone()
            risk = con.execute(
                """
                SELECT COALESCE(SUM(max_risk_usd),0) AS committed
                FROM journal.decisions WHERE status IN ('planned','active')
                """
            ).fetchone()
            outcome = con.execute(
                """
                SELECT COUNT(*) AS count,
                       AVG(CASE WHEN signed_return_pct > 0 THEN 1.0 ELSE 0.0 END) AS precision,
                       AVG(signed_return_pct) AS avg_edge
                FROM outcomes WHERE horizon_minutes=1440 AND status='complete'
                  AND signed_return_pct IS NOT NULL
                  AND signal_id IN (SELECT id FROM signals WHERE is_simulation=0)
                """
            ).fetchone()
            last_sync = con.execute(
                "SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            freshness_cutoff = iso(utc_now() - timedelta(hours=2))
            positions = con.execute(
                """
                SELECT COUNT(*) AS reconstructed_count,
                       SUM(CASE WHEN updated_at >= ? THEN 1 ELSE 0 END) AS fresh_count,
                       SUM(CASE WHEN updated_at < ? THEN 1 ELSE 0 END) AS stale_count,
                       COALESCE(SUM(CASE WHEN updated_at >= ?
                         THEN ABS(size * COALESCE(entry,0)) ELSE 0 END),0) AS notional,
                       COALESCE(SUM(CASE WHEN updated_at >= ?
                         THEN unrealized_pnl ELSE 0 END),0) AS unrealized_pnl
                FROM journal.current_positions
                """,
                (freshness_cutoff, freshness_cutoff, freshness_cutoff, freshness_cutoff),
            ).fetchone()
        daily_budget = float(settings.get("daily_risk_budget", 500))
        committed = float(risk["committed"] or 0)
        return {
            "signals": signal_counts,
            "signal_quality": dict(signal_quality),
            "decisions": decision_counts,
            "trades": {
                "count": trade["count"],
                "realized_pnl": trade["pnl"],
                "win_rate": float(trade["win_rate"] or 0) * 100,
            },
            "positions": dict(positions),
            "edge": {
                "sample_size": outcome["count"],
                "precision_24h": (
                    float(outcome["precision"]) * 100 if outcome["precision"] is not None else None
                ),
                "average_edge_24h": outcome["avg_edge"],
            },
            "risk": {
                "daily_budget": daily_budget,
                "committed": committed,
                "available": daily_budget - committed,
                "utilization_pct": (committed / daily_budget * 100) if daily_budget else 0,
            },
            "last_sync": dict(last_sync) if last_sync else None,
        }

    def edge_report(self) -> dict[str, Any]:
        with self.connect() as con:
            groups = con.execute(
                """
                SELECT s.source, COALESCE(s.detector, s.event_type) AS strategy,
                       COUNT(o.id) AS samples,
                       AVG(o.signed_return_pct) AS avg_return,
                       AVG(CASE WHEN o.signed_return_pct > 0 THEN 1.0 ELSE 0.0 END) AS hit_rate,
                       AVG(o.mfe_pct) AS avg_mfe,
                       AVG(o.mae_pct) AS avg_mae
                FROM outcomes o JOIN signals s ON s.id=o.signal_id
                WHERE o.horizon_minutes=1440 AND o.status='complete'
                  AND o.signed_return_pct IS NOT NULL AND s.is_simulation=0
                GROUP BY s.source, COALESCE(s.detector, s.event_type)
                ORDER BY avg_return DESC
                """
            ).fetchall()
            by_horizon = con.execute(
                """
                SELECT horizon_minutes, COUNT(*) AS samples,
                       AVG(signed_return_pct) AS avg_return,
                       AVG(CASE WHEN signed_return_pct > 0 THEN 1.0 ELSE 0.0 END) AS hit_rate
                FROM outcomes o JOIN signals s ON s.id=o.signal_id
                WHERE o.status='complete' AND o.signed_return_pct IS NOT NULL
                  AND s.is_simulation=0
                GROUP BY horizon_minutes ORDER BY horizon_minutes
                """
            ).fetchall()
            decision_quality = con.execute(
                """
                SELECT
                  SUM(CASE WHEN s.status='acted' THEN 1 ELSE 0 END) AS acted,
                  SUM(CASE WHEN s.status IN ('ignored','dismissed') THEN 1 ELSE 0 END) AS ignored,
                  AVG(CASE WHEN s.status='acted' THEN o.signed_return_pct END) AS acted_edge,
                  AVG(CASE WHEN s.status IN ('ignored','dismissed') THEN o.signed_return_pct END) AS ignored_edge
                FROM signals s LEFT JOIN outcomes o
                  ON o.signal_id=s.id AND o.horizon_minutes=1440 AND o.status='complete'
                WHERE s.is_simulation=0
                """
            ).fetchone()
            excluded = con.execute(
                """
                SELECT COUNT(DISTINCT s.id) AS signals,
                       SUM(CASE WHEN o.status='complete' THEN 1 ELSE 0 END) AS outcomes
                FROM signals s LEFT JOIN outcomes o ON o.signal_id=s.id
                WHERE s.is_simulation=1
                """
            ).fetchone()
        return {
            "strategies": [
                {
                    **dict(row),
                    "hit_rate": float(row["hit_rate"] or 0) * 100,
                }
                for row in groups
            ],
            "horizons": [
                {
                    **dict(row),
                    "hit_rate": float(row["hit_rate"] or 0) * 100,
                }
                for row in by_horizon
            ],
            "decision_quality": dict(decision_quality),
            "excluded_simulations": dict(excluded),
        }

    def weekly_review(self) -> dict[str, Any]:
        since = iso(utc_now() - timedelta(days=7))
        with self.connect() as con:
            lifecycle_count = int(
                con.execute(
                    "SELECT COUNT(*) FROM journal.trade_lifecycles WHERE status!='superseded'"
                ).fetchone()[0]
            )
            trade_table = (
                "journal.trade_lifecycles" if lifecycle_count else "journal.trades"
            )
            trade_time = "opened_at" if lifecycle_count else "occurred_at"
            active_clause = "AND status!='superseded'" if lifecycle_count else ""
            signals = con.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status='acted' THEN 1 ELSE 0 END) AS acted,
                       SUM(CASE WHEN status IN ('ignored','dismissed') THEN 1 ELSE 0 END) AS ignored
                FROM signals WHERE occurred_at >= ? AND is_simulation=0
                """,
                (since,),
            ).fetchone()
            trades = con.execute(
                f"""
                SELECT COUNT(*) AS total, COALESCE(SUM(pnl),0) AS pnl,
                       AVG(CASE WHEN is_win IS NOT NULL THEN is_win END) AS win_rate,
                       MAX(pnl) AS best_pnl, MIN(pnl) AS worst_pnl
                FROM {trade_table} WHERE {trade_time} >= ? {active_clause}
                """,
                (since,),
            ).fetchone()
            best = con.execute(
                """
                SELECT s.title, s.symbol, s.source, o.signed_return_pct
                FROM outcomes o JOIN signals s ON s.id=o.signal_id
                WHERE o.horizon_minutes=1440 AND o.status='complete'
                  AND s.occurred_at >= ? AND o.signed_return_pct IS NOT NULL
                  AND s.is_simulation=0
                ORDER BY o.signed_return_pct DESC LIMIT 1
                """,
                (since,),
            ).fetchone()
            missed = con.execute(
                """
                SELECT s.title, s.symbol, o.signed_return_pct
                FROM outcomes o JOIN signals s ON s.id=o.signal_id
                WHERE o.horizon_minutes=1440 AND o.status='complete'
                  AND s.status IN ('ignored','dismissed') AND s.occurred_at >= ?
                  AND o.signed_return_pct > 0 AND s.is_simulation=0
                ORDER BY o.signed_return_pct DESC LIMIT 1
                """,
                (since,),
            ).fetchone()
            worst_signal = con.execute(
                """
                SELECT s.title, s.symbol, s.source, o.signed_return_pct
                FROM outcomes o JOIN signals s ON s.id=o.signal_id
                WHERE o.horizon_minutes=1440 AND o.status='complete'
                  AND s.occurred_at >= ? AND o.signed_return_pct IS NOT NULL
                  AND s.is_simulation=0
                ORDER BY o.signed_return_pct ASC LIMIT 1
                """,
                (since,),
            ).fetchone()
            top_strategy = con.execute(
                """
                SELECT COALESCE(s.detector,s.event_type) AS strategy, s.source,
                       COUNT(*) AS samples, AVG(o.signed_return_pct) AS avg_edge,
                       AVG(CASE WHEN o.signed_return_pct > 0 THEN 1.0 ELSE 0.0 END) AS hit_rate
                FROM outcomes o JOIN signals s ON s.id=o.signal_id
                WHERE o.horizon_minutes=1440 AND o.status='complete'
                  AND s.occurred_at >= ? AND o.signed_return_pct IS NOT NULL
                  AND s.is_simulation=0
                GROUP BY s.source, COALESCE(s.detector,s.event_type)
                ORDER BY avg_edge DESC LIMIT 1
                """,
                (since,),
            ).fetchone()
            loss_pattern = con.execute(
                f"""
                SELECT symbol, COUNT(*) AS losses, SUM(pnl) AS total_pnl
                FROM {trade_table}
                WHERE {trade_time} >= ? AND pnl < 0 {active_clause}
                GROUP BY symbol ORDER BY total_pnl ASC LIMIT 1
                """,
                (since,),
            ).fetchone()
        total_signals = int(signals["total"] or 0)
        acted = int(signals["acted"] or 0)
        discipline = (acted / total_signals * 100) if total_signals else 0
        observations: list[str] = []
        if total_signals == 0:
            observations.append("No new signals were captured this week.")
        else:
            observations.append(
                f"{acted} of {total_signals} signals became documented actions."
            )
        if missed:
            observations.append(
                f"Best ignored setup returned {missed['signed_return_pct']:.2f}% after 24h."
            )
        if top_strategy:
            observations.append(
                f"Best measured setup was {top_strategy['strategy']} at "
                f"{float(top_strategy['avg_edge'] or 0):+.2f}% average 24h edge."
            )
        if worst_signal and float(worst_signal["signed_return_pct"] or 0) < 0:
            observations.append(
                f"Noisiest signal was {worst_signal['symbol'] or worst_signal['title']} "
                f"at {float(worst_signal['signed_return_pct']):+.2f}% directional edge."
            )
        if loss_pattern:
            observations.append(
                f"Repeated loss concentration: {loss_pattern['losses']} losing "
                f"{loss_pattern['symbol']} trade(s), {float(loss_pattern['total_pnl']):+.2f} P&L."
            )
        if trades["total"]:
            observations.append(
                f"Realized P&L was {float(trades['pnl'] or 0):+.2f} across {trades['total']} closed trades."
            )
        return {
            "period_start": since,
            "period_end": iso(),
            "signals": dict(signals),
            "trades": {
                **dict(trades),
                "win_rate": float(trades["win_rate"] or 0) * 100,
            },
            "discipline_score": discipline,
            "best_signal": dict(best) if best else None,
            "best_ignored": dict(missed) if missed else None,
            "worst_signal": dict(worst_signal) if worst_signal else None,
            "top_strategy": dict(top_strategy) if top_strategy else None,
            "loss_pattern": dict(loss_pattern) if loss_pattern else None,
            "observations": observations,
        }

    def start_sync(self) -> int:
        with self.connect() as con:
            cur = con.execute(
                "INSERT INTO sync_runs(started_at,status) VALUES (?,'running')", (iso(),)
            )
            return int(cur.lastrowid)

    def start_journal_sync(self) -> int:
        with self.connect() as con:
            cur = con.execute(
                """
                INSERT INTO journal.sync_runs(started_at,status)
                VALUES (?, 'running')
                """,
                (iso(),),
            )
            return int(cur.lastrowid)

    def finish_journal_sync(
        self, run_id: int, *, trades: int, positions: int,
        error: str | None = None,
    ) -> None:
        with self.connect() as con:
            con.execute(
                """
                UPDATE journal.sync_runs
                SET finished_at=?, status=?, imported_trades=?,
                    imported_positions=?, error=?
                WHERE id=?
                """,
                (
                    iso(), "error" if error else "ok", trades, positions,
                    error, run_id,
                ),
            )

    def last_journal_sync(self) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute(
                "SELECT * FROM journal.sync_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return self._row(row)

    def finish_sync(
        self, run_id: int, *, signals: int, trades: int, outcomes: int, error: str | None = None
    ) -> None:
        with self.connect() as con:
            con.execute(
                """
                UPDATE sync_runs SET finished_at=?, status=?, imported_signals=?,
                    imported_trades=?, completed_outcomes=?, error=? WHERE id=?
                """,
                (
                    iso(), "error" if error else "ok", signals, trades, outcomes,
                    error, run_id,
                ),
            )
