from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ..core.engine import ReconstructionResult
from ..core.models import RawFill, RoundTrip


DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "pnl_analytics.db"


class AnalyticsStore:
    def __init__(self, path: Path | str = DEFAULT_DB):
        self.path = Path(path)

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS raw_fills (
                    source TEXT NOT NULL,
                    account TEXT NOT NULL,
                    fill_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    side TEXT NOT NULL,
                    qty TEXT NOT NULL,
                    price TEXT NOT NULL,
                    fee TEXT NOT NULL,
                    fee_token TEXT,
                    exchange_realized_pnl TEXT,
                    funding TEXT,
                    raw_json TEXT NOT NULL,
                    PRIMARY KEY (source, account, fill_id)
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS round_trips (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    account TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT NOT NULL,
                    closed_qty TEXT NOT NULL,
                    cost_basis TEXT NOT NULL,
                    gross_pnl TEXT NOT NULL,
                    net_pnl TEXT NOT NULL,
                    fees TEXT NOT NULL,
                    funding TEXT NOT NULL,
                    return_on_cost TEXT,
                    is_win INTEGER NOT NULL,
                    funding_status TEXT NOT NULL,
                    entry_fill_ids TEXT NOT NULL,
                    exit_fill_ids TEXT NOT NULL
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS realizations (
                    round_trip_id TEXT NOT NULL,
                    fill_id TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    closed_qty TEXT NOT NULL,
                    entry_price TEXT NOT NULL,
                    exit_price TEXT NOT NULL,
                    gross_pnl TEXT NOT NULL,
                    exchange_realized_pnl TEXT,
                    allocated_open_fee TEXT NOT NULL,
                    close_fee TEXT NOT NULL,
                    funding TEXT NOT NULL,
                    funding_status TEXT NOT NULL,
                    net_pnl TEXT NOT NULL
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS sent_alerts (
                    round_trip_id TEXT PRIMARY KEY,
                    sent_at TEXT NOT NULL,
                    channel TEXT NOT NULL
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS alert_state (
                    channel TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    PRIMARY KEY (channel, key)
                )
                """
            )

    def save_raw_fills(self, fills: list[RawFill]) -> tuple[int, int]:
        inserted = skipped = 0
        with sqlite3.connect(self.path) as con:
            for fill in fills:
                cur = con.execute(
                    """
                    INSERT OR IGNORE INTO raw_fills
                    (source, account, fill_id, symbol, ts, side, qty, price, fee,
                     fee_token, exchange_realized_pnl, funding, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fill.source,
                        fill.account,
                        fill.fill_id,
                        fill.symbol,
                        fill.timestamp.isoformat(),
                        fill.side,
                        str(fill.qty),
                        str(fill.price),
                        str(fill.fee),
                        fill.fee_token,
                        str(fill.exchange_realized_pnl) if fill.exchange_realized_pnl is not None else None,
                        str(fill.funding) if fill.funding is not None else None,
                        json.dumps(fill.raw, sort_keys=True, default=str),
                    ),
                )
                if cur.rowcount:
                    inserted += 1
                else:
                    skipped += 1
        return inserted, skipped

    def save_result(self, result: ReconstructionResult) -> None:
        self.save_raw_fills(result.raw_fills)
        with sqlite3.connect(self.path) as con:
            for rt in result.round_trips:
                self._save_round_trip(con, rt)

    def was_alert_sent(self, round_trip_id: str, *, channel: str = "telegram") -> bool:
        with sqlite3.connect(self.path) as con:
            row = con.execute(
                "SELECT 1 FROM sent_alerts WHERE round_trip_id = ? AND channel = ?",
                (round_trip_id, channel),
            ).fetchone()
            return row is not None

    def mark_alert_sent(self, round_trip_id: str, sent_at: str, *, channel: str = "telegram") -> None:
        with sqlite3.connect(self.path) as con:
            con.execute(
                "INSERT OR REPLACE INTO sent_alerts (round_trip_id, sent_at, channel) VALUES (?, ?, ?)",
                (round_trip_id, sent_at, channel),
            )

    def get_alert_state(self, key: str, *, channel: str = "telegram") -> str | None:
        with sqlite3.connect(self.path) as con:
            row = con.execute(
                "SELECT value FROM alert_state WHERE channel = ? AND key = ?",
                (channel, key),
            ).fetchone()
            return None if row is None else str(row[0])

    def set_alert_state(self, key: str, value: str, *, channel: str = "telegram") -> None:
        with sqlite3.connect(self.path) as con:
            con.execute(
                "INSERT OR REPLACE INTO alert_state (channel, key, value) VALUES (?, ?, ?)",
                (channel, key, value),
            )

    @staticmethod
    def _save_round_trip(con: sqlite3.Connection, rt: RoundTrip) -> None:
        con.execute("DELETE FROM realizations WHERE round_trip_id = ?", (rt.id,))
        con.execute(
            """
            INSERT OR REPLACE INTO round_trips
            (id, source, account, symbol, direction, opened_at, closed_at,
             closed_qty, cost_basis, gross_pnl, net_pnl, fees, funding,
             return_on_cost, is_win, funding_status, entry_fill_ids, exit_fill_ids)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rt.id,
                rt.source,
                rt.account,
                rt.symbol,
                rt.direction,
                rt.opened_at.isoformat(),
                rt.closed_at.isoformat(),
                str(rt.closed_qty),
                str(rt.cost_basis),
                str(rt.gross_pnl),
                str(rt.net_pnl),
                str(rt.fees),
                str(rt.funding),
                str(rt.return_on_cost) if rt.return_on_cost is not None else None,
                1 if rt.is_win else 0,
                rt.funding_status,
                json.dumps(rt.entry_fill_ids),
                json.dumps(rt.exit_fill_ids),
            ),
        )
        for r in rt.realizations:
            con.execute(
                """
                INSERT INTO realizations
                (round_trip_id, fill_id, ts, closed_qty, entry_price, exit_price,
                 gross_pnl, exchange_realized_pnl, allocated_open_fee, close_fee,
                 funding, funding_status, net_pnl)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rt.id,
                    r.fill_id,
                    r.timestamp.isoformat(),
                    str(r.closed_qty),
                    str(r.entry_price),
                    str(r.exit_price),
                    str(r.gross_pnl),
                    str(r.exchange_realized_pnl) if r.exchange_realized_pnl is not None else None,
                    str(r.allocated_open_fee),
                    str(r.close_fee),
                    str(r.funding),
                    r.funding_status,
                    str(r.net_pnl),
                ),
            )
