"""Tests for the min-notional filter."""

from decimal import Decimal

import pytest

from src.filters import passes_min_notional
from src.types import Event, EventKind
from tests.conftest import make_position, make_trade

MIN = Decimal("1000")


def _open_event(notional: str) -> Event:
    # OPEN: fill notional determines filter
    size, price = "1", notional
    t = make_trade(side="long", size=size, price=price)
    pos = make_position(side="long", size=size, avg_entry_price=price)
    return Event(kind=EventKind.OPEN, trade=t, position_before=None, position_after=pos)


def _size_change_event(pos_notional: str, fill_notional: str = "100") -> Event:
    pos_size, pos_price = "10", str(Decimal(pos_notional) / 10)
    t = make_trade(side="long", size="1", price=fill_notional)
    pos_before = make_position(side="long", size=pos_size, avg_entry_price=pos_price)
    pos_after  = make_position(side="long", size=str(Decimal(pos_size) + 1), avg_entry_price=pos_price)
    return Event(kind=EventKind.SIZE_CHANGE, trade=t, position_before=pos_before, position_after=pos_after)


def _reduce_event(pos_notional: str) -> Event:
    pos_size, pos_price = "10", str(Decimal(pos_notional) / 10)
    t = make_trade(side="short", size="1", price="100")
    pos_before = make_position(side="long", size=pos_size, avg_entry_price=pos_price)
    pos_after  = make_position(side="long", size="9", avg_entry_price=pos_price)
    return Event(kind=EventKind.REDUCE, trade=t, position_before=pos_before, position_after=pos_after)


def _close_event(fill_notional: str = "500") -> Event:
    t = make_trade(side="short", size="1", price=fill_notional)
    pos_before = make_position(side="long", size="1", avg_entry_price="100")
    return Event(kind=EventKind.CLOSE, trade=t, position_before=pos_before, position_after=None)


class TestPassesMinNotional:

    # CLOSE always passes
    def test_close_always_passes_regardless_of_size(self):
        assert passes_min_notional(_close_event("1"), MIN) is True

    def test_close_tiny_notional_still_passes(self):
        assert passes_min_notional(_close_event("0.01"), MIN) is True

    # OPEN — judged by fill notional
    def test_open_above_min_passes(self):
        assert passes_min_notional(_open_event("1500"), MIN) is True

    def test_open_below_min_fails(self):
        assert passes_min_notional(_open_event("500"), MIN) is False

    def test_open_exactly_at_min_passes(self):
        assert passes_min_notional(_open_event("1000"), MIN) is True

    # SIZE_CHANGE — judged by position_before notional
    def test_size_change_large_position_passes(self):
        assert passes_min_notional(_size_change_event("5000"), MIN) is True

    def test_size_change_small_position_fails(self):
        assert passes_min_notional(_size_change_event("200"), MIN) is False

    def test_size_change_no_position_before_falls_back_to_fill(self):
        t = make_trade(side="long", size="1", price="500")
        ev = Event(kind=EventKind.SIZE_CHANGE, trade=t, position_before=None, position_after=None)
        # fill notional = 500 < 1000 → fails
        assert passes_min_notional(ev, MIN) is False

    # REDUCE — judged by position_before notional
    def test_reduce_large_position_passes(self):
        assert passes_min_notional(_reduce_event("5000"), MIN) is True

    def test_reduce_small_position_fails(self):
        assert passes_min_notional(_reduce_event("200"), MIN) is False

    def test_reduce_no_position_before_falls_back_to_fill_notional(self):
        t = make_trade(side="short", size="1", price="500")
        ev = Event(kind=EventKind.REDUCE, trade=t, position_before=None, position_after=None)
        assert passes_min_notional(ev, MIN) is False

    def test_reduce_no_position_before_large_fill_passes(self):
        t = make_trade(side="short", size="10", price="200")
        ev = Event(kind=EventKind.REDUCE, trade=t, position_before=None, position_after=None)
        assert passes_min_notional(ev, MIN) is True

    # Zero min_notional passes everything
    def test_zero_min_always_passes_open(self):
        assert passes_min_notional(_open_event("1"), Decimal("0")) is True

    def test_zero_min_always_passes_size_change(self):
        assert passes_min_notional(_size_change_event("1"), Decimal("0")) is True
