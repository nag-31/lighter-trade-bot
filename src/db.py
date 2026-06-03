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
