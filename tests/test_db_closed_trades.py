"""Round-trip tests for the closed_trades table in db.py."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.db import init_db, load_closed_trades, save_closed_trade


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def _sample_record(**overrides) -> dict:
    base = {
        "ts": "2026-01-01T00:00:00+00:00",
        "source": "HL",
        "market_symbol": "BTC",
        "side": "long",
        "entry": "50000.00",
        "exit": "55000.00",
        "size": "0.1000",
        "notional": "5000",
        "pnl": "500.00",
        "pct": "10.00",
        "is_win": 1,
        "leverage": "10.0",
        "wins": 3,
        "total": 5,
        "card_path": "/cards/some_card.png",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Round-trip: save then load
# ---------------------------------------------------------------------------

class TestSaveAndLoad:
    def test_round_trip_basic(self, tmp_path):
        db = tmp_path / "test.db"
        _run(init_db(db))
        rec = _sample_record()
        _run(save_closed_trade(db, rec))
        rows = _run(load_closed_trades(db))
        assert len(rows) == 1
        row = rows[0]
        assert row["ts"] == rec["ts"]
        assert row["source"] == "HL"
        assert row["market_symbol"] == "BTC"
        assert row["side"] == "long"
        assert row["entry"] == "50000.00"
        assert row["exit"] == "55000.00"
        assert row["pnl"] == "500.00"
        assert row["pct"] == "10.00"
        assert row["is_win"] == 1
        assert row["wins"] == 3
        assert row["total"] == 5
        assert row["card_path"] == "/cards/some_card.png"

    def test_none_values_survive(self, tmp_path):
        db = tmp_path / "test.db"
        _run(init_db(db))
        rec = _sample_record(pnl=None, pct=None, card_path=None, leverage=None)
        _run(save_closed_trade(db, rec))
        rows = _run(load_closed_trades(db))
        assert rows[0]["pnl"] is None
        assert rows[0]["pct"] is None
        assert rows[0]["card_path"] is None
        assert rows[0]["leverage"] is None

    def test_string_values_preserved(self, tmp_path):
        db = tmp_path / "test.db"
        _run(init_db(db))
        rec = _sample_record(pnl="123.456789", entry="0.001234")
        _run(save_closed_trade(db, rec))
        rows = _run(load_closed_trades(db))
        assert rows[0]["pnl"] == "123.456789"
        assert rows[0]["entry"] == "0.001234"

    def test_is_win_zero_for_loss(self, tmp_path):
        db = tmp_path / "test.db"
        _run(init_db(db))
        rec = _sample_record(is_win=0, pnl="-200.00")
        _run(save_closed_trade(db, rec))
        rows = _run(load_closed_trades(db))
        assert rows[0]["is_win"] == 0


# ---------------------------------------------------------------------------
# Ordering: newest-first
# ---------------------------------------------------------------------------

class TestNewestFirst:
    def test_newest_first_ordering(self, tmp_path):
        db = tmp_path / "test.db"
        _run(init_db(db))
        for i in range(3):
            rec = _sample_record(ts=f"2026-01-0{i+1}T00:00:00+00:00", pnl=str(i * 100))
            _run(save_closed_trade(db, rec))
        rows = _run(load_closed_trades(db))
        # id is autoincrement so highest id (last inserted) comes first
        assert rows[0]["pnl"] == "200"
        assert rows[1]["pnl"] == "100"
        assert rows[2]["pnl"] == "0"

    def test_multiple_records_correct_count(self, tmp_path):
        db = tmp_path / "test.db"
        _run(init_db(db))
        for i in range(10):
            _run(save_closed_trade(db, _sample_record(pnl=str(i))))
        rows = _run(load_closed_trades(db))
        assert len(rows) == 10


# ---------------------------------------------------------------------------
# limit behaviour
# ---------------------------------------------------------------------------

class TestLimit:
    def test_limit_none_returns_all(self, tmp_path):
        db = tmp_path / "test.db"
        _run(init_db(db))
        for i in range(7):
            _run(save_closed_trade(db, _sample_record(pnl=str(i))))
        rows = _run(load_closed_trades(db, limit=None))
        assert len(rows) == 7

    def test_limit_returns_at_most_n(self, tmp_path):
        db = tmp_path / "test.db"
        _run(init_db(db))
        for i in range(10):
            _run(save_closed_trade(db, _sample_record(pnl=str(i))))
        rows = _run(load_closed_trades(db, limit=3))
        assert len(rows) == 3

    def test_limit_returns_newest(self, tmp_path):
        db = tmp_path / "test.db"
        _run(init_db(db))
        for i in range(5):
            _run(save_closed_trade(db, _sample_record(pnl=str(i * 10))))
        rows = _run(load_closed_trades(db, limit=2))
        # Newest (highest id) first
        assert rows[0]["pnl"] == "40"
        assert rows[1]["pnl"] == "30"

    def test_limit_larger_than_count(self, tmp_path):
        db = tmp_path / "test.db"
        _run(init_db(db))
        _run(save_closed_trade(db, _sample_record()))
        rows = _run(load_closed_trades(db, limit=100))
        assert len(rows) == 1

    def test_empty_table(self, tmp_path):
        db = tmp_path / "test.db"
        _run(init_db(db))
        rows = _run(load_closed_trades(db))
        assert rows == []

    def test_empty_table_with_limit(self, tmp_path):
        db = tmp_path / "test.db"
        _run(init_db(db))
        rows = _run(load_closed_trades(db, limit=10))
        assert rows == []
