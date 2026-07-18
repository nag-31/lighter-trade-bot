"""SQLite persistence for the local portfolio dashboard."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def _init_sync(path: Path) -> None:
    con = _connect(path)
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS portfolio_addresses (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                address    TEXT    NOT NULL UNIQUE,
                label      TEXT,
                enabled    INTEGER NOT NULL DEFAULT 1,
                excluded   INTEGER NOT NULL DEFAULT 0,
                created_at TEXT    NOT NULL,
                updated_at TEXT    NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                address_id INTEGER NOT NULL,
                ts         TEXT    NOT NULL,
                status     TEXT    NOT NULL,
                total_usd  REAL,
                payload    TEXT    NOT NULL,
                error      TEXT,
                FOREIGN KEY(address_id) REFERENCES portfolio_addresses(id)
            )
        """)
        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_latest
            ON portfolio_snapshots(address_id, id DESC)
        """)
        # Guarded migration: add `excluded` column to pre-existing databases.
        cols = {row["name"] for row in con.execute("PRAGMA table_info(portfolio_addresses)").fetchall()}
        if "excluded" not in cols:
            con.execute(
                "ALTER TABLE portfolio_addresses ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0"
            )
        con.commit()
    finally:
        con.close()


async def init_portfolio_db(path: Path) -> None:
    await asyncio.to_thread(_init_sync, path)


def _address_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """Normalize an address row: coerce `excluded` to a real bool."""
    if row is None:
        return None
    d = dict(row)
    d["excluded"] = bool(d.get("excluded", 0))
    return d


def _upsert_address_sync(path: Path, address: str, label: str | None = None) -> dict[str, Any]:
    now = utc_now_iso()
    con = _connect(path)
    try:
        existing = con.execute(
            "SELECT * FROM portfolio_addresses WHERE address = ?",
            (address,),
        ).fetchone()
        if existing:
            new_label = label if label is not None else existing["label"]
            con.execute(
                """
                UPDATE portfolio_addresses
                SET label = ?, enabled = 1, updated_at = ?
                WHERE id = ?
                """,
                (new_label, now, existing["id"]),
            )
            con.commit()
            row = con.execute(
                "SELECT * FROM portfolio_addresses WHERE id = ?",
                (existing["id"],),
            ).fetchone()
            return _address_dict(row)

        cur = con.execute(
            """
            INSERT INTO portfolio_addresses (address, label, enabled, created_at, updated_at)
            VALUES (?, ?, 1, ?, ?)
            """,
            (address, label, now, now),
        )
        con.commit()
        row = con.execute(
            "SELECT * FROM portfolio_addresses WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()
        return _address_dict(row)
    finally:
        con.close()


async def upsert_address(path: Path, address: str, label: str | None = None) -> dict[str, Any]:
    return await asyncio.to_thread(_upsert_address_sync, path, address, label)


def _list_addresses_sync(path: Path, *, include_disabled: bool = False) -> list[dict[str, Any]]:
    con = _connect(path)
    try:
        if include_disabled:
            rows = con.execute(
                "SELECT * FROM portfolio_addresses ORDER BY id ASC"
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM portfolio_addresses WHERE enabled = 1 ORDER BY id ASC"
            ).fetchall()
        return [_address_dict(r) for r in rows]
    finally:
        con.close()


async def list_addresses(path: Path, *, include_disabled: bool = False) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_list_addresses_sync, path, include_disabled=include_disabled)


def _get_address_sync(path: Path, address_id: int) -> dict[str, Any] | None:
    con = _connect(path)
    try:
        row = con.execute(
            "SELECT * FROM portfolio_addresses WHERE id = ? AND enabled = 1",
            (address_id,),
        ).fetchone()
        return _address_dict(row)
    finally:
        con.close()


async def get_address(path: Path, address_id: int) -> dict[str, Any] | None:
    return await asyncio.to_thread(_get_address_sync, path, address_id)


def _disable_address_sync(path: Path, address_id: int) -> bool:
    con = _connect(path)
    try:
        cur = con.execute(
            """
            UPDATE portfolio_addresses
            SET enabled = 0, updated_at = ?
            WHERE id = ? AND enabled = 1
            """,
            (utc_now_iso(), address_id),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


async def disable_address(path: Path, address_id: int) -> bool:
    return await asyncio.to_thread(_disable_address_sync, path, address_id)


def _save_snapshot_sync(
    path: Path,
    *,
    address_id: int,
    ts: str,
    status: str,
    total_usd: float | None,
    payload: dict[str, Any],
    error: str | None = None,
) -> dict[str, Any]:
    con = _connect(path)
    try:
        cur = con.execute(
            """
            INSERT INTO portfolio_snapshots
                (address_id, ts, status, total_usd, payload, error)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (address_id, ts, status, total_usd, json.dumps(payload, sort_keys=True), error),
        )
        con.commit()
        row = con.execute(
            "SELECT * FROM portfolio_snapshots WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()
        return dict(row)
    finally:
        con.close()


async def save_snapshot(
    path: Path,
    *,
    address_id: int,
    ts: str,
    status: str,
    total_usd: float | None,
    payload: dict[str, Any],
    error: str | None = None,
) -> dict[str, Any]:
    return await asyncio.to_thread(
        _save_snapshot_sync,
        path,
        address_id=address_id,
        ts=ts,
        status=status,
        total_usd=total_usd,
        payload=payload,
        error=error,
    )


def _latest_snapshots_sync(
    path: Path, successful_only: bool = False
) -> dict[int, dict[str, Any]]:
    status_filter = "WHERE status != 'error'" if successful_only else ""
    con = _connect(path)
    try:
        rows = con.execute(f"""
            SELECT s.*
            FROM portfolio_snapshots s
            JOIN (
                SELECT address_id, MAX(id) AS max_id
                FROM portfolio_snapshots
                {status_filter}
                GROUP BY address_id
            ) latest
              ON latest.address_id = s.address_id
             AND latest.max_id = s.id
        """).fetchall()
        out: dict[int, dict[str, Any]] = {}
        for row in rows:
            d = dict(row)
            try:
                d["payload"] = json.loads(d["payload"])
            except Exception:
                d["payload"] = {}
            out[int(d["address_id"])] = d
        return out
    finally:
        con.close()


async def latest_snapshots(
    path: Path, *, successful_only: bool = False
) -> dict[int, dict[str, Any]]:
    return await asyncio.to_thread(
        _latest_snapshots_sync, path, successful_only
    )

def _snapshot_history_sync(path: Path, address_id: int, limit: int = 25) -> list[dict[str, Any]]:
    con = _connect(path)
    try:
        rows = con.execute(
            """
            SELECT * FROM portfolio_snapshots
            WHERE address_id = ? AND status != 'error'
            ORDER BY id DESC
            LIMIT ?
            """,
            (address_id, limit),
        ).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            try:
                d["payload"] = json.loads(d["payload"])
            except Exception:
                d["payload"] = {}
            out.append(d)
        return out
    finally:
        con.close()


async def snapshot_history(path: Path, address_id: int, limit: int = 25) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_snapshot_history_sync, path, address_id, limit)


_UNSET: Any = object()


def _set_address_fields_sync(
    path: Path,
    address_id: int,
    *,
    label: Any = _UNSET,
    excluded: Any = _UNSET,
) -> dict[str, Any] | None:
    sets: list[str] = []
    params: list[Any] = []
    if label is not _UNSET:
        sets.append("label = ?")
        params.append(label)
    if excluded is not _UNSET:
        sets.append("excluded = ?")
        params.append(1 if excluded else 0)

    con = _connect(path)
    try:
        if sets:
            sets.append("updated_at = ?")
            params.append(utc_now_iso())
            params.append(address_id)
            cur = con.execute(
                f"UPDATE portfolio_addresses SET {', '.join(sets)} WHERE id = ? AND enabled = 1",
                params,
            )
            con.commit()
            if cur.rowcount == 0:
                return None
        row = con.execute(
            "SELECT * FROM portfolio_addresses WHERE id = ? AND enabled = 1",
            (address_id,),
        ).fetchone()
        return _address_dict(row)
    finally:
        con.close()


async def set_address_fields(
    path: Path,
    address_id: int,
    *,
    label: Any = _UNSET,
    excluded: Any = _UNSET,
) -> dict[str, Any] | None:
    """Update label and/or excluded for an enabled address. Returns the updated
    address dict, or None if the id is unknown/disabled."""
    return await asyncio.to_thread(
        _set_address_fields_sync, path, address_id, label=label, excluded=excluded
    )


def _aggregate_history_sync(
    path: Path, limit: int = 1000, address_ids: list[int] | None = None
) -> list[dict[str, Any]]:
    if address_ids == []:
        return []
    where = "a.enabled = 1 AND a.excluded = 0 AND s.status != 'error'"
    params: list[Any] = []
    if address_ids:
        where += " AND s.address_id IN (" + ", ".join("?" for _ in address_ids) + ")"
        params.extend(address_ids)
    con = _connect(path)
    try:
        rows = con.execute(
            """
            SELECT s.address_id AS address_id, s.ts AS ts, s.total_usd AS total_usd
            FROM portfolio_snapshots s
            JOIN portfolio_addresses a ON a.id = s.address_id
            WHERE """ + where + """
            ORDER BY s.ts ASC, s.id ASC
            """,
            params,
        ).fetchall()
    finally:
        con.close()

    last_known: dict[int, float] = {}
    points: list[dict[str, Any]] = []
    for row in rows:
        addr_id = int(row["address_id"])
        last_known[addr_id] = float(row["total_usd"] or 0.0)
        total = sum(last_known.values())
        points.append({"ts": row["ts"], "total_usd": total})

    if limit is not None and limit >= 0:
        return points[-limit:] if limit else []
    return points


async def aggregate_history(
    path: Path, limit: int = 1000, address_ids: list[int] | None = None
) -> list[dict[str, Any]]:
    """Carry-forward history for enabled, non-excluded addresses, optionally
    limited to an explicit wallet scope."""
    return await asyncio.to_thread(_aggregate_history_sync, path, limit, address_ids)
