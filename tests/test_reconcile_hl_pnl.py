"""Unit tests for scripts/hl_pnl_logic.py — the pure reconstruction logic.

Tests cover:
- Entry back-out for long and short fills
- pct sign correctness
- is_win flag
- size==0 guard (ValueError)
- realized_pnl=None guard (ValueError)
- running wins/total counter accumulation
- realization_kind tagging via RealizationKindTracker + finalize()
- reconstruct_all() end-to-end (multi-fill, multi-market)
- DB helper: delete_closed_trades_by_source only deletes the target source
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from scripts.hl_pnl_logic import (
    RealizationKindTracker,
    reconstruct_all,
    reconstruct_record,
)
from src.types import Trade
from scripts.reconcile_hl_pnl import _print_report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_fill(
    *,
    trade_id: int = 1,
    market_symbol: str = "BTC",
    side: str = "long",
    size: str = "1",
    price: str = "50000",
    realized_pnl: str | None = "500",
    source: str = "HL Test",
    timestamp: datetime | None = None,
    start_position: str | None = None,
) -> Trade:
    return Trade(
        trade_id=trade_id,
        timestamp=timestamp or _T0,
        market_id=0,
        market_symbol=market_symbol,
        side=side,
        size=Decimal(size),
        price=Decimal(price),
        source=source,
        realized_pnl=Decimal(realized_pnl) if realized_pnl is not None else None,
        start_position=Decimal(start_position) if start_position is not None else None,
    )


def _run(coro):
    return asyncio.run(coro)


def test_reconcile_report_compares_only_the_replaced_window(capsys):
    existing_rows = [
        {"ts": "2026-01-01T00:00:00+00:00", "pnl": "100"},
        {"ts": "2026-01-02T00:00:00+00:00", "pnl": "25"},
    ]
    existing_window_rows = [existing_rows[1]]
    rebuilt = [{"pnl": "40", "market_symbol": "BTC"}]

    _print_report(
        existing_rows,
        existing_window_rows,
        rebuilt,
        "HL",
        dry_run=True,
        t0_iso="2026-01-02T00:00:00+00:00",
        now_iso="2026-01-03T00:00:00+00:00",
        rows_replaced=1,
        rows_preserved=1,
    )
    output = capsys.readouterr().out
    assert "Existing net PnL (all rows) : $+125.00" in output
    assert "Existing net PnL (window)   : $+25.00" in output
    assert "Rebuilt net PnL (window)    : $+40.00" in output
    assert "Window delta                : $+15.00  (+0 rows)" in output
    assert "(-1 rows)" not in output


# ---------------------------------------------------------------------------
# Entry back-out: long
# ---------------------------------------------------------------------------

class TestEntryBackoutLong:
    def test_basic_long_win(self):
        # exit=50000, realized=+500, size=1
        # entry = 50000 - 500/1 = 49500
        fill = _make_fill(side="long", price="50000", realized_pnl="500", size="1")
        rec = reconstruct_record(fill, "FULL", 0, 0)
        assert Decimal(rec["entry"]) == Decimal("49500")
        assert Decimal(rec["exit"]) == Decimal("50000")

    def test_long_win_fractional_size(self):
        # exit=60000, realized=+300, size=0.1
        # entry = 60000 - 300/0.1 = 60000 - 3000 = 57000
        fill = _make_fill(side="long", price="60000", realized_pnl="300", size="0.1")
        rec = reconstruct_record(fill, "FULL", 0, 0)
        assert Decimal(rec["entry"]) == Decimal("57000")

    def test_long_loss(self):
        # exit=48000, realized=-200, size=1
        # entry = 48000 - (-200)/1 = 48000 + 200 = 48200
        fill = _make_fill(side="long", price="48000", realized_pnl="-200", size="1")
        rec = reconstruct_record(fill, "FULL", 0, 0)
        assert Decimal(rec["entry"]) == Decimal("48200")
        assert rec["is_win"] == 0

    def test_long_pct_positive_on_win(self):
        # exit=50000, entry=49500 -> pct = (50000-49500)/49500*100
        fill = _make_fill(side="long", price="50000", realized_pnl="500", size="1")
        rec = reconstruct_record(fill, "FULL", 0, 0)
        expected_pct = (Decimal("50000") - Decimal("49500")) / Decimal("49500") * Decimal("100")
        assert abs(Decimal(rec["pct"]) - expected_pct) < Decimal("1e-10")

    def test_long_pct_negative_on_loss(self):
        # exit=48000, entry=48200 -> pct should be negative
        fill = _make_fill(side="long", price="48000", realized_pnl="-200", size="1")
        rec = reconstruct_record(fill, "FULL", 0, 0)
        assert Decimal(rec["pct"]) < 0

    def test_long_pct_equivalent_formula(self):
        # pct = realized / (entry * size) * 100  (equivalent to (exit-entry)/entry*100)
        fill = _make_fill(side="long", price="50000", realized_pnl="500", size="1")
        rec = reconstruct_record(fill, "FULL", 0, 0)
        entry = Decimal(rec["entry"])
        realized = Decimal(rec["pnl"])
        size = Decimal(rec["size"])
        expected = realized / (entry * size) * Decimal("100")
        assert abs(Decimal(rec["pct"]) - expected) < Decimal("1e-10")


# ---------------------------------------------------------------------------
# Entry back-out: short
# ---------------------------------------------------------------------------

class TestEntryBackoutShort:
    def test_basic_short_win(self):
        # short win: entry > exit
        # exit=48000, realized=+500, size=1
        # entry = 48000 + 500/1 = 48500
        fill = _make_fill(side="short", price="48000", realized_pnl="500", size="1")
        rec = reconstruct_record(fill, "FULL", 0, 0)
        assert Decimal(rec["entry"]) == Decimal("48500")
        assert Decimal(rec["exit"]) == Decimal("48000")
        assert rec["is_win"] == 1

    def test_short_loss(self):
        # exit=52000, realized=-200, size=1
        # entry = 52000 + (-200)/1 = 51800
        fill = _make_fill(side="short", price="52000", realized_pnl="-200", size="1")
        rec = reconstruct_record(fill, "FULL", 0, 0)
        assert Decimal(rec["entry"]) == Decimal("51800")
        assert rec["is_win"] == 0

    def test_short_pct_positive_on_win(self):
        # exit=48000, entry=48500 -> pct = (48500-48000)/48500*100 > 0
        fill = _make_fill(side="short", price="48000", realized_pnl="500", size="1")
        rec = reconstruct_record(fill, "FULL", 0, 0)
        assert Decimal(rec["pct"]) > 0

    def test_short_pct_negative_on_loss(self):
        fill = _make_fill(side="short", price="52000", realized_pnl="-200", size="1")
        rec = reconstruct_record(fill, "FULL", 0, 0)
        assert Decimal(rec["pct"]) < 0

    def test_short_fractional_size(self):
        # exit=1000, realized=+50, size=0.5
        # entry = 1000 + 50/0.5 = 1000 + 100 = 1100
        fill = _make_fill(
            market_symbol="ETH", side="short", price="1000",
            realized_pnl="50", size="0.5"
        )
        rec = reconstruct_record(fill, "FULL", 0, 0)
        assert Decimal(rec["entry"]) == Decimal("1100")


# ---------------------------------------------------------------------------
# is_win flag
# ---------------------------------------------------------------------------

class TestIsWin:
    def test_positive_pnl_is_win(self):
        fill = _make_fill(realized_pnl="100")
        rec = reconstruct_record(fill, "FULL", 0, 0)
        assert rec["is_win"] == 1

    def test_negative_pnl_is_loss(self):
        fill = _make_fill(realized_pnl="-50")
        rec = reconstruct_record(fill, "FULL", 0, 0)
        assert rec["is_win"] == 0

    def test_zero_pnl_is_loss(self):
        # realized=0 means breakeven; treat as not-win
        fill = _make_fill(realized_pnl="0")
        rec = reconstruct_record(fill, "FULL", 0, 0)
        assert rec["is_win"] == 0


# ---------------------------------------------------------------------------
# Size == 0 guard
# ---------------------------------------------------------------------------

class TestSizeZeroGuard:
    def test_size_zero_raises(self):
        fill = _make_fill(size="0", realized_pnl="500")
        with pytest.raises(ValueError, match="size=0"):
            reconstruct_record(fill, "FULL", 0, 0)

    def test_size_zero_skipped_in_reconstruct_all(self):
        fills = [
            _make_fill(trade_id=1, size="0", realized_pnl="100"),
            _make_fill(trade_id=2, size="1", realized_pnl="200"),
        ]
        records = reconstruct_all(fills)
        # Zero-size fill must be skipped
        assert len(records) == 1
        assert records[0]["trade_id"] == 2


# ---------------------------------------------------------------------------
# realized_pnl = None guard
# ---------------------------------------------------------------------------

class TestNoPnlGuard:
    def test_none_pnl_raises(self):
        fill = _make_fill(realized_pnl=None)
        with pytest.raises(ValueError, match="realized_pnl=None"):
            reconstruct_record(fill, "FULL", 0, 0)

    def test_none_pnl_skipped_in_reconstruct_all(self):
        fills = [
            _make_fill(trade_id=1, realized_pnl=None),
            _make_fill(trade_id=2, realized_pnl="300"),
        ]
        records = reconstruct_all(fills)
        assert len(records) == 1
        assert records[0]["trade_id"] == 2


# ---------------------------------------------------------------------------
# Running wins / total counter
# ---------------------------------------------------------------------------

class TestRunningCounters:
    # wins/total count CLOSED TRADES (one per FULL close, judged on the
    # round-trip total) — not fills. reconstruct_record stores the counters
    # verbatim; reconstruct_all computes them.

    def test_reconstruct_record_stores_counters_verbatim(self):
        fill = _make_fill(realized_pnl="100")
        rec = reconstruct_record(fill, "FULL", 3, 7)
        assert rec["wins"] == 3
        assert rec["total"] == 7

    def test_single_trade_win_and_loss(self):
        win = reconstruct_all([_make_fill(realized_pnl="100", size="1", start_position="1")])
        assert win[0]["wins"] == 1 and win[0]["total"] == 1
        loss = reconstruct_all([_make_fill(realized_pnl="-100", size="1", start_position="1")])
        assert loss[0]["wins"] == 0 and loss[0]["total"] == 1

    def test_accumulation_across_trades(self):
        fills = [
            _make_fill(trade_id=1, realized_pnl="100", size="1", start_position="1"),   # win
            _make_fill(trade_id=2, realized_pnl="-50", size="1", start_position="1"),   # loss
            _make_fill(trade_id=3, realized_pnl="200", size="1", start_position="1"),   # win
        ]
        records = reconstruct_all(fills)
        assert records[0]["wins"] == 1 and records[0]["total"] == 1
        assert records[1]["wins"] == 1 and records[1]["total"] == 2
        assert records[2]["wins"] == 2 and records[2]["total"] == 3

    def test_partials_do_not_advance_counter(self):
        # 3 fills, ONE trade: scale-out +100, scale-out +50, close -5.
        # The trade total is +145 → ONE win, even though the close fill is red.
        fills = [
            _make_fill(trade_id=1, realized_pnl="100", size="2", start_position="10"),
            _make_fill(trade_id=2, realized_pnl="50", size="3", start_position="8"),
            _make_fill(trade_id=3, realized_pnl="-5", size="5", start_position="5"),
        ]
        records = reconstruct_all(fills)
        assert [(r["wins"], r["total"]) for r in records] == [(0, 0), (0, 0), (1, 1)]

    def test_all_losses(self):
        fills = [
            _make_fill(trade_id=i, realized_pnl="-10", size="1", start_position="1")
            for i in range(1, 4)
        ]
        records = reconstruct_all(fills)
        assert records[-1]["wins"] == 0
        assert records[-1]["total"] == 3

    def test_all_wins(self):
        fills = [
            _make_fill(trade_id=i, realized_pnl="10", size="1", start_position="1")
            for i in range(1, 6)
        ]
        records = reconstruct_all(fills)
        assert records[-1]["wins"] == 5
        assert records[-1]["total"] == 5


# ---------------------------------------------------------------------------
# Realization kind (RealizationKindTracker + finalize)
# ---------------------------------------------------------------------------

class TestRealizationKind:
    def test_single_fill_per_market_is_full(self):
        fills = [_make_fill(trade_id=1, market_symbol="BTC", realized_pnl="100")]
        records = reconstruct_all(fills)
        assert records[0]["realization_kind"] == "FULL"

    def test_two_fills_same_market_last_is_full(self):
        fills = [
            _make_fill(trade_id=1, market_symbol="BTC", realized_pnl="100"),
            _make_fill(trade_id=2, market_symbol="BTC", realized_pnl="200"),
        ]
        records = reconstruct_all(fills)
        # After finalize(): last fill for BTC should be FULL
        assert records[1]["realization_kind"] == "FULL"
        assert records[0]["realization_kind"] == "PARTIAL"

    def test_three_fills_same_market(self):
        fills = [
            _make_fill(trade_id=i, market_symbol="ETH", realized_pnl="50")
            for i in range(1, 4)
        ]
        records = reconstruct_all(fills)
        assert records[0]["realization_kind"] == "PARTIAL"
        assert records[1]["realization_kind"] == "PARTIAL"
        assert records[2]["realization_kind"] == "FULL"

    def test_different_markets_each_full(self):
        fills = [
            _make_fill(trade_id=1, market_symbol="BTC", realized_pnl="100"),
            _make_fill(trade_id=2, market_symbol="ETH", realized_pnl="50"),
        ]
        records = reconstruct_all(fills)
        assert records[0]["realization_kind"] == "FULL"
        assert records[1]["realization_kind"] == "FULL"

    def test_start_position_exact_tagging_multi_round_trip(self):
        # SOL traded 3 times in the window — each close must be its own FULL
        # (the legacy heuristic collapsed this to ONE FULL per window, hiding
        # finished trades inside an "in progress" blob).
        fills = [
            # trip 1: scale-out 2 of 5, then close remaining 3
            _make_fill(trade_id=1, market_symbol="SOL", size="2", start_position="5", realized_pnl="100"),
            _make_fill(trade_id=2, market_symbol="SOL", size="3", start_position="3", realized_pnl="297.71"),
            # trip 2: single-fill full close of a short (negative startPosition)
            _make_fill(trade_id=3, market_symbol="SOL", size="4", start_position="-4", realized_pnl="-105.12"),
            # trip 3: scale-out then close
            _make_fill(trade_id=4, market_symbol="SOL", size="1", start_position="6", realized_pnl="50"),
            _make_fill(trade_id=5, market_symbol="SOL", size="5", start_position="5", realized_pnl="502.55"),
        ]
        records = reconstruct_all(fills)
        kinds = [r["realization_kind"] for r in records]
        assert kinds == ["PARTIAL", "FULL", "FULL", "PARTIAL", "FULL"]

    def test_start_position_flip_counts_as_full(self):
        # A flip ("Long > Short") closes the whole position: size > |startPosition|.
        fills = [_make_fill(trade_id=1, market_symbol="ETH", size="7", start_position="3", realized_pnl="10")]
        records = reconstruct_all(fills)
        assert records[0]["realization_kind"] == "FULL"

    def test_start_position_markets_skip_finalize_but_fallback_markets_get_it(self):
        # BTC fills carry start_position (exact tags: two FULL closes) while
        # DOGE fills don't (heuristic + finalize → last DOGE fill FULL).
        fills = [
            _make_fill(trade_id=1, market_symbol="BTC", size="1", start_position="1", realized_pnl="5"),
            _make_fill(trade_id=2, market_symbol="DOGE", size="1", realized_pnl="1"),
            _make_fill(trade_id=3, market_symbol="BTC", size="2", start_position="2", realized_pnl="7"),
            _make_fill(trade_id=4, market_symbol="DOGE", size="1", realized_pnl="2"),
        ]
        records = reconstruct_all(fills)
        by_id = {r["trade_id"]: r["realization_kind"] for r in records}
        assert by_id[1] == "FULL" and by_id[3] == "FULL"   # exact, untouched by finalize
        assert by_id[2] == "PARTIAL" and by_id[4] == "FULL"  # heuristic + finalize

    def test_same_ts_burst_sorted_by_start_position_descending(self):
        # Same-millisecond burst close: sorting by (ts, -|startPosition|)
        # restores the physical order — the flattening fill (sp == size) last.
        fills = [
            _make_fill(trade_id=1, market_symbol="MEGA", size="500", start_position="500", realized_pnl="-26.50"),
            _make_fill(trade_id=2, market_symbol="MEGA", size="1500", start_position="3500", realized_pnl="-14.39"),
            _make_fill(trade_id=3, market_symbol="MEGA", size="1500", start_position="2000", realized_pnl="-34.76"),
        ]
        fills.sort(key=lambda f: (
            f.timestamp,
            -abs(f.start_position) if f.start_position is not None else Decimal(0),
        ))
        assert [f.trade_id for f in fills] == [2, 3, 1]
        records = reconstruct_all(fills)
        kinds = [r["realization_kind"] for r in records]
        assert kinds == ["PARTIAL", "PARTIAL", "FULL"]

    def test_finalize_static_method(self):
        records = [
            {"market_symbol": "BTC", "realization_kind": "FULL"},
            {"market_symbol": "BTC", "realization_kind": "PARTIAL"},
            {"market_symbol": "ETH", "realization_kind": "FULL"},
        ]
        result = RealizationKindTracker.finalize(records)
        # last BTC (idx 1) → FULL; first BTC (idx 0) → PARTIAL; ETH (idx 2) → FULL
        assert result[0]["realization_kind"] == "PARTIAL"
        assert result[1]["realization_kind"] == "FULL"
        assert result[2]["realization_kind"] == "FULL"


# ---------------------------------------------------------------------------
# Record fields completeness
# ---------------------------------------------------------------------------

class TestRecordFields:
    def test_all_required_fields_present(self):
        from src.db import _CLOSED_TRADE_COLUMNS
        fill = _make_fill()
        rec = reconstruct_record(fill, "FULL", 0, 0)
        for col in _CLOSED_TRADE_COLUMNS:
            assert col in rec, f"Missing column: {col}"

    def test_fill_ids_is_json_list_with_trade_id(self):
        fill = _make_fill(trade_id=42)
        rec = reconstruct_record(fill, "FULL", 0, 0)
        ids = json.loads(rec["fill_ids"])
        assert isinstance(ids, list)
        assert 42 in ids

    def test_trade_id_stored(self):
        fill = _make_fill(trade_id=999)
        rec = reconstruct_record(fill, "FULL", 0, 0)
        assert rec["trade_id"] == 999

    def test_card_path_is_none(self):
        fill = _make_fill()
        rec = reconstruct_record(fill, "FULL", 0, 0)
        assert rec["card_path"] is None

    def test_leverage_is_none(self):
        fill = _make_fill()
        rec = reconstruct_record(fill, "FULL", 0, 0)
        assert rec["leverage"] is None

    def test_notional_equals_size_times_entry(self):
        fill = _make_fill(side="long", price="50000", realized_pnl="500", size="2")
        rec = reconstruct_record(fill, "FULL", 0, 0)
        entry = Decimal(rec["entry"])
        size = Decimal(rec["size"])
        notional = Decimal(rec["notional"])
        assert abs(notional - size * entry) < Decimal("1e-8")

    def test_ts_iso_format(self):
        fill = _make_fill()
        rec = reconstruct_record(fill, "FULL", 0, 0)
        # Should be parseable as ISO datetime
        dt = datetime.fromisoformat(rec["ts"])
        assert dt.year == 2026

    def test_source_propagated(self):
        fill = _make_fill(source="MyHL")
        rec = reconstruct_record(fill, "FULL", 0, 0)
        assert rec["source"] == "MyHL"


# ---------------------------------------------------------------------------
# reconstruct_all: multi-fill, multi-market
# ---------------------------------------------------------------------------

class TestReconstructAll:
    def test_empty_list_returns_empty(self):
        assert reconstruct_all([]) == []

    def test_mixed_wins_losses(self):
        fills = [
            _make_fill(trade_id=1, realized_pnl="100"),
            _make_fill(trade_id=2, realized_pnl="-50"),
            _make_fill(trade_id=3, realized_pnl="200"),
        ]
        records = reconstruct_all(fills)
        assert len(records) == 3
        assert records[0]["is_win"] == 1
        assert records[1]["is_win"] == 0
        assert records[2]["is_win"] == 1

    def test_preserves_trade_id_order(self):
        fills = [
            _make_fill(trade_id=10, realized_pnl="100"),
            _make_fill(trade_id=20, realized_pnl="200"),
            _make_fill(trade_id=30, realized_pnl="300"),
        ]
        records = reconstruct_all(fills)
        tids = [r["trade_id"] for r in records]
        assert tids == [10, 20, 30]

    def test_multi_market_counters_span_markets(self):
        # wins/total should count across ALL markets, not per-market
        fills = [
            _make_fill(trade_id=1, market_symbol="BTC", realized_pnl="100", size="1", start_position="1"),
            _make_fill(trade_id=2, market_symbol="ETH", realized_pnl="-50", size="1", start_position="1"),
            _make_fill(trade_id=3, market_symbol="BTC", realized_pnl="200", size="1", start_position="1"),
        ]
        records = reconstruct_all(fills)
        assert records[2]["total"] == 3
        assert records[2]["wins"] == 2  # BTC win + ETH loss + BTC win = 2 wins


# ---------------------------------------------------------------------------
# DB helper: delete_closed_trades_by_source
# ---------------------------------------------------------------------------

class TestDeleteBySourceSince:
    """Tests for the new scoped delete helper delete_closed_trades_by_source_since."""

    def _make_rec(self, source: str, ts: str, trade_id: int) -> dict:
        return {
            "ts": ts,
            "source": source,
            "market_symbol": "BTC",
            "side": "long",
            "entry": "49500",
            "exit": "50000",
            "size": "1",
            "notional": "49500",
            "pnl": "500",
            "pct": "1.01",
            "is_win": 1,
            "leverage": None,
            "wins": 1,
            "total": 1,
            "card_path": None,
            "trade_id": trade_id,
            "fill_ids": f"[{trade_id}]",
            "realization_kind": "FULL",
        }

    def test_deletes_rows_at_or_after_t0(self, tmp_path):
        from src.db import (
            delete_closed_trades_by_source_since,
            init_db,
            load_closed_trades,
            save_closed_trade,
        )
        db = tmp_path / "test.db"
        _run(init_db(db))
        # Insert three HL rows at different timestamps
        _run(save_closed_trade(db, self._make_rec("HL", "2026-01-01T00:00:00+00:00", 1)))
        _run(save_closed_trade(db, self._make_rec("HL", "2026-03-01T00:00:00+00:00", 2)))
        _run(save_closed_trade(db, self._make_rec("HL", "2026-06-01T00:00:00+00:00", 3)))

        # Delete rows with ts >= March 2026 (T0)
        deleted = _run(
            delete_closed_trades_by_source_since(db, "HL", "2026-03-01T00:00:00+00:00")
        )
        assert deleted == 2  # March and June rows deleted

        remaining = _run(load_closed_trades(db))
        assert len(remaining) == 1
        assert remaining[0]["trade_id"] == 1  # January row preserved

    def test_preserves_other_source(self, tmp_path):
        from src.db import (
            delete_closed_trades_by_source_since,
            init_db,
            load_closed_trades,
            save_closed_trade,
        )
        db = tmp_path / "test.db"
        _run(init_db(db))
        _run(save_closed_trade(db, self._make_rec("HL", "2026-03-01T00:00:00+00:00", 1)))
        _run(save_closed_trade(db, self._make_rec("Lighter NK", "2026-03-01T00:00:00+00:00", 2)))

        deleted = _run(
            delete_closed_trades_by_source_since(db, "HL", "2026-01-01T00:00:00+00:00")
        )
        assert deleted == 1

        remaining = _run(load_closed_trades(db))
        assert len(remaining) == 1
        assert remaining[0]["source"] == "Lighter NK"

    def test_no_rows_in_window_returns_zero(self, tmp_path):
        from src.db import (
            delete_closed_trades_by_source_since,
            init_db,
            save_closed_trade,
        )
        db = tmp_path / "test.db"
        _run(init_db(db))
        _run(save_closed_trade(db, self._make_rec("HL", "2026-01-01T00:00:00+00:00", 1)))

        # T0 is AFTER the only row — nothing matches ts >= T0
        deleted = _run(
            delete_closed_trades_by_source_since(db, "HL", "2026-06-01T00:00:00+00:00")
        )
        assert deleted == 0

    def test_idempotent(self, tmp_path):
        from src.db import (
            delete_closed_trades_by_source_since,
            init_db,
            save_closed_trade,
        )
        db = tmp_path / "test.db"
        _run(init_db(db))
        _run(save_closed_trade(db, self._make_rec("HL", "2026-03-01T00:00:00+00:00", 1)))

        d1 = _run(
            delete_closed_trades_by_source_since(db, "HL", "2026-01-01T00:00:00+00:00")
        )
        d2 = _run(
            delete_closed_trades_by_source_since(db, "HL", "2026-01-01T00:00:00+00:00")
        )
        assert d1 == 1
        assert d2 == 0  # row already gone


class TestDeleteBySource:
    def test_deletes_only_target_source(self, tmp_path):
        from src.db import (
            delete_closed_trades_by_source,
            init_db,
            load_closed_trades,
            save_closed_trade,
        )

        db = tmp_path / "test.db"
        _run(init_db(db))

        hl_rec = {
            "ts": "2026-01-01T00:00:00+00:00",
            "source": "HL",
            "market_symbol": "BTC",
            "side": "long",
            "entry": "49500",
            "exit": "50000",
            "size": "1",
            "notional": "49500",
            "pnl": "500",
            "pct": "1.01",
            "is_win": 1,
            "leverage": None,
            "wins": 1,
            "total": 1,
            "card_path": None,
            "trade_id": 1,
            "fill_ids": "[1]",
            "realization_kind": "FULL",
        }
        lighter_rec = {**hl_rec, "source": "Lighter NK", "trade_id": 2, "fill_ids": "[2]"}

        _run(save_closed_trade(db, hl_rec))
        _run(save_closed_trade(db, lighter_rec))

        deleted = _run(delete_closed_trades_by_source(db, "HL"))
        assert deleted == 1

        remaining = _run(load_closed_trades(db))
        assert len(remaining) == 1
        assert remaining[0]["source"] == "Lighter NK"

    def test_delete_nonexistent_source_returns_zero(self, tmp_path):
        from src.db import delete_closed_trades_by_source, init_db

        db = tmp_path / "test.db"
        _run(init_db(db))
        deleted = _run(delete_closed_trades_by_source(db, "NoSuchSource"))
        assert deleted == 0

    def test_delete_is_idempotent(self, tmp_path):
        from src.db import (
            delete_closed_trades_by_source,
            init_db,
            save_closed_trade,
        )

        db = tmp_path / "test.db"
        _run(init_db(db))
        rec = {
            "ts": "2026-01-01T00:00:00+00:00",
            "source": "HL",
            "market_symbol": "BTC",
            "side": "long",
            "entry": "49500",
            "exit": "50000",
            "size": "1",
            "notional": "49500",
            "pnl": "500",
            "pct": "1.01",
            "is_win": 1,
            "leverage": None,
            "wins": 1,
            "total": 1,
            "card_path": None,
            "trade_id": 1,
            "fill_ids": "[1]",
            "realization_kind": "FULL",
        }
        _run(save_closed_trade(db, rec))
        d1 = _run(delete_closed_trades_by_source(db, "HL"))
        d2 = _run(delete_closed_trades_by_source(db, "HL"))
        assert d1 == 1
        assert d2 == 0  # already gone


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_very_small_size(self):
        # size=0.0001, price=50000, realized=0.5
        # long: entry = 50000 - 0.5/0.0001 = 50000 - 5000 = 45000
        fill = _make_fill(
            side="long", price="50000", realized_pnl="0.5", size="0.0001"
        )
        rec = reconstruct_record(fill, "FULL", 0, 0)
        assert Decimal(rec["entry"]) == Decimal("45000")

    def test_large_notional(self):
        # 10 BTC at 60000 entry, exit at 65000 -> realized = 50000
        fill = _make_fill(
            side="long", price="65000", realized_pnl="50000", size="10"
        )
        rec = reconstruct_record(fill, "FULL", 0, 0)
        # entry = 65000 - 50000/10 = 65000 - 5000 = 60000
        assert Decimal(rec["entry"]) == Decimal("60000")
        assert Decimal(rec["notional"]) == Decimal("600000")

    def test_pnl_stored_as_string(self):
        fill = _make_fill(realized_pnl="123.456789")
        rec = reconstruct_record(fill, "FULL", 0, 0)
        # Stored as str; value should round-trip through Decimal
        assert Decimal(rec["pnl"]) == Decimal("123.456789")
