"""Tests for the realization-tracking additions in db.py and pnl_card.py.

Covers:
- Migration adds the 3 new columns to a legacy closed_trades table
- Existing rows survive migration with realization_kind treated as FULL (NULL)
- save+load round-trips trade_id / fill_ids (JSON) / realization_kind
- load_recorded_fill_ids returns the union of trade_id + parsed fill_ids, skips malformed
- calculate_pnl respects pnl_override (highest precedence)
- generate_pnl_card(is_partial=True, pnl_override=...) renders without error
  and produces bytes different from the full-close render
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from src.db import init_db, load_closed_trades, load_recorded_fill_ids, save_closed_trade
from src.pnl_card import calculate_pnl, generate_pnl_card
from src.types import Event, EventKind
from tests.conftest import make_position, make_trade


# ---------------------------------------------------------------------------
# Helpers
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
        "size": "0.1",
        "notional": "5000",
        "pnl": "500.00",
        "pct": "10.00",
        "is_win": 1,
        "leverage": "10.0",
        "wins": 1,
        "total": 1,
        "card_path": None,
    }
    base.update(overrides)
    return base


def _make_close_event(
    *,
    side: str = "long",
    entry_price: str = "100",
    exit_price: str = "110",
    size: str = "10",
    realized_pnl: str | None = "100",
) -> Event:
    pos = make_position(
        market_id=1, market_symbol="BTC",
        side=side, size=size, avg_entry_price=entry_price,
    )
    trade = make_trade(
        trade_id=1, market_id=1, market_symbol="BTC",
        side="short" if side == "long" else "long",
        size=size, price=exit_price, realized_pnl=realized_pnl,
    )
    return Event(kind=EventKind.CLOSE, trade=trade, position_before=pos, position_after=None)


# ---------------------------------------------------------------------------
# Migration tests
# ---------------------------------------------------------------------------

class TestMigration:
    def _create_legacy_db(self, db_path: Path) -> None:
        """Create a closed_trades table WITHOUT the 3 new columns (legacy schema)."""
        con = sqlite3.connect(db_path)
        con.execute("""
            CREATE TABLE closed_trades (
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
        # Insert a legacy row (no new columns)
        con.execute(
            "INSERT INTO closed_trades "
            "(ts, source, market_symbol, side, entry, exit, size, notional, "
            "pnl, pct, is_win, leverage, wins, total, card_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2025-01-01T00:00:00+00:00", "HL", "ETH", "long",
             "2000", "2200", "1.0", "2000",
             "200.00", "10.00", 1, "5.0", 1, 1, None),
        )
        con.commit()
        con.close()

    def test_migration_adds_missing_columns(self, tmp_path):
        db = tmp_path / "legacy.db"
        self._create_legacy_db(db)
        # Running init_db triggers _migrate_closed_trades
        _run(init_db(db))
        con = sqlite3.connect(db)
        col_names = {row[1] for row in con.execute("PRAGMA table_info(closed_trades)")}
        con.close()
        assert "trade_id" in col_names
        assert "fill_ids" in col_names
        assert "realization_kind" in col_names

    def test_migration_is_idempotent(self, tmp_path):
        """Calling init_db twice on a migrated DB must not raise."""
        db = tmp_path / "legacy.db"
        self._create_legacy_db(db)
        _run(init_db(db))
        _run(init_db(db))  # should not fail or duplicate columns

    def test_legacy_row_survives_migration(self, tmp_path):
        db = tmp_path / "legacy.db"
        self._create_legacy_db(db)
        _run(init_db(db))
        rows = _run(load_closed_trades(db))
        assert len(rows) == 1
        row = rows[0]
        assert row["market_symbol"] == "ETH"
        assert row["pnl"] == "200.00"
        # New columns are NULL for legacy rows
        assert row["trade_id"] is None
        assert row["fill_ids"] is None
        assert row["realization_kind"] is None

    def test_null_realization_kind_treated_as_full(self, tmp_path):
        """NULL realization_kind on legacy row should be treated as 'FULL' by convention."""
        db = tmp_path / "legacy.db"
        self._create_legacy_db(db)
        _run(init_db(db))
        rows = _run(load_closed_trades(db))
        row = rows[0]
        # NULL coalesced to "FULL" at the application layer
        kind = row["realization_kind"] or "FULL"
        assert kind == "FULL"


# ---------------------------------------------------------------------------
# Round-trip: new columns
# ---------------------------------------------------------------------------

class TestNewColumnsRoundTrip:
    def test_trade_id_round_trips(self, tmp_path):
        db = tmp_path / "t.db"
        _run(init_db(db))
        rec = _sample_record(trade_id=42)
        _run(save_closed_trade(db, rec))
        rows = _run(load_closed_trades(db))
        assert rows[0]["trade_id"] == 42

    def test_fill_ids_json_round_trips(self, tmp_path):
        db = tmp_path / "t.db"
        _run(init_db(db))
        fill_ids_json = json.dumps([101, 102, 103])
        rec = _sample_record(fill_ids=fill_ids_json)
        _run(save_closed_trade(db, rec))
        rows = _run(load_closed_trades(db))
        stored = json.loads(rows[0]["fill_ids"])
        assert stored == [101, 102, 103]

    def test_realization_kind_partial(self, tmp_path):
        db = tmp_path / "t.db"
        _run(init_db(db))
        rec = _sample_record(realization_kind="PARTIAL")
        _run(save_closed_trade(db, rec))
        rows = _run(load_closed_trades(db))
        assert rows[0]["realization_kind"] == "PARTIAL"

    def test_realization_kind_full(self, tmp_path):
        db = tmp_path / "t.db"
        _run(init_db(db))
        rec = _sample_record(realization_kind="FULL")
        _run(save_closed_trade(db, rec))
        rows = _run(load_closed_trades(db))
        assert rows[0]["realization_kind"] == "FULL"

    def test_new_columns_null_when_omitted(self, tmp_path):
        """Backward compat: record without the new keys → NULLs stored."""
        db = tmp_path / "t.db"
        _run(init_db(db))
        rec = _sample_record()  # no trade_id / fill_ids / realization_kind
        _run(save_closed_trade(db, rec))
        rows = _run(load_closed_trades(db))
        assert rows[0]["trade_id"] is None
        assert rows[0]["fill_ids"] is None
        assert rows[0]["realization_kind"] is None

    def test_all_three_columns_together(self, tmp_path):
        db = tmp_path / "t.db"
        _run(init_db(db))
        fill_ids_json = json.dumps([200, 201])
        rec = _sample_record(trade_id=200, fill_ids=fill_ids_json, realization_kind="PARTIAL")
        _run(save_closed_trade(db, rec))
        rows = _run(load_closed_trades(db))
        row = rows[0]
        assert row["trade_id"] == 200
        assert json.loads(row["fill_ids"]) == [200, 201]
        assert row["realization_kind"] == "PARTIAL"


# ---------------------------------------------------------------------------
# load_recorded_fill_ids
# ---------------------------------------------------------------------------

class TestLoadRecordedFillIds:
    def test_empty_table_returns_empty_set(self, tmp_path):
        db = tmp_path / "t.db"
        _run(init_db(db))
        ids = _run(load_recorded_fill_ids(db))
        assert ids == set()

    def test_trade_id_included(self, tmp_path):
        db = tmp_path / "t.db"
        _run(init_db(db))
        _run(save_closed_trade(db, _sample_record(trade_id=77)))
        ids = _run(load_recorded_fill_ids(db))
        assert 77 in ids

    def test_fill_ids_parsed_and_included(self, tmp_path):
        db = tmp_path / "t.db"
        _run(init_db(db))
        _run(save_closed_trade(db, _sample_record(fill_ids=json.dumps([10, 20, 30]))))
        ids = _run(load_recorded_fill_ids(db))
        assert {10, 20, 30}.issubset(ids)

    def test_union_of_trade_id_and_fill_ids(self, tmp_path):
        db = tmp_path / "t.db"
        _run(init_db(db))
        # Row 1: trade_id=5, fill_ids=[5, 6, 7]
        _run(save_closed_trade(db, _sample_record(
            trade_id=5, fill_ids=json.dumps([5, 6, 7])
        )))
        # Row 2: trade_id=99, fill_ids=[99, 100]
        _run(save_closed_trade(db, _sample_record(
            ts="2026-01-02T00:00:00+00:00",
            trade_id=99, fill_ids=json.dumps([99, 100])
        )))
        ids = _run(load_recorded_fill_ids(db))
        assert ids == {5, 6, 7, 99, 100}

    def test_malformed_fill_ids_skipped(self, tmp_path):
        db = tmp_path / "t.db"
        _run(init_db(db))
        # Good row
        _run(save_closed_trade(db, _sample_record(trade_id=42, fill_ids=json.dumps([42]))))
        # Row with malformed fill_ids JSON — inject directly via sqlite
        con = sqlite3.connect(db)
        con.execute(
            "INSERT INTO closed_trades (ts, trade_id, fill_ids) VALUES (?, ?, ?)",
            ("2026-01-02T00:00:00+00:00", 99, "NOT VALID JSON {{{{")
        )
        con.commit()
        con.close()
        # Should not raise; malformed row skipped, good row still included
        ids = _run(load_recorded_fill_ids(db))
        assert 42 in ids
        assert 99 in ids   # trade_id=99 still captured even if fill_ids invalid

    def test_null_trade_id_and_fill_ids_skipped(self, tmp_path):
        """Legacy rows with no new columns — should return empty set (no crash)."""
        db = tmp_path / "t.db"
        _run(init_db(db))
        rec = _sample_record()  # trade_id=None, fill_ids=None
        _run(save_closed_trade(db, rec))
        ids = _run(load_recorded_fill_ids(db))
        assert ids == set()

    def test_multiple_rows_across_sessions(self, tmp_path):
        db = tmp_path / "t.db"
        _run(init_db(db))
        for i in range(5):
            _run(save_closed_trade(db, _sample_record(
                ts=f"2026-01-0{i+1}T00:00:00+00:00",
                trade_id=i * 10,
                fill_ids=json.dumps([i * 10, i * 10 + 1]),
            )))
        ids = _run(load_recorded_fill_ids(db))
        # trade_ids: 0,10,20,30,40 + adjacent fill ids
        for i in range(5):
            assert i * 10 in ids
            assert i * 10 + 1 in ids


# ---------------------------------------------------------------------------
# pnl_override in calculate_pnl
# ---------------------------------------------------------------------------

class TestPnlOverride:
    def test_override_takes_precedence_over_realized_pnl(self):
        ev = _make_close_event(realized_pnl="100")
        result = calculate_pnl(ev, pnl_override=Decimal("429.97"))
        assert result == Decimal("429.97")

    def test_override_takes_precedence_over_fallback(self):
        ev = _make_close_event(realized_pnl=None)  # Lighter path
        result = calculate_pnl(ev, pnl_override=Decimal("50.00"))
        assert result == Decimal("50.00")

    def test_override_zero_is_valid(self):
        ev = _make_close_event(realized_pnl="999")
        result = calculate_pnl(ev, pnl_override=Decimal("0"))
        assert result == Decimal("0")

    def test_override_negative(self):
        ev = _make_close_event(realized_pnl="999")
        result = calculate_pnl(ev, pnl_override=Decimal("-150.50"))
        assert result == Decimal("-150.50")

    def test_override_none_falls_through_to_realized_pnl(self):
        ev = _make_close_event(realized_pnl="100")
        result = calculate_pnl(ev, pnl_override=None)
        assert result == Decimal("100")

    def test_override_none_falls_through_to_fallback(self):
        ev = _make_close_event(
            side="long", entry_price="100", exit_price="120",
            size="10", realized_pnl=None,
        )
        result = calculate_pnl(ev, pnl_override=None)
        assert result == Decimal("200")  # (120-100)*10

    def test_override_with_accumulated_pnl_ignores_accumulated(self):
        """pnl_override is returned verbatim; accumulated_pnl is ignored."""
        ev = _make_close_event(realized_pnl="50")
        result = calculate_pnl(
            ev,
            accumulated_pnl=Decimal("999"),
            pnl_override=Decimal("429.97"),
        )
        assert result == Decimal("429.97")


# ---------------------------------------------------------------------------
# generate_pnl_card with is_partial and pnl_override (smoke tests)
# ---------------------------------------------------------------------------

class TestGeneratePnlCardPartial:
    def _make_event(self) -> Event:
        return _make_close_event(
            side="long",
            entry_price="50000",
            exit_price="55000",
            size="0.1",
            realized_pnl="500",
        )

    def test_partial_card_renders_without_error(self):
        try:
            from PIL import Image  # noqa: F401
        except ImportError:
            pytest.skip("Pillow not installed")

        ev = self._make_event()
        result = generate_pnl_card(
            ev,
            source_name="HL",
            wins=3,
            total=5,
            is_partial=True,
            pnl_override=Decimal("429.97"),
        )
        assert result is not None
        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_partial_card_differs_from_full_close_card(self):
        try:
            from PIL import Image  # noqa: F401
        except ImportError:
            pytest.skip("Pillow not installed")

        ev = self._make_event()
        full_bytes = generate_pnl_card(
            ev, source_name="HL", wins=3, total=5,
            pnl_override=Decimal("429.97"), is_partial=False,
        )
        partial_bytes = generate_pnl_card(
            ev, source_name="HL", wins=3, total=5,
            pnl_override=Decimal("429.97"), is_partial=True,
        )
        assert full_bytes is not None
        assert partial_bytes is not None
        assert full_bytes != partial_bytes, (
            "Partial close card should differ from full close card "
            "(different header text)"
        )

    def test_pnl_override_used_in_card(self):
        """Smoke: card renders when pnl_override is given with no accumulated_pnl."""
        try:
            from PIL import Image  # noqa: F401
        except ImportError:
            pytest.skip("Pillow not installed")

        ev = self._make_event()
        result = generate_pnl_card(
            ev, source_name="HL", wins=1, total=2,
            pnl_override=Decimal("-75.25"),
        )
        assert result is not None
        assert len(result) > 100  # valid PNG bytes
