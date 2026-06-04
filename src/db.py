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


def _init_sync(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            ts       TEXT    NOT NULL,
            payload  TEXT    NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS closed_trades (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           TEXT    NOT NULL,
            source       TEXT,
            market_symbol TEXT,
            side         TEXT,
            entry        TEXT,
            exit         TEXT,
            size         TEXT,
            notional     TEXT,
            pnl          TEXT,
            pct          TEXT,
            is_win       INTEGER,
            leverage     TEXT,
            wins         INTEGER,
            total        INTEGER,
            card_path    TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS tg_alerts (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            ts   TEXT    NOT NULL,
            kind TEXT    NOT NULL,
            text TEXT    NOT NULL
        )
    """)
    con.commit()
    con.close()


def _save_sync(path: Path, ts: str, payload: str) -> None:
    con = sqlite3.connect(path)
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


async def save_event(path: Path, ts: str, payload: str) -> None:
    await asyncio.to_thread(_save_sync, path, ts, payload)


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
)


def _save_closed_trade_sync(path: Path, record: dict) -> None:
    con = sqlite3.connect(path)
    placeholders = ", ".join("?" for _ in _CLOSED_TRADE_COLUMNS)
    cols = ", ".join(_CLOSED_TRADE_COLUMNS)
    values = tuple(record.get(c) for c in _CLOSED_TRADE_COLUMNS)
    con.execute(
        f"INSERT INTO closed_trades ({cols}) VALUES ({placeholders})",
        values,
    )
    con.commit()
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
