"""Immutable per-account exchange ledgers and rebuildable projections.

The shared ``events.db`` remains a compatibility/archive store.  New raw fills
are copied into one append-only SQLite ledger per configured account.  Derived
realizations are kept separately in the same account database and may be
replaced by a projection run without mutating the exchange-fact tables.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "1"
_SAFE_ACCOUNT = re.compile(r"[^A-Za-z0-9_.-]+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _db_value(value: Any) -> Any:
    """Convert Decimal/dataclass scalar values to SQLite-safe primitives."""
    if value is None or isinstance(value, (str, int, float, bytes)):
        return value
    return str(value)


def _trade_mapping(trade: Any) -> dict[str, Any]:
    if isinstance(trade, Mapping):
        return dict(trade)
    if is_dataclass(trade):
        return asdict(trade)
    return {
        key: getattr(trade, key, None)
        for key in (
            "trade_id", "timestamp", "market_id", "market_symbol", "side",
            "size", "price", "tx_hash", "source", "realized_pnl",
            "closed_pnl", "start_position", "source_id", "exchange",
            "native_trade_id", "position_side", "dir",
        )
    }


def _trade_uid(account_id: str, trade: Any, values: Mapping[str, Any]) -> str:
    native = str(values.get("native_trade_id") or values.get("trade_id") or "")
    market = str(values.get("market_id") or values.get("market_symbol") or "")
    side = str(values.get("position_side") or "BOTH")
    return f"{account_id}|{market}|{side}|{native}"


def account_db_path(data_dir: str | Path, account_id: str) -> Path:
    """Return the stable physical ledger path for one account."""
    safe = _SAFE_ACCOUNT.sub("_", str(account_id).strip()).strip("._") or "account"
    return Path(data_dir) / "accounts" / f"{safe}.db"


def _init_sync(path: Path, *, account_id: str, exchange: str, display_name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS account_meta (
                account_id TEXT PRIMARY KEY,
                exchange TEXT NOT NULL DEFAULT '',
                display_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS exchange_fills (
                fill_uid TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                exchange TEXT NOT NULL DEFAULT '',
                occurred_at TEXT NOT NULL,
                market_id INTEGER,
                market_symbol TEXT NOT NULL DEFAULT '',
                side TEXT NOT NULL DEFAULT '',
                position_side TEXT NOT NULL DEFAULT 'BOTH',
                size TEXT,
                price TEXT,
                realized_pnl TEXT,
                closed_pnl TEXT,
                start_position TEXT,
                native_trade_id TEXT,
                tx_hash TEXT,
                raw_json TEXT NOT NULL,
                first_seen_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_exchange_fills_time
                ON exchange_fills(occurred_at, fill_uid);
            CREATE TABLE IF NOT EXISTS fill_observations (
                observation_id TEXT PRIMARY KEY,
                fill_uid TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                raw_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_fill_observations_uid
                ON fill_observations(fill_uid, observed_at);
            CREATE TABLE IF NOT EXISTS pnl_realizations (
                realization_uid TEXT PRIMARY KEY,
                fill_uid TEXT,
                occurred_at TEXT NOT NULL,
                realization_kind TEXT NOT NULL DEFAULT 'FULL',
                payload_json TEXT NOT NULL,
                projection_version TEXT NOT NULL,
                projected_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_pnl_realizations_time
                ON pnl_realizations(occurred_at, realization_uid);
            CREATE TABLE IF NOT EXISTS projection_runs (
                run_id TEXT PRIMARY KEY,
                cutoff_utc TEXT NOT NULL,
                created_at TEXT NOT NULL,
                source_label TEXT NOT NULL DEFAULT '',
                rows_projected INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        now = _now()
        con.execute(
            """INSERT INTO account_meta(account_id, exchange, display_name, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(account_id) DO UPDATE SET
                 exchange=CASE WHEN excluded.exchange <> '' THEN excluded.exchange ELSE account_meta.exchange END,
                 display_name=CASE WHEN excluded.display_name <> '' THEN excluded.display_name ELSE account_meta.display_name END,
                 updated_at=excluded.updated_at""",
            (account_id, exchange or "", display_name or account_id, now, now),
        )
        con.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (SCHEMA_VERSION,),
        )
        con.commit()
    finally:
        con.close()


async def init_account_ledger(
    path: str | Path, *, account_id: str, exchange: str = "", display_name: str = ""
) -> None:
    await asyncio.to_thread(
        _init_sync,
        Path(path),
        account_id=account_id,
        exchange=exchange,
        display_name=display_name,
    )


def _append_trade_sync(
    path: Path,
    *,
    account_id: str,
    exchange: str,
    trade: Any,
    raw_payload: Any = None,
    observed_at: str | None = None,
) -> str:
    values = _trade_mapping(trade)
    uid = _trade_uid(account_id, trade, values)
    occurred_at = str(values.get("timestamp") or values.get("ts") or _now())
    raw = raw_payload if raw_payload is not None else values
    raw_json = _json(raw)
    payload_hash = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
    seen = observed_at or _now()
    observation_id = hashlib.sha256(f"{uid}|{payload_hash}".encode("utf-8")).hexdigest()
    con = sqlite3.connect(path)
    try:
        con.execute(
            """INSERT OR IGNORE INTO exchange_fills(
                fill_uid, account_id, exchange, occurred_at, market_id,
                market_symbol, side, position_side, size, price, realized_pnl,
                closed_pnl, start_position, native_trade_id, tx_hash, raw_json,
                first_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                uid, account_id, exchange or str(values.get("exchange") or ""),
                occurred_at, values.get("market_id"),
                str(values.get("market_symbol") or ""), str(values.get("side") or ""),
                str(values.get("position_side") or "BOTH"),
                _db_value(values.get("size")), _db_value(values.get("price")),
                _db_value(values.get("realized_pnl")),
                _db_value(values.get("closed_pnl") or values.get("realized_pnl")),
                _db_value(values.get("start_position")),
                str(values.get("native_trade_id") or values.get("trade_id") or ""),
                str(values.get("tx_hash") or ""), raw_json, seen,
            ),
        )
        # A changed exchange response becomes a new observation; the canonical
        # fill row itself is never updated.
        con.execute(
            """INSERT OR IGNORE INTO fill_observations(
                observation_id, fill_uid, observed_at, payload_hash, raw_json
            ) VALUES (?, ?, ?, ?, ?)""",
            (observation_id, uid, seen, payload_hash, raw_json),
        )
        con.commit()
    finally:
        con.close()
    return uid


async def append_trade(
    path: str | Path,
    *,
    account_id: str,
    exchange: str,
    trade: Any,
    raw_payload: Any = None,
    observed_at: str | None = None,
) -> str:
    return await asyncio.to_thread(
        _append_trade_sync,
        Path(path), account_id=account_id, exchange=exchange, trade=trade,
        raw_payload=raw_payload, observed_at=observed_at,
    )


def _append_realization_sync(
    path: Path,
    *,
    record: Mapping[str, Any],
    projection_version: str = SCHEMA_VERSION,
    fill_uid: str | None = None,
) -> str:
    uid = str(record.get("event_uid") or record.get("native_trade_id") or "")
    if not uid:
        raw = _json(dict(record))
        uid = "legacy:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
    payload = _json(dict(record))
    con = sqlite3.connect(path)
    try:
        con.execute(
            """INSERT OR IGNORE INTO pnl_realizations(
                realization_uid, fill_uid, occurred_at, realization_kind,
                payload_json, projection_version, projected_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                uid, fill_uid or record.get("event_uid"),
                str(record.get("ts") or _now()),
                str(record.get("realization_kind") or "FULL"), payload,
                projection_version, _now(),
            ),
        )
        con.commit()
    finally:
        con.close()
    return uid


async def append_realization(
    path: str | Path,
    *,
    record: Mapping[str, Any],
    projection_version: str = SCHEMA_VERSION,
    fill_uid: str | None = None,
) -> str:
    return await asyncio.to_thread(
        _append_realization_sync,
        Path(path), record=record, projection_version=projection_version,
        fill_uid=fill_uid,
    )


def _replace_realizations_sync(
    path: Path,
    *,
    records: Iterable[Mapping[str, Any]],
    cutoff_utc: str,
    projection_version: str = SCHEMA_VERSION,
    run_id: str = "",
) -> dict[str, int]:
    """Replace only the derived projection at/after a cutoff.

    Raw ``exchange_fills`` are never touched.  This is the safe operation for
    an exchange backfill: derived rows in the affected reporting window are
    rebuilt, while older realizations remain available as context/history.
    """
    con = sqlite3.connect(path)
    try:
        deleted = con.execute(
            "DELETE FROM pnl_realizations WHERE occurred_at >= ?", (cutoff_utc,)
        ).rowcount
        inserted = 0
        for record in records:
            uid = str(record.get("event_uid") or record.get("native_trade_id") or "")
            if not uid:
                raw = _json(dict(record))
                uid = "legacy:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
            cursor = con.execute(
                """INSERT OR IGNORE INTO pnl_realizations(
                    realization_uid, fill_uid, occurred_at, realization_kind,
                    payload_json, projection_version, projected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    uid, record.get("event_uid"), str(record.get("ts") or _now()),
                    str(record.get("realization_kind") or "FULL"),
                    _json(dict(record)), projection_version, _now(),
                ),
            )
            inserted += int(cursor.rowcount == 1)
        con.execute(
            "INSERT OR REPLACE INTO projection_runs(run_id, cutoff_utc, created_at, source_label, rows_projected) VALUES (?, ?, ?, ?, ?)",
            (run_id or hashlib.sha256(f"{cutoff_utc}|{_now()}".encode()).hexdigest(), cutoff_utc, _now(), "reconcile", inserted),
        )
        con.commit()
        return {"deleted": deleted, "inserted": inserted}
    finally:
        con.close()


async def replace_realizations(
    path: str | Path,
    *,
    records: Iterable[Mapping[str, Any]],
    cutoff_utc: str,
    projection_version: str = SCHEMA_VERSION,
    run_id: str = "",
) -> dict[str, int]:
    return await asyncio.to_thread(
        _replace_realizations_sync,
        Path(path), records=list(records), cutoff_utc=cutoff_utc,
        projection_version=projection_version, run_id=run_id,
    )


def _load_realizations_sync(path: Path) -> list[dict[str, Any]]:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        meta = con.execute(
            "SELECT account_id, exchange, display_name FROM account_meta LIMIT 1"
        ).fetchone()
        account_id = str(meta["account_id"]) if meta else ""
        exchange = str(meta["exchange"]) if meta else ""
        display_name = str(meta["display_name"]) if meta else account_id
        rows = con.execute(
            "SELECT payload_json FROM pnl_realizations ORDER BY occurred_at DESC, realization_uid DESC"
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            # Legacy rows may predate source_id.  The physical account file is
            # authoritative for that identity, so normalize it at read time
            # without rewriting the historical projection payload.
            payload.setdefault("source_id", account_id)
            payload.setdefault("exchange", exchange)
            payload.setdefault("source", display_name)
            result.append(payload)
        return result
    finally:
        con.close()


async def load_realizations(path: str | Path) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_load_realizations_sync, Path(path))


def _migrate_shared_sync(
    shared_path: Path,
    account_paths: Mapping[str, Path],
    aliases: Mapping[tuple[str, str], str],
    metadata: Mapping[str, Mapping[str, str]],
) -> dict[str, int]:
    con = sqlite3.connect(shared_path)
    con.row_factory = sqlite3.Row
    counts = {account_id: 0 for account_id in account_paths}
    try:
        for row in con.execute("SELECT * FROM events ORDER BY id"):
            payload = json.loads(row["payload"])
            trade = payload.get("trade") if isinstance(payload, Mapping) else None
            if not isinstance(trade, Mapping):
                continue
            source_id = str(row["source_id"] or trade.get("source_id") or "")
            source = str(trade.get("source") or "")
            exchange = str(row["exchange"] or trade.get("exchange") or "")
            account_id = source_id or aliases.get((source, exchange)) or aliases.get((source, ""))
            if account_id not in account_paths:
                continue
            _append_trade_sync(
                account_paths[account_id], account_id=account_id, exchange=exchange,
                trade=trade, raw_payload=payload, observed_at=row["ts"],
            )
            counts[account_id] += 1

        for row in con.execute("SELECT * FROM closed_trades ORDER BY id"):
            source_id = str(row["source_id"] or "")
            source = str(row["source"] or "")
            exchange = str(row["exchange"] or "")
            account_id = source_id or aliases.get((source, exchange)) or aliases.get((source, ""))
            if account_id not in account_paths:
                continue
            _append_realization_sync(account_paths[account_id], record=dict(row))
        return counts
    finally:
        con.close()


async def migrate_shared_db(
    shared_path: str | Path,
    *,
    account_paths: Mapping[str, Path],
    aliases: Mapping[tuple[str, str], str],
    metadata: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, int]:
    """Idempotently copy legacy events/realizations into account ledgers."""
    for account_id, path in account_paths.items():
        info = (metadata or {}).get(account_id, {})
        await init_account_ledger(
            path, account_id=account_id, exchange=info.get("exchange", ""),
            display_name=info.get("display_name", account_id),
        )
    return await asyncio.to_thread(
        _migrate_shared_sync,
        Path(shared_path), account_paths, aliases, metadata or {},
    )


async def load_all_realizations(account_paths: Iterable[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in account_paths:
        if path.exists():
            rows.extend(await load_realizations(path))
    rows.sort(key=lambda row: str(row.get("ts") or ""), reverse=True)
    return rows
