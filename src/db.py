"""SQLite persistence for dashboard events.

Stores events as JSON blobs so the dashboard survives restarts.
Uses asyncio.to_thread so SQLite blocking calls don't stall the event loop.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any


def _migrate_closed_trades(con: sqlite3.Connection) -> None:
    """Idempotently add legacy realization and v2 identity columns.

    SQLite has no ADD COLUMN IF NOT EXISTS, so we inspect PRAGMA table_info
    and only issue ALTER TABLE for columns that are actually missing.
    Existing rows will have NULL for the new columns — callers should treat
    NULL realization_kind as "FULL".
    """
    existing = {row[1] for row in con.execute("PRAGMA table_info(closed_trades)")}
    new_cols = [
        ("trade_id",         "INTEGER"),
        ("fill_ids",         "TEXT"),
        ("realization_kind", "TEXT"),
        ("source_id",         "TEXT"),
        ("exchange",          "TEXT"),
        ("market_key",        "TEXT"),
        ("position_side",     "TEXT"),
        ("native_trade_id",   "TEXT"),
        ("event_uid",         "TEXT"),
        ("lifecycle_opened_at", "TEXT"),
        ("holding_duration_ms", "INTEGER"),
        ("holding_duration_basis", "TEXT"),
    ]
    for col_name, col_type in new_cols:
        if col_name not in existing:
            con.execute(
                f"ALTER TABLE closed_trades ADD COLUMN {col_name} {col_type}"
            )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_closed_trades_event_uid "
        "ON closed_trades(event_uid) WHERE event_uid IS NOT NULL"
    )
    con.commit()


def _init_sync(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("""
        CREATE TABLE IF NOT EXISTS schema_meta (
            key    TEXT PRIMARY KEY,
            value  TEXT NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            ts       TEXT    NOT NULL,
            payload  TEXT    NOT NULL,
            source_id TEXT,
            exchange TEXT,
            event_uid TEXT
        )
    """)
    event_cols = {row[1] for row in con.execute("PRAGMA table_info(events)")}
    for col_name in ("source_id", "exchange", "event_uid"):
        if col_name not in event_cols:
            con.execute(f"ALTER TABLE events ADD COLUMN {col_name} TEXT")
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_events_event_uid "
        "ON events(event_uid) WHERE event_uid IS NOT NULL"
    )
    con.execute("""
        CREATE TABLE IF NOT EXISTS closed_trades (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            ts                TEXT    NOT NULL,
            source            TEXT,
            market_symbol     TEXT,
            side              TEXT,
            entry             TEXT,
            exit              TEXT,
            size              TEXT,
            notional          TEXT,
            pnl               TEXT,
            pct               TEXT,
            is_win            INTEGER,
            leverage          TEXT,
            wins              INTEGER,
            total             INTEGER,
            card_path         TEXT,
            trade_id          INTEGER,
            fill_ids          TEXT,
            realization_kind  TEXT
            ,source_id         TEXT
            ,exchange          TEXT
            ,market_key        TEXT
            ,position_side     TEXT
            ,native_trade_id   TEXT
            ,event_uid         TEXT
            ,lifecycle_opened_at TEXT
            ,holding_duration_ms INTEGER
            ,holding_duration_basis TEXT
        )
    """)
    _migrate_closed_trades(con)
    con.execute(
        "INSERT INTO schema_meta(key, value) VALUES('schema_version', '2') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )
    con.execute("""
        CREATE TABLE IF NOT EXISTS tg_alerts (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            ts   TEXT    NOT NULL,
            kind TEXT    NOT NULL,
            text TEXT    NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS source_cursors (
            source_id  TEXT NOT NULL,
            cursor_key TEXT NOT NULL,
            cursor     TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (source_id, cursor_key)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS notification_outbox (
            event_uid    TEXT PRIMARY KEY,
            destination  TEXT NOT NULL,
            payload      TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'pending',
            attempts     INTEGER NOT NULL DEFAULT 0,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            last_error   TEXT
        )
    """)
    # Additive v3 accounting schema.  Existing tables remain intact during the
    # shadow/cutover period; canonical_pnl owns all new ledger tables.
    from .canonical_pnl import init_canonical_schema

    init_canonical_schema(con)
    con.commit()
    con.close()


def _save_sync(
    path: Path,
    ts: str,
    payload: str,
    source_id: str = "",
    exchange: str = "",
    event_uid: str = "",
) -> None:
    con = sqlite3.connect(path)
    if event_uid:
        con.execute(
            "INSERT OR IGNORE INTO events "
            "(ts, payload, source_id, exchange, event_uid) VALUES (?, ?, ?, ?, ?)",
            (ts, payload, source_id or None, exchange or None, event_uid),
        )
    else:
        con.execute("INSERT INTO events (ts, payload) VALUES (?, ?)", (ts, payload))
    con.commit()
    con.close()


def _load_sync(path: Path, limit: int) -> list[dict]:
    con = sqlite3.connect(path)
    rows = con.execute(
        "SELECT payload FROM events ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    con.close()
    return [json.loads(r[0]) for r in rows]


async def init_db(path: Path) -> None:
    await asyncio.to_thread(_init_sync, path)


async def save_event(
    path: Path,
    ts: str,
    payload: str,
    *,
    source_id: str = "",
    exchange: str = "",
    event_uid: str = "",
) -> None:
    await asyncio.to_thread(
        _save_sync, path, ts, payload, source_id, exchange, event_uid
    )


async def load_recent_events(path: Path, limit: int) -> list[dict[str, Any]]:
    """Return up to `limit` events newest-first as plain dicts (already JSON-safe)."""
    return await asyncio.to_thread(_load_sync, path, limit)


# ---------------------------------------------------------------------------
# Closed trades table
# ---------------------------------------------------------------------------

_CLOSED_TRADE_COLUMNS = (
    "ts", "source", "market_symbol", "side", "entry", "exit",
    "size", "notional", "pnl", "pct", "is_win", "leverage",
    "wins", "total", "card_path",
    "trade_id", "fill_ids", "realization_kind",
    "source_id", "exchange", "market_key", "position_side",
    "native_trade_id", "event_uid", "lifecycle_opened_at",
    "holding_duration_ms", "holding_duration_basis",
)


def _save_closed_trade_sync(path: Path, record: dict) -> None:
    con = sqlite3.connect(path)
    try:
        placeholders = ", ".join("?" for _ in _CLOSED_TRADE_COLUMNS)
        cols = ", ".join(_CLOSED_TRADE_COLUMNS)
        values = tuple(record.get(c) for c in _CLOSED_TRADE_COLUMNS)
        if record.get("event_uid"):
            cursor = con.execute(
                f"INSERT OR IGNORE INTO closed_trades ({cols}) VALUES ({placeholders})",
                values,
            )
            if cursor.rowcount == 0:
                existing = con.execute(
                    "SELECT id FROM closed_trades WHERE event_uid=?",
                    (record["event_uid"],),
                ).fetchone()
                row_id = existing[0] if existing else None
            else:
                row_id = cursor.lastrowid
        else:
            cursor = con.execute(
                f"INSERT INTO closed_trades ({cols}) VALUES ({placeholders})",
                values,
            )
            row_id = cursor.lastrowid

        if row_id is not None:
            from .canonical_pnl import write_closed_trade_ledger_entry

            write_closed_trade_ledger_entry(con, row_id, record)
        con.commit()
    finally:
        con.close()


def _load_closed_trades_sync(path: Path, limit: int | None) -> list[dict]:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    if limit is None:
        rows = con.execute(
            "SELECT * FROM closed_trades ORDER BY id DESC"
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT * FROM closed_trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    con.close()
    return [dict(r) for r in rows]


async def save_closed_trade(path: Path, record: dict) -> None:
    """Persist a closed trade record to the closed_trades table."""
    await asyncio.to_thread(_save_closed_trade_sync, path, record)


async def load_closed_trades(path: Path, limit: int | None = None) -> list[dict]:
    """Return closed trades newest-first. If limit is None, return all."""
    return await asyncio.to_thread(_load_closed_trades_sync, path, limit)


def _delete_closed_trades_by_source_sync(path: Path, source: str) -> int:
    """Delete all closed_trades rows WHERE source = ?  Returns row count deleted."""
    con = sqlite3.connect(path)
    try:
        row_ids = [
            row[0]
            for row in con.execute(
                "SELECT id FROM closed_trades WHERE source = ?", (source,)
            ).fetchall()
        ]
        from .canonical_pnl import retract_closed_trade_rows

        retract_closed_trade_rows(
            con, row_ids, reason="legacy source-scoped reconciliation"
        )
        cur = con.execute(
            "DELETE FROM closed_trades WHERE source = ?", (source,)
        )
        deleted = cur.rowcount
        con.commit()
        return deleted
    finally:
        con.close()


async def delete_closed_trades_by_source(path: Path, source: str) -> int:
    """Delete all closed_trades rows for the given source name.

    Parametrized query — safe against SQL injection.
    Returns the number of rows deleted.
    Only touches rows matching the exact source string; all other rows
    (e.g. Lighter trades) are left completely untouched.
    """
    return await asyncio.to_thread(_delete_closed_trades_by_source_sync, path, source)


def _delete_closed_trades_by_source_since_sync(
    path: Path, source: str, ts_iso: str
) -> int:
    """Delete closed_trades rows WHERE source = ? AND ts >= ?.

    Used by the reconciler for a SCOPED delete-rebuild: only rows within the
    authoritative time window are removed; rows older than the window are
    preserved untouched.  Parametrized — safe against SQL injection.
    Returns the number of rows deleted.
    """
    con = sqlite3.connect(path)
    try:
        row_ids = [
            row[0]
            for row in con.execute(
                "SELECT id FROM closed_trades WHERE source = ? AND ts >= ?",
                (source, ts_iso),
            ).fetchall()
        ]
        from .canonical_pnl import retract_closed_trade_rows

        retract_closed_trade_rows(
            con, row_ids, reason="legacy source-window reconciliation"
        )
        cur = con.execute(
            "DELETE FROM closed_trades WHERE source = ? AND ts >= ?",
            (source, ts_iso),
        )
        deleted = cur.rowcount
        con.commit()
        return deleted
    finally:
        con.close()


async def delete_closed_trades_by_source_since(
    path: Path, source: str, ts_iso: str
) -> int:
    """Delete closed_trades rows for *source* with ts >= ts_iso (ISO-8601 string).

    Scoped alternative to delete_closed_trades_by_source: only rows inside the
    authoritative fetch window are removed; rows older than ts_iso are preserved
    intact.  Parametrized query — safe against SQL injection.
    Returns the number of rows deleted.
    """
    return await asyncio.to_thread(
        _delete_closed_trades_by_source_since_sync, path, source, ts_iso
    )


def _query_closed_trades_by_source_sync(path: Path, source: str) -> list[dict]:
    """Return all closed_trades rows WHERE source = ?, newest-first."""
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT * FROM closed_trades WHERE source = ? ORDER BY id DESC",
            (source,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


async def query_closed_trades_by_source(path: Path, source: str) -> list[dict]:
    """Return all closed_trades rows for the given source name, newest-first."""
    return await asyncio.to_thread(_query_closed_trades_by_source_sync, path, source)


def _query_closed_trades_by_identity_sync(
    path: Path, source_id: str, legacy_name: str, include_legacy: bool
) -> list[dict]:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        if include_legacy:
            rows = con.execute(
                "SELECT * FROM closed_trades WHERE source_id=? "
                "OR (source_id IS NULL AND source=?) ORDER BY id DESC",
                (source_id, legacy_name),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM closed_trades WHERE source_id=? ORDER BY id DESC",
                (source_id,),
            ).fetchall()
        return [dict(row) for row in rows]
    finally:
        con.close()


async def query_closed_trades_by_identity(
    path: Path,
    source_id: str,
    legacy_name: str,
    *,
    include_legacy: bool = True,
) -> list[dict]:
    return await asyncio.to_thread(
        _query_closed_trades_by_identity_sync,
        path,
        source_id,
        legacy_name,
        include_legacy,
    )


def _delete_closed_trades_by_identity_since_sync(
    path: Path,
    source_id: str,
    legacy_name: str,
    ts_iso: str,
    include_legacy: bool,
) -> int:
    con = sqlite3.connect(path)
    try:
        if include_legacy:
            row_ids = [
                row[0]
                for row in con.execute(
                    "SELECT id FROM closed_trades WHERE ts>=? AND "
                    "(source_id=? OR (source_id IS NULL AND source=?))",
                    (ts_iso, source_id, legacy_name),
                ).fetchall()
            ]
            from .canonical_pnl import retract_closed_trade_rows

            retract_closed_trade_rows(
                con, row_ids, reason="account-window reconciliation"
            )
            cur = con.execute(
                "DELETE FROM closed_trades WHERE ts>=? AND "
                "(source_id=? OR (source_id IS NULL AND source=?))",
                (ts_iso, source_id, legacy_name),
            )
        else:
            row_ids = [
                row[0]
                for row in con.execute(
                    "SELECT id FROM closed_trades WHERE ts>=? AND source_id=?",
                    (ts_iso, source_id),
                ).fetchall()
            ]
            from .canonical_pnl import retract_closed_trade_rows

            retract_closed_trade_rows(
                con, row_ids, reason="account-window reconciliation"
            )
            cur = con.execute(
                "DELETE FROM closed_trades WHERE ts>=? AND source_id=?",
                (ts_iso, source_id),
            )
        con.commit()
        return cur.rowcount
    finally:
        con.close()


async def delete_closed_trades_by_identity_since(
    path: Path,
    source_id: str,
    legacy_name: str,
    ts_iso: str,
    *,
    include_legacy: bool = True,
) -> int:
    return await asyncio.to_thread(
        _delete_closed_trades_by_identity_since_sync,
        path,
        source_id,
        legacy_name,
        ts_iso,
        include_legacy,
    )


def _load_recorded_fill_ids_sync(path: Path) -> set[int]:
    """Return the union of all trade_id values and all ids inside fill_ids JSON lists.

    Used at boot time to build the dedup set so a fill is never recorded twice.
    Malformed / unparseable fill_ids rows are silently skipped.
    """
    con = sqlite3.connect(path)
    try:
        rows = con.execute(
            "SELECT trade_id, fill_ids FROM closed_trades"
        ).fetchall()
    finally:
        con.close()

    result: set[int] = set()
    for trade_id, fill_ids_raw in rows:
        if trade_id is not None:
            try:
                result.add(int(trade_id))
            except (ValueError, TypeError):
                pass
        if fill_ids_raw is not None:
            try:
                ids = json.loads(fill_ids_raw)
                if isinstance(ids, list):
                    for fid in ids:
                        try:
                            result.add(int(fid))
                        except (ValueError, TypeError):
                            pass
            except Exception:
                pass  # unparseable JSON — skip defensively
    return result


async def load_recorded_fill_ids(path: Path) -> set[int]:
    """Return the union of all fill ids (trade_id + fill_ids JSON) recorded in closed_trades.

    Defensive: unparseable rows / values are silently skipped.
    """
    return await asyncio.to_thread(_load_recorded_fill_ids_sync, path)


def _load_recorded_event_uids_sync(path: Path) -> set[str]:
    con = sqlite3.connect(path)
    try:
        existing = {r[1] for r in con.execute("PRAGMA table_info(closed_trades)")}
        if "event_uid" not in existing:
            return set()
        rows = con.execute(
            "SELECT event_uid FROM closed_trades WHERE event_uid IS NOT NULL"
        ).fetchall()
        return {str(row[0]) for row in rows if row[0]}
    finally:
        con.close()


async def load_recorded_event_uids(path: Path) -> set[str]:
    """Return v2 source-scoped realization idempotency keys."""
    return await asyncio.to_thread(_load_recorded_event_uids_sync, path)


def _load_recorded_trade_uids_sync(path: Path) -> set[str]:
    """Return source-scoped fill identities already persisted in ``events``.

    Event rows store ``<fill-uid>|<event-kind>``. Keeping the fill identity
    across restarts prevents a REST replay from being classified as a new
    position change and sent to Telegram again.
    """
    con = sqlite3.connect(path)
    try:
        rows = con.execute(
            "SELECT event_uid FROM events WHERE event_uid IS NOT NULL"
        ).fetchall()
    finally:
        con.close()

    result: set[str] = set()
    for (event_uid,) in rows:
        value = str(event_uid or "")
        fill_uid, separator, kind = value.rpartition("|")
        if separator and kind in {"OPEN", "CLOSE", "SIZE_CHANGE", "REDUCE"}:
            result.add(fill_uid)
        elif value:
            # Preserve already-normalized legacy rows that did not include an
            # event-kind suffix.
            result.add(value)
    return result


async def load_recorded_trade_uids(path: Path) -> set[str]:
    """Return all source-scoped fill identities previously recorded."""
    return await asyncio.to_thread(_load_recorded_trade_uids_sync, path)


def _save_cursor_sync(
    path: Path, source_id: str, cursor_key: str, cursor: str, updated_at: str
) -> None:
    con = sqlite3.connect(path)
    try:
        con.execute(
            "INSERT INTO source_cursors(source_id, cursor_key, cursor, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(source_id, cursor_key) DO UPDATE SET "
            "cursor=excluded.cursor, updated_at=excluded.updated_at",
            (source_id, cursor_key, cursor, updated_at),
        )
        con.commit()
    finally:
        con.close()


def _load_cursor_sync(path: Path, source_id: str, cursor_key: str) -> str | None:
    con = sqlite3.connect(path)
    try:
        row = con.execute(
            "SELECT cursor FROM source_cursors WHERE source_id=? AND cursor_key=?",
            (source_id, cursor_key),
        ).fetchone()
        return str(row[0]) if row else None
    finally:
        con.close()


async def save_source_cursor(
    path: Path, source_id: str, cursor_key: str, cursor: str, updated_at: str
) -> None:
    await asyncio.to_thread(
        _save_cursor_sync, path, source_id, cursor_key, cursor, updated_at
    )


async def load_source_cursor(
    path: Path, source_id: str, cursor_key: str = "trades"
) -> str | None:
    return await asyncio.to_thread(_load_cursor_sync, path, source_id, cursor_key)


def _notification_status_sync(path: Path, event_uid: str) -> str | None:
    con = sqlite3.connect(path)
    try:
        row = con.execute(
            "SELECT status FROM notification_outbox WHERE event_uid=?",
            (event_uid,),
        ).fetchone()
        return str(row[0]) if row else None
    finally:
        con.close()


def _enqueue_notification_sync(
    path: Path,
    event_uid: str,
    destination: str,
    payload: str,
    now_iso: str,
) -> bool:
    # A failed delivery is retryable, but the retry must be claimed atomically
    # so two concurrent producers cannot both resend the same alert.  Pending
    # rows are treated as a short lease: if the process died during delivery,
    # a later attempt may reclaim the row after five minutes.
    con = sqlite3.connect(path)
    try:
        cursor = con.execute(
            "INSERT INTO notification_outbox"
            "(event_uid, destination, payload, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'pending', ?, ?) "
            "ON CONFLICT(event_uid) DO UPDATE SET "
            "destination=excluded.destination, payload=excluded.payload, "
            "status='pending', updated_at=excluded.updated_at, last_error=NULL "
            "WHERE notification_outbox.status='failed' "
            "OR (notification_outbox.status='pending' "
            "AND (julianday(excluded.updated_at) "
            "- julianday(notification_outbox.updated_at)) * 86400 >= 300)",
            (event_uid, destination, payload, now_iso, now_iso),
        )
        con.commit()
        return cursor.rowcount == 1
    finally:
        con.close()


def _mark_notification_sync(
    path: Path, event_uid: str, status: str, now_iso: str, error: str
) -> None:
    con = sqlite3.connect(path)
    try:
        con.execute(
            "UPDATE notification_outbox SET status=?, attempts=attempts+1, "
            "updated_at=?, last_error=? WHERE event_uid=?",
            (status, now_iso, error or None, event_uid),
        )
        con.commit()
    finally:
        con.close()


async def notification_status(path: Path, event_uid: str) -> str | None:
    return await asyncio.to_thread(_notification_status_sync, path, event_uid)


async def enqueue_notification(
    path: Path, event_uid: str, destination: str, payload: str, now_iso: str
) -> bool:
    return await asyncio.to_thread(
        _enqueue_notification_sync,
        path,
        event_uid,
        destination,
        payload,
        now_iso,
    )


async def mark_notification(
    path: Path, event_uid: str, status: str, now_iso: str, error: str = ""
) -> None:
    if status not in {"pending", "sent", "failed"}:
        raise ValueError("invalid notification status")
    await asyncio.to_thread(
        _mark_notification_sync, path, event_uid, status, now_iso, error
    )


# ---------------------------------------------------------------------------
# Backfill helper — reconstruct closed_trades from the events table
# ---------------------------------------------------------------------------

import logging as _logging

_log = _logging.getLogger(__name__)


def _backfill_closed_trades_sync(path: Path) -> int:
    """Worker for backfill_closed_trades_from_events.

    Reads ALL rows from the ``events`` table, identifies CLOSE events, and
    inserts synthesised ``closed_trades`` rows for each one found.

    Returns the number of rows inserted.

    Caveat: recovery is only as complete and accurate as the ``events`` table
    preserved.  Events that were never written to the DB (e.g. from runs before
    DB persistence existed) are unrecoverable, and ``wins``/``total`` counters
    are re-derived from the back-filled set only — they will not match the
    original runtime values.
    """
    con = sqlite3.connect(path)
    try:
        # ── Idempotency guard ─────────────────────────────────────────────────
        (existing,) = con.execute("SELECT COUNT(*) FROM closed_trades").fetchone()
        if existing >= 1:
            return 0

        # ── Load ALL events in chronological order (oldest first) ─────────────
        rows = con.execute(
            "SELECT id, ts, payload FROM events ORDER BY id ASC"
        ).fetchall()

        good_records: list[dict] = []

        for row_id, row_ts, raw_payload in rows:
            try:
                payload = json.loads(raw_payload)
            except Exception:
                continue  # unparseable JSON — skip

            # Accept both "CLOSE" string (EventKind.value) and wrapped shapes
            kind = payload.get("kind") or payload.get("type") or ""
            if str(kind).upper() != "CLOSE":
                continue

            try:
                trade = payload.get("trade") or {}
                pos_b = payload.get("position_before") or {}

                # Require at minimum a non-empty trade dict with a symbol or
                # price — a bare {"kind":"CLOSE"} with no trade data is not
                # a parseable trade record and should be skipped.
                if not trade or not (trade.get("market_symbol") or trade.get("price")):
                    continue

                # ── Timestamps ────────────────────────────────────────────────
                ts = (
                    trade.get("timestamp")
                    or payload.get("ts")
                    or row_ts
                )

                # ── Core trade fields ─────────────────────────────────────────
                market_symbol = (
                    trade.get("market_symbol")
                    or pos_b.get("market_symbol")
                    or payload.get("market_symbol")
                )
                exit_price_raw = trade.get("price") or payload.get("exit")
                source = (
                    trade.get("source")
                    or payload.get("source")
                    or pos_b.get("source")
                )

                # ── Position-before fields ────────────────────────────────────
                side = pos_b.get("side") or trade.get("side") or payload.get("side")
                entry_raw = pos_b.get("avg_entry_price") or payload.get("entry")
                size_raw = pos_b.get("size") or trade.get("size") or payload.get("size")
                notional_raw = pos_b.get("notional_usd") or payload.get("notional")

                leverage_raw = payload.get("leverage")

                # ── Recompute pnl/pct when we have the needed fields ───────────
                pnl_str: str | None = None
                pct_str: str | None = None
                is_win = 0

                if entry_raw is not None and exit_price_raw is not None and side:
                    try:
                        entry_f = float(entry_raw)
                        exit_f = float(exit_price_raw)
                        size_f = float(size_raw) if size_raw is not None else None

                        if size_f is not None:
                            if side == "long":
                                pnl = (exit_f - entry_f) * size_f
                            else:
                                pnl = (entry_f - exit_f) * size_f
                            pnl_str = str(pnl)
                            is_win = 1 if pnl > 0 else 0

                        if entry_f:
                            if side == "long":
                                pct = (exit_f - entry_f) / entry_f * 100
                            else:
                                pct = (entry_f - exit_f) / entry_f * 100
                            pct_str = str(pct)
                    except (ValueError, TypeError, ZeroDivisionError):
                        # Fall back to whatever the payload already contains
                        pnl_str = str(payload["pnl"]) if payload.get("pnl") is not None else None
                        pct_str = str(payload["pct"]) if payload.get("pct") is not None else None
                        is_win = int(bool(payload.get("is_win", 0)))
                else:
                    # No entry/exit available — use pre-computed values if present
                    pnl_str = str(payload["pnl"]) if payload.get("pnl") is not None else None
                    pct_str = str(payload["pct"]) if payload.get("pct") is not None else None
                    is_win = int(bool(payload.get("is_win", 0)))

                record: dict = {
                    "ts": ts,
                    "source": str(source) if source is not None else None,
                    "market_symbol": str(market_symbol) if market_symbol is not None else None,
                    "side": side,
                    "entry": str(entry_raw) if entry_raw is not None else None,
                    "exit": str(exit_price_raw) if exit_price_raw is not None else None,
                    "size": str(size_raw) if size_raw is not None else None,
                    "notional": str(notional_raw) if notional_raw is not None else None,
                    "pnl": pnl_str,
                    "pct": pct_str,
                    "is_win": is_win,
                    "leverage": str(leverage_raw) if leverage_raw is not None else None,
                    # wins/total are re-derived below after collecting all rows
                    "wins": None,
                    "total": None,
                    "card_path": None,
                }
                good_records.append(record)
            except Exception as exc:
                _log.warning("backfill: skipping row id=%s: %s", row_id, exc)
                continue

        if not good_records:
            return 0

        # ── Derive cumulative wins/total counters (chronological order) ───────
        wins_so_far = 0
        for i, rec in enumerate(good_records, start=1):
            if rec["is_win"]:
                wins_so_far += 1
            rec["wins"] = wins_so_far
            rec["total"] = i

        # ── Bulk-insert ───────────────────────────────────────────────────────
        placeholders = ", ".join("?" for _ in _CLOSED_TRADE_COLUMNS)
        cols = ", ".join(_CLOSED_TRADE_COLUMNS)
        for rec in good_records:
            values = tuple(rec.get(c) for c in _CLOSED_TRADE_COLUMNS)
            con.execute(
                f"INSERT INTO closed_trades ({cols}) VALUES ({placeholders})",
                values,
            )
        con.commit()
        return len(good_records)
    finally:
        con.close()


async def backfill_closed_trades_from_events(path: Path) -> int:
    """One-time, idempotent helper: populate ``closed_trades`` from ``events``.

    If ``closed_trades`` already contains at least one row this function returns
    0 immediately and performs no writes — it is safe to call on every startup.

    Recovery is only as complete and accurate as the ``events`` table preserved.
    Events not in the DB are unrecoverable; ``wins``/``total`` counters reflect
    only the back-filled set and will not match original runtime values.

    Returns the number of rows inserted (0 if already populated or no CLOSE
    events found).
    """
    return await asyncio.to_thread(_backfill_closed_trades_sync, path)


# ---------------------------------------------------------------------------
# TG alerts table
# ---------------------------------------------------------------------------


def _save_tg_alert_sync(path: Path, ts: str, kind: str, text: str) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "INSERT INTO tg_alerts (ts, kind, text) VALUES (?, ?, ?)",
        (ts, kind, text),
    )
    con.commit()
    con.close()


def _load_tg_alerts_sync(path: Path, limit: int) -> list[dict]:
    con = sqlite3.connect(path)
    rows = con.execute(
        "SELECT ts, kind, text FROM tg_alerts ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    con.close()
    return [{"ts": r[0], "kind": r[1], "text": r[2]} for r in rows]


async def save_tg_alert(path: Path, ts: str, kind: str, text: str) -> None:
    """Persist a single Telegram alert row to the tg_alerts table."""
    await asyncio.to_thread(_save_tg_alert_sync, path, ts, kind, text)


async def load_tg_alerts(path: Path, limit: int = 100) -> list[dict]:
    """Return up to `limit` Telegram alert records newest-first."""
    return await asyncio.to_thread(_load_tg_alerts_sync, path, limit)
