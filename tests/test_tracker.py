"""Tests for PositionTracker — the core event-classification engine."""

from decimal import Decimal

import pytest

from src.position_tracker import PositionTracker
from src.types import EventKind
from tests.conftest import make_trade, make_position


# ---------------------------------------------------------------------------
# OPEN
# ---------------------------------------------------------------------------

class TestOpen:
    def test_first_trade_is_open(self):
        tracker = PositionTracker(source="test")
        t = make_trade(side="long", size="1", price="50000")
        events = tracker.apply(t)

        assert len(events) == 1
        assert events[0].kind == EventKind.OPEN
        assert events[0].position_before is None
        assert events[0].position_after is not None
        assert events[0].position_after.size == Decimal("1")
        assert events[0].position_after.side == "long"
        assert events[0].position_after.avg_entry_price == Decimal("50000")

    def test_short_open(self):
        tracker = PositionTracker()
        t = make_trade(side="short", size="2", price="100")
        events = tracker.apply(t)

        assert events[0].kind == EventKind.OPEN
        assert events[0].position_after.side == "short"

    def test_open_position_stored_in_tracker(self):
        tracker = PositionTracker()
        tracker.apply(make_trade(market_id=0, side="long", size="5", price="200"))
        snap = tracker.snapshot()
        assert 0 in snap
        assert snap[0].size == Decimal("5")

    def test_open_separate_markets_tracked_independently(self):
        tracker = PositionTracker()
        tracker.apply(make_trade(market_id=0, market_symbol="BTC", side="long", size="1", price="50000"))
        tracker.apply(make_trade(market_id=1, market_symbol="ETH", side="short", size="10", price="3000"))
        snap = tracker.snapshot()
        assert snap[0].side == "long"
        assert snap[1].side == "short"


# ---------------------------------------------------------------------------
# SIZE_CHANGE (same-side add)
# ---------------------------------------------------------------------------

class TestSizeChange:
    def test_same_side_add_is_size_change(self):
        tracker = PositionTracker()
        tracker.apply(make_trade(side="long", size="1", price="100"))
        t2 = make_trade(trade_id=2, side="long", size="1", price="200")
        events = tracker.apply(t2)

        assert len(events) == 1
        assert events[0].kind == EventKind.SIZE_CHANGE

    def test_weighted_avg_entry_price(self):
        tracker = PositionTracker()
        tracker.apply(make_trade(trade_id=1, side="long", size="1", price="100"))
        tracker.apply(make_trade(trade_id=2, side="long", size="3", price="200"))
        snap = tracker.snapshot()
        # (1*100 + 3*200) / 4 = 700/4 = 175
        assert snap[0].avg_entry_price == Decimal("175")

    def test_size_accumulates_correctly(self):
        tracker = PositionTracker()
        for i in range(5):
            tracker.apply(make_trade(trade_id=i, side="long", size="2", price="100"))
        snap = tracker.snapshot()
        assert snap[0].size == Decimal("10")

    def test_size_change_position_before_and_after(self):
        tracker = PositionTracker()
        tracker.apply(make_trade(trade_id=1, side="long", size="4", price="100"))
        t2 = make_trade(trade_id=2, side="long", size="2", price="100")
        events = tracker.apply(t2)
        ev = events[0]
        assert ev.position_before.size == Decimal("4")
        assert ev.position_after.size == Decimal("6")

    def test_short_size_change(self):
        tracker = PositionTracker()
        tracker.apply(make_trade(trade_id=1, side="short", size="2", price="100"))
        events = tracker.apply(make_trade(trade_id=2, side="short", size="2", price="100"))
        assert events[0].kind == EventKind.SIZE_CHANGE
        assert tracker.snapshot()[0].size == Decimal("4")

    def test_avg_price_precision(self):
        tracker = PositionTracker()
        tracker.apply(make_trade(trade_id=1, side="long", size="1", price="0.100000"))
        tracker.apply(make_trade(trade_id=2, side="long", size="1", price="0.105000"))
        snap = tracker.snapshot()
        # avg = (0.1 + 0.105) / 2 = 0.1025
        assert snap[0].avg_entry_price == Decimal("0.1025")


# ---------------------------------------------------------------------------
# REDUCE (opposite side, partial close)
# ---------------------------------------------------------------------------

class TestReduce:
    def test_opposite_side_smaller_size_is_reduce(self):
        tracker = PositionTracker()
        tracker.apply(make_trade(trade_id=1, side="long", size="5", price="100"))
        t2 = make_trade(trade_id=2, side="short", size="2", price="105")
        events = tracker.apply(t2)

        assert len(events) == 1
        assert events[0].kind == EventKind.REDUCE

    def test_reduce_updates_position_size(self):
        tracker = PositionTracker()
        tracker.apply(make_trade(trade_id=1, side="long", size="5", price="100"))
        tracker.apply(make_trade(trade_id=2, side="short", size="2", price="105"))
        snap = tracker.snapshot()
        assert snap[0].size == Decimal("3")

    def test_reduce_preserves_avg_entry(self):
        tracker = PositionTracker()
        tracker.apply(make_trade(trade_id=1, side="long", size="5", price="100"))
        tracker.apply(make_trade(trade_id=2, side="short", size="2", price="150"))
        snap = tracker.snapshot()
        assert snap[0].avg_entry_price == Decimal("100")

    def test_reduce_position_before_after(self):
        tracker = PositionTracker()
        tracker.apply(make_trade(trade_id=1, side="long", size="10", price="100"))
        events = tracker.apply(make_trade(trade_id=2, side="short", size="3", price="100"))
        ev = events[0]
        assert ev.position_before.size == Decimal("10")
        assert ev.position_after.size == Decimal("7")

    def test_reduce_by_one_unit(self):
        tracker = PositionTracker()
        tracker.apply(make_trade(trade_id=1, side="long", size="2", price="100"))
        events = tracker.apply(make_trade(trade_id=2, side="short", size="1", price="100"))
        assert events[0].kind == EventKind.REDUCE
        assert tracker.snapshot()[0].size == Decimal("1")

    def test_realized_pnl_propagated_on_reduce(self):
        tracker = PositionTracker()
        tracker.apply(make_trade(trade_id=1, side="long", size="2", price="100"))
        t2 = make_trade(trade_id=2, side="short", size="1", price="120", realized_pnl="20")
        events = tracker.apply(t2)
        assert events[0].trade.realized_pnl == Decimal("20")


# ---------------------------------------------------------------------------
# CLOSE (exact size)
# ---------------------------------------------------------------------------

class TestClose:
    def test_exact_opposite_size_is_close(self):
        tracker = PositionTracker()
        tracker.apply(make_trade(trade_id=1, side="long", size="5", price="100"))
        t2 = make_trade(trade_id=2, side="short", size="5", price="120")
        events = tracker.apply(t2)

        assert len(events) == 1
        assert events[0].kind == EventKind.CLOSE

    def test_close_removes_position_from_tracker(self):
        tracker = PositionTracker()
        tracker.apply(make_trade(trade_id=1, side="long", size="5", price="100"))
        tracker.apply(make_trade(trade_id=2, side="short", size="5", price="120"))
        assert 0 not in tracker.snapshot()

    def test_close_position_before_set_after_none(self):
        tracker = PositionTracker()
        tracker.apply(make_trade(trade_id=1, side="long", size="3", price="100"))
        events = tracker.apply(make_trade(trade_id=2, side="short", size="3", price="100"))
        ev = events[0]
        assert ev.position_before is not None
        assert ev.position_after is None

    def test_close_short_position(self):
        tracker = PositionTracker()
        tracker.apply(make_trade(trade_id=1, side="short", size="4", price="100"))
        events = tracker.apply(make_trade(trade_id=2, side="long", size="4", price="90"))
        assert events[0].kind == EventKind.CLOSE
        assert 0 not in tracker.snapshot()


# ---------------------------------------------------------------------------
# FLIP (opposite side, larger than existing)
# ---------------------------------------------------------------------------

class TestFlip:
    def test_flip_emits_close_then_open(self):
        tracker = PositionTracker()
        tracker.apply(make_trade(trade_id=1, side="long", size="2", price="100"))
        events = tracker.apply(make_trade(trade_id=2, side="short", size="5", price="120"))

        assert len(events) == 2
        assert events[0].kind == EventKind.CLOSE
        assert events[1].kind == EventKind.OPEN

    def test_flip_new_position_size_is_remainder(self):
        tracker = PositionTracker()
        tracker.apply(make_trade(trade_id=1, side="long", size="2", price="100"))
        tracker.apply(make_trade(trade_id=2, side="short", size="5", price="120"))
        snap = tracker.snapshot()
        assert snap[0].size == Decimal("3")
        assert snap[0].side == "short"

    def test_flip_new_position_entry_is_fill_price(self):
        tracker = PositionTracker()
        tracker.apply(make_trade(trade_id=1, side="long", size="2", price="100"))
        tracker.apply(make_trade(trade_id=2, side="short", size="5", price="120"))
        snap = tracker.snapshot()
        assert snap[0].avg_entry_price == Decimal("120")

    def test_flip_open_event_position_after_is_new(self):
        tracker = PositionTracker()
        tracker.apply(make_trade(trade_id=1, side="long", size="1", price="100"))
        events = tracker.apply(make_trade(trade_id=2, side="short", size="3", price="200"))
        open_ev = events[1]
        assert open_ev.position_after.size == Decimal("2")
        assert open_ev.position_after.side == "short"


# ---------------------------------------------------------------------------
# seed() and snapshot()
# ---------------------------------------------------------------------------

class TestSeedSnapshot:
    def test_seed_replaces_all_positions(self):
        tracker = PositionTracker()
        tracker.apply(make_trade(market_id=0, side="long", size="5", price="100"))
        seeded = {
            1: make_position(market_id=1, market_symbol="ETH", side="short", size="3", avg_entry_price="2000"),
        }
        tracker.seed(seeded)
        snap = tracker.snapshot()
        assert 0 not in snap  # old position gone
        assert 1 in snap

    def test_snapshot_is_a_copy(self):
        tracker = PositionTracker()
        tracker.apply(make_trade(side="long", size="1", price="100"))
        snap = tracker.snapshot()
        snap[99] = make_position(market_id=99)
        assert 99 not in tracker.snapshot()

    def test_seed_empty_clears_all(self):
        tracker = PositionTracker()
        tracker.apply(make_trade(market_id=0, side="long", size="1", price="100"))
        tracker.apply(make_trade(market_id=1, market_symbol="ETH", side="short", size="1", price="2000"))
        tracker.seed({})
        assert tracker.snapshot() == {}


# ---------------------------------------------------------------------------
# apply_many
# ---------------------------------------------------------------------------

class TestApplyMany:
    def test_apply_many_sequence(self):
        tracker = PositionTracker()
        trades = [
            make_trade(trade_id=1, side="long",  size="2", price="100"),
            make_trade(trade_id=2, side="long",  size="3", price="200"),
            make_trade(trade_id=3, side="short", size="1", price="250"),
        ]
        events = tracker.apply_many(trades)
        assert len(events) == 3
        assert events[0].kind == EventKind.OPEN
        assert events[1].kind == EventKind.SIZE_CHANGE
        assert events[2].kind == EventKind.REDUCE

    def test_apply_many_open_and_close(self):
        tracker = PositionTracker()
        trades = [
            make_trade(trade_id=1, side="long",  size="2", price="100"),
            make_trade(trade_id=2, side="short", size="2", price="150"),
        ]
        events = tracker.apply_many(trades)
        assert events[0].kind == EventKind.OPEN
        assert events[1].kind == EventKind.CLOSE
        assert tracker.snapshot() == {}


# ---------------------------------------------------------------------------
# Source label propagated
# ---------------------------------------------------------------------------

class TestSourceLabel:
    def test_source_label_on_new_position(self):
        tracker = PositionTracker(source="My NK pool")
        tracker.apply(make_trade(side="long", size="1", price="100"))
        snap = tracker.snapshot()
        assert snap[0].source == "My NK pool"
