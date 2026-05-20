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
