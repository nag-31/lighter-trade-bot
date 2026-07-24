"""Tests for backfill_closed_trades_from_events in db.py.

Mirrors tests/test_db_closed_trades.py style: uses tmp_path, asyncio.run helpers.

Payload shapes match the real serialisation in dashboard.py:
    _to_jsonable(Event) -> asdict(Event) with Decimal->str, datetime->ISO str.

Relevant dashboard.py lines:
  - save_event call at ~line 1188-1192: json.dumps(_to_jsonable(ev))
  - _to_jsonable at ~line 69-80: converts Decimal->str, datetime->ISO, dataclass->asdict
  - CLOSE record built at ~lines 1273-1289 for reference column mapping
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from src.db import (
    backfill_closed_trades_from_events,
    init_db,
    load_recorded_trade_uids,
    load_closed_trades,
    save_closed_trade,
    save_event,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def _close_event_payload(
    *,
    market_symbol: str = "BTC",
    side: str = "long",
    entry: str = "50000.0",
    exit_price: str = "55000.0",
    size: str = "0.1",
    ts: str = "2026-01-01T00:00:00+00:00",
    source: str = "HL",
    leverage: float | None = 10.0,
    notional_usd: str | None = None,
) -> dict:
    """Build a CLOSE event payload matching the real _to_jsonable(Event) shape.

    Structure mirrors Event -> asdict():
      {
        "kind": "CLOSE",
        "trade": { Trade fields with Decimal as str },
        "position_before": { Position fields with Decimal as str } | None,
        "position_after": None,
        "leverage": float | None,
      }
    """
    if notional_usd is None:
        notional_usd = str(float(size) * float(entry))

    return {
        "kind": "CLOSE",
        "trade": {
            "trade_id": 42,
            "timestamp": ts,
            "market_id": 1,
            "market_symbol": market_symbol,
            "side": side,
            "size": size,
            "price": exit_price,
            "tx_hash": "0xabc",
            "source": source,
            "realized_pnl": None,
            "dir": None,
            "closed_pnl": None,
        },
        "position_before": {
            "market_id": 1,
            "market_symbol": market_symbol,
            "side": side,
            "size": size,
            "avg_entry_price": entry,
            "source": source,
            "unrealized_pnl": None,
            "liquidation_px": None,
            "notional_usd": notional_usd,
        },
        "position_after": None,
        "leverage": leverage,
    }


def _open_event_payload(market_symbol: str = "ETH") -> dict:
    """Build a non-CLOSE event payload (OPEN kind) — should be skipped."""
    return {
        "kind": "OPEN",
        "trade": {
            "trade_id": 99,
            "timestamp": "2026-01-01T01:00:00+00:00",
            "market_id": 2,
            "market_symbol": market_symbol,
            "side": "long",
            "size": "1.0",
            "price": "3000.0",
            "tx_hash": "",
            "source": "HL",
            "realized_pnl": None,
            "dir": None,
            "closed_pnl": None,
        },
        "position_before": None,
        "position_after": {
            "market_id": 2,
            "market_symbol": market_symbol,
            "side": "long",
            "size": "1.0",
            "avg_entry_price": "3000.0",
            "source": "HL",
            "unrealized_pnl": None,
            "liquidation_px": None,
        },
        "leverage": 5.0,
    }


# ---------------------------------------------------------------------------
# Core backfill tests
# ---------------------------------------------------------------------------

class TestBackfillFromEvents:
    def test_inserts_expected_rows_with_recomputed_pnl(self, tmp_path: Path):
        """Backfill on a DB with two CLOSE events inserts two closed_trades rows
        and correctly recomputes pnl and pct from entry/exit/size.
        """
        db = tmp_path / "test.db"
        _run(init_db(db))

        p1 = _close_event_payload(
            market_symbol="BTC",
            side="long",
            entry="50000.0",
            exit_price="55000.0",
            size="0.1",
            ts="2026-01-01T00:00:00+00:00",
        )
        p2 = _close_event_payload(
            market_symbol="ETH",
            side="short",
            entry="3000.0",
            exit_price="2700.0",
            size="1.0",
            ts="2026-01-02T00:00:00+00:00",
        )
        _run(save_event(db, "2026-01-01T00:00:00+00:00", json.dumps(p1)))
        _run(save_event(db, "2026-01-02T00:00:00+00:00", json.dumps(p2)))

        count = _run(backfill_closed_trades_from_events(db))
        assert count == 2

        rows = _run(load_closed_trades(db, limit=None))
        assert len(rows) == 2

        # newest-first ordering (id DESC) — p2 is row[0]
        row_eth = rows[0]
        row_btc = rows[1]

        # BTC long: pnl = (55000 - 50000) * 0.1 = 500.0
        assert row_btc["market_symbol"] == "BTC"
        assert row_btc["side"] == "long"
        assert row_btc["entry"] == "50000.0"
        assert row_btc["exit"] == "55000.0"
        assert float(row_btc["pnl"]) == pytest.approx(500.0)
        assert float(row_btc["pct"]) == pytest.approx(10.0)
        assert row_btc["is_win"] == 1
        assert row_btc["leverage"] == "10.0"
        assert row_btc["card_path"] is None

        # ETH short: pnl = (3000 - 2700) * 1.0 = 300.0
        assert row_eth["market_symbol"] == "ETH"
        assert row_eth["side"] == "short"
        assert float(row_eth["pnl"]) == pytest.approx(300.0)
        assert float(row_eth["pct"]) == pytest.approx(10.0)
        assert row_eth["is_win"] == 1

    def test_backfill_is_idempotent_second_call_returns_zero(self, tmp_path: Path):
        """Second call returns 0 and does not duplicate rows."""
        db = tmp_path / "test.db"
        _run(init_db(db))
        p = _close_event_payload(ts="2026-01-01T00:00:00+00:00")
        _run(save_event(db, "2026-01-01T00:00:00+00:00", json.dumps(p)))

        first = _run(backfill_closed_trades_from_events(db))
        assert first == 1

        second = _run(backfill_closed_trades_from_events(db))
        assert second == 0

        rows = _run(load_closed_trades(db, limit=None))
        assert len(rows) == 1  # no duplicates

    def test_backfill_does_nothing_when_closed_trades_already_populated(self, tmp_path: Path):
        """If closed_trades already has rows (e.g. from live operation), backfill skips."""
        db = tmp_path / "test.db"
        _run(init_db(db))

        # Pre-populate closed_trades directly (simulates live running)
        existing = {
            "ts": "2025-12-01T00:00:00+00:00",
            "source": "HL",
            "market_symbol": "SOL",
            "side": "long",
            "entry": "100.0",
            "exit": "120.0",
            "size": "10.0",
            "notional": "1000.0",
            "pnl": "200.0",
            "pct": "20.0",
            "is_win": 1,
            "leverage": "5.0",
            "wins": 1,
            "total": 1,
            "card_path": None,
        }
        _run(save_closed_trade(db, existing))

        # Also add a CLOSE event — backfill should ignore it
        p = _close_event_payload(market_symbol="BTC", ts="2026-01-01T00:00:00+00:00")
        _run(save_event(db, "2026-01-01T00:00:00+00:00", json.dumps(p)))

        result = _run(backfill_closed_trades_from_events(db))
        assert result == 0

        rows = _run(load_closed_trades(db, limit=None))
        assert len(rows) == 1
        assert rows[0]["market_symbol"] == "SOL"

    def test_backfill_skips_malformed_and_non_close_events(self, tmp_path: Path):
        """Malformed payloads and non-CLOSE events are silently skipped; no exception raised."""
        db = tmp_path / "test.db"
        _run(init_db(db))

        # 1. Completely invalid JSON (saved via raw string)
        import sqlite3 as _sqlite3
        con = _sqlite3.connect(db)
        con.execute(
            "INSERT INTO events (ts, payload) VALUES (?, ?)",
            ("2026-01-01T00:00:00+00:00", "{not valid json"),
        )
        con.commit()
        con.close()

        # 2. Valid JSON but not CLOSE
        _run(save_event(db, "2026-01-01T01:00:00+00:00", json.dumps(_open_event_payload())))

        # 3. CLOSE but missing most fields
        _run(save_event(db, "2026-01-01T02:00:00+00:00", json.dumps({"kind": "CLOSE"})))

        # 4. One good CLOSE event
        p = _close_event_payload(market_symbol="ADA", ts="2026-01-01T03:00:00+00:00")
        _run(save_event(db, "2026-01-01T03:00:00+00:00", json.dumps(p)))

        # Should not raise; only the one valid CLOSE should be inserted
        count = _run(backfill_closed_trades_from_events(db))
        assert count == 1

        rows = _run(load_closed_trades(db, limit=None))
        assert len(rows) == 1
        assert rows[0]["market_symbol"] == "ADA"

    def test_backfill_returns_zero_on_empty_events_table(self, tmp_path: Path):
        """No events => nothing to backfill => returns 0."""
        db = tmp_path / "test.db"
        _run(init_db(db))

        count = _run(backfill_closed_trades_from_events(db))
        assert count == 0

        rows = _run(load_closed_trades(db, limit=None))
        assert rows == []

    def test_backfill_only_events_no_close_returns_zero(self, tmp_path: Path):
        """Events table has entries but none are CLOSE kind => returns 0."""
        db = tmp_path / "test.db"
        _run(init_db(db))
        _run(save_event(db, "2026-01-01T00:00:00+00:00", json.dumps(_open_event_payload("BTC"))))
        _run(save_event(db, "2026-01-01T01:00:00+00:00", json.dumps(_open_event_payload("ETH"))))

        count = _run(backfill_closed_trades_from_events(db))
        assert count == 0

    def test_backfill_wins_total_counters_derived_correctly(self, tmp_path: Path):
        """wins/total are cumulative counters over the backfilled set (oldest first)."""
        db = tmp_path / "test.db"
        _run(init_db(db))

        # win, loss, win  -> wins=[1,1,2]  total=[1,2,3]
        payloads = [
            _close_event_payload(
                entry="100.0", exit_price="110.0", side="long",
                ts=f"2026-01-0{i+1}T00:00:00+00:00"
            )
            for i in range(3)
        ]
        # Make second one a loss: long, exit < entry
        payloads[1]["trade"]["price"] = "90.0"
        payloads[1]["position_before"]["avg_entry_price"] = "100.0"

        for i, p in enumerate(payloads):
            _run(save_event(db, p["trade"]["timestamp"], json.dumps(p)))

        count = _run(backfill_closed_trades_from_events(db))
        assert count == 3

        rows = _run(load_closed_trades(db, limit=None))
        # newest-first: rows[0]=trade3, rows[1]=trade2, rows[2]=trade1
        assert rows[0]["total"] == 3
        assert rows[0]["wins"] == 2   # trade3 is a win
        assert rows[1]["total"] == 2
        assert rows[1]["wins"] == 1   # trade2 is a loss, wins stays 1
        assert rows[2]["total"] == 1
        assert rows[2]["wins"] == 1   # trade1 is a win


def test_load_recorded_trade_uids_strips_event_kind_suffix(tmp_path: Path):
    db = tmp_path / "test.db"
    _run(init_db(db))
    con = sqlite3.connect(db)
    con.executemany(
        "INSERT INTO events (ts, payload, event_uid) VALUES (?, ?, ?)",
        [
            ("2026-01-01T00:00:00+00:00", "{}", "hl|1|BOTH|10|OPEN"),
            ("2026-01-01T00:00:01+00:00", "{}", "hl|2|BOTH|11|SIZE_CHANGE"),
            ("2026-01-01T00:00:02+00:00", "{}", "legacy-fill-12"),
        ],
    )
    con.commit()
    con.close()

    assert _run(load_recorded_trade_uids(db)) == {
        "hl|1|BOTH|10",
        "hl|2|BOTH|11",
        "legacy-fill-12",
    }


# ---------------------------------------------------------------------------
# Round-trip sanity check (save_closed_trade / load_closed_trades)
# ---------------------------------------------------------------------------

class TestRoundTripSanity:
    def test_save_several_load_all_newest_first(self, tmp_path: Path):
        """save_closed_trade several rows, load_closed_trades(None) returns all newest-first."""
        db = tmp_path / "test.db"
        _run(init_db(db))

        records = [
            {
                "ts": f"2026-01-0{i+1}T00:00:00+00:00",
                "source": "HL",
                "market_symbol": "BTC",
                "side": "long",
                "entry": "50000.00",
                "exit": str(50000 + i * 1000),
                "size": "0.1",
                "notional": "5000",
                "pnl": str(i * 100),
                "pct": str(i * 2.0),
                "is_win": 1 if i > 0 else 0,
                "leverage": "10.0",
                "wins": i,
                "total": i + 1,
                "card_path": None,
            }
            for i in range(4)
        ]
        for rec in records:
            _run(save_closed_trade(db, rec))

        rows = _run(load_closed_trades(db, limit=None))
        assert len(rows) == 4
        # newest-first: last inserted (i=3) is rows[0]
        assert rows[0]["pnl"] == "300"
        assert rows[1]["pnl"] == "200"
        assert rows[2]["pnl"] == "100"
        assert rows[3]["pnl"] == "0"
