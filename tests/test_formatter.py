"""Tests for the formatter module — every message shape and edge case."""

from decimal import Decimal

import pytest

from src.formatter import (
    _fmt_price,
    _fmt_pnl,
    _fmt_size,
    _fmt_sl_tp,
    format_aggregate,
    format_event,
    format_reduce_aggregate,
    format_sl_tp_set,
)
from src.types import Event, EventKind, Position
from tests.conftest import T0, make_position, make_trade


# ---------------------------------------------------------------------------
# Primitive formatters
# ---------------------------------------------------------------------------

class TestFmtPrice:
    def test_large_price_two_decimals(self):
        assert _fmt_price(Decimal("50000")) == "$50,000.00"

    def test_mid_price_four_decimals(self):
        assert _fmt_price(Decimal("1.5")) == "$1.5000"

    def test_sub_dollar_six_decimals(self):
        assert _fmt_price(Decimal("0.102488")) == "$0.102488"

    def test_exactly_one_dollar(self):
        # >= 1 → four decimals
        assert _fmt_price(Decimal("1")) == "$1.0000"

    def test_exactly_one_thousand(self):
        # >= 1000 → two decimals
        assert _fmt_price(Decimal("1000")) == "$1,000.00"

    def test_thousands_separator(self):
        assert "," in _fmt_price(Decimal("1234567"))


class TestFmtPnl:
    def test_positive_pnl_has_plus_sign(self):
        assert _fmt_pnl(Decimal("250.5")) == "+$250.50"

    def test_negative_pnl_has_minus_sign(self):
        # _fmt_pnl uses Python's number formatting which puts minus inside the $
        # so Decimal("-100") → "$-100.00" (not "-$100.00")
        result = _fmt_pnl(Decimal("-100"))
        assert "$-100.00" == result

    def test_zero_pnl_has_plus_sign(self):
        assert _fmt_pnl(Decimal("0")) == "+$0.00"

    def test_large_pnl_has_thousands_separator(self):
        assert "," in _fmt_pnl(Decimal("1500.00"))


class TestFmtSlTp:
    def test_both_set(self):
        result = _fmt_sl_tp(Decimal("90"), Decimal("120"))
        assert "SL:" in result
        assert "TP:" in result
        assert "|" in result

    def test_only_sl(self):
        result = _fmt_sl_tp(Decimal("90"), None)
        assert "SL:" in result
        assert "TP:" not in result

    def test_only_tp(self):
        result = _fmt_sl_tp(None, Decimal("120"))
        assert "TP:" in result
        assert "SL:" not in result

    def test_neither_returns_empty_string(self):
        assert _fmt_sl_tp(None, None) == ""

    def test_starts_with_newline(self):
        result = _fmt_sl_tp(Decimal("90"), Decimal("120"))
        assert result.startswith("\n")


# ---------------------------------------------------------------------------
# format_event — OPEN
# ---------------------------------------------------------------------------

class TestFormatEventOpen:
    def _open_event(self, **kwargs):
        t = make_trade(market_symbol="BTC", side="long", size="0.1", price="50000", **kwargs)
        pos = make_position(market_symbol="BTC", side="long", size="0.1", avg_entry_price="50000")
        return Event(kind=EventKind.OPEN, trade=t, position_before=None, position_after=pos)

    def test_contains_opened(self):
        ev = self._open_event()
        msg = format_event(ev, "")
        assert "Opened" in msg

    def test_contains_long_indicator(self):
        ev = self._open_event()
        msg = format_event(ev, "")
        assert "LONG" in msg

    def test_contains_market_symbol(self):
        ev = self._open_event()
        msg = format_event(ev, "")
        assert "BTC" in msg

    def test_contains_price(self):
        ev = self._open_event()
        msg = format_event(ev, "")
        assert "50,000.00" in msg

    def test_source_name_header(self):
        ev = self._open_event()
        msg = format_event(ev, "", source_name="My NK pool")
        assert msg.startswith("📍 My NK pool")

    def test_no_source_name_no_header(self):
        ev = self._open_event()
        msg = format_event(ev, "")
        assert "📍" not in msg

    def test_pool_url_footer(self):
        ev = self._open_event()
        msg = format_event(ev, "https://example.com/pool")
        assert msg.endswith("https://example.com/pool")

    def test_no_url_no_footer_newline(self):
        ev = self._open_event()
        msg = format_event(ev, "")
        assert not msg.endswith("\n")

    def test_sl_and_tp_shown(self):
        ev = self._open_event()
        msg = format_event(ev, "", sl=Decimal("45000"), tp=Decimal("60000"))
        assert "SL:" in msg
        assert "TP:" in msg

    def test_leverage_shown(self):
        ev = self._open_event()
        ev.leverage = 10.0
        msg = format_event(ev, "")
        assert "10x" in msg

    def test_leverage_none_not_shown(self):
        ev = self._open_event()
        ev.leverage = None
        msg = format_event(ev, "")
        assert "x" not in msg

    def test_short_open(self):
        t = make_trade(market_symbol="ETH", side="short", size="1", price="3000")
        pos = make_position(market_symbol="ETH", side="short", size="1", avg_entry_price="3000")
        ev = Event(kind=EventKind.OPEN, trade=t, position_before=None, position_after=pos)
        msg = format_event(ev, "")
        assert "SHORT" in msg


# ---------------------------------------------------------------------------
# format_event — CLOSE
# ---------------------------------------------------------------------------

class TestFormatEventClose:
    def _close_event(self, pnl=None):
        t = make_trade(
            market_symbol="BTC", side="short", size="0.1", price="55000",
            realized_pnl=pnl,
        )
        pos_before = make_position(
            market_symbol="BTC", side="long", size="0.1", avg_entry_price="50000",
        )
        return Event(kind=EventKind.CLOSE, trade=t, position_before=pos_before, position_after=None)

    def test_contains_closed(self):
        msg = format_event(self._close_event(), "")
        assert "Closed" in msg

    def test_pnl_shown_when_present(self):
        msg = format_event(self._close_event(pnl="500"), "")
        assert "P&L:" in msg
        assert "+$500" in msg

    def test_pnl_negative(self):
        msg = format_event(self._close_event(pnl="-200"), "")
        assert "$-200.00" in msg

    def test_no_pnl_no_pnl_line(self):
        msg = format_event(self._close_event(), "")
        assert "P&L:" not in msg

    def test_no_sl_tp_on_close(self):
        msg = format_event(self._close_event(), "", sl=Decimal("40000"), tp=Decimal("60000"))
        # SL/TP lines are intentionally omitted on close (orders are cancelled)
        assert "SL:" not in msg
        assert "TP:" not in msg


# ---------------------------------------------------------------------------
# format_event — REDUCE
# ---------------------------------------------------------------------------

class TestFormatEventReduce:
    def _reduce_event(self, pnl=None):
        t = make_trade(
            market_symbol="ETH", side="short", size="1", price="3200",
            realized_pnl=pnl,
        )
        pos_before = make_position(market_symbol="ETH", side="long", size="3", avg_entry_price="3000")
        pos_after  = make_position(market_symbol="ETH", side="long", size="2", avg_entry_price="3000")
        return Event(kind=EventKind.REDUCE, trade=t, position_before=pos_before, position_after=pos_after)

    def test_contains_reduced(self):
        msg = format_event(self._reduce_event(), "")
        assert "Reduced" in msg

    def test_shows_remaining_and_was(self):
        msg = format_event(self._reduce_event(), "")
        assert "Remaining:" in msg
        assert "was" in msg

    def test_pnl_shown_when_present(self):
        msg = format_event(self._reduce_event(pnl="200"), "")
        assert "P&L:" in msg

    def test_sl_tp_shown_on_reduce(self):
        ev = self._reduce_event()
        msg = format_event(ev, "", sl=Decimal("2800"), tp=Decimal("3500"))
        assert "SL:" in msg
        assert "TP:" in msg


# ---------------------------------------------------------------------------
# format_aggregate
# ---------------------------------------------------------------------------

class TestFormatAggregate:
    def _pos(self):
        return make_position(
            market_symbol="SUI", side="long", size="10000",
            avg_entry_price="1.0371",
        )

    def test_contains_added(self):
        msg = format_aggregate(self._pos(), Decimal("5000"), 13, 10.0, "", "My NK pool")
        assert "Added" in msg

    def test_contains_fill_count(self):
        msg = format_aggregate(self._pos(), Decimal("5000"), 13, 10.0, "", "My NK pool")
        assert "13" in msg
        assert "fills" in msg

    def test_singular_fill_word(self):
        msg = format_aggregate(self._pos(), Decimal("500"), 1, 5.0, "", "pool")
        assert "fill" in msg
        assert "fills" not in msg

    def test_contains_net_added(self):
        msg = format_aggregate(self._pos(), Decimal("5000"), 13, 10.0, "", "pool")
        assert "$5,000" in msg

    def test_contains_avg_entry(self):
        msg = format_aggregate(self._pos(), Decimal("5000"), 13, 10.0, "", "pool")
        assert "1.0371" in msg

    def test_leverage_shown(self):
        msg = format_aggregate(self._pos(), Decimal("5000"), 13, 10.0, "", "pool")
        assert "10x" in msg

    def test_leverage_none_not_shown(self):
        msg = format_aggregate(self._pos(), Decimal("5000"), 13, None, "", "pool")
        assert "x" not in msg

    def test_sl_tp_appended(self):
        msg = format_aggregate(
            self._pos(), Decimal("5000"), 13, 10.0, "",
            sl=Decimal("0.9"), tp=Decimal("1.2"),
        )
        assert "SL:" in msg
        assert "TP:" in msg

    def test_pool_url_in_footer(self):
        msg = format_aggregate(self._pos(), Decimal("5000"), 13, 10.0, "https://pool.url", "pool")
        assert msg.endswith("https://pool.url")

    def test_source_name_in_header(self):
        msg = format_aggregate(self._pos(), Decimal("5000"), 13, 10.0, "", "My NK pool")
        assert "My NK pool" in msg


# ---------------------------------------------------------------------------
# format_sl_tp_set
# ---------------------------------------------------------------------------

class TestFormatSlTpSet:
    def test_both_set_contains_shield_marker(self):
        msg = format_sl_tp_set("NK", "long", "BTC", Decimal("45000"), Decimal("60000"))
        assert "🛡" in msg

    def test_both_set_contains_sl_and_tp(self):
        msg = format_sl_tp_set("NK", "long", "BTC", Decimal("45000"), Decimal("60000"))
        assert "SL:" in msg
        assert "TP:" in msg

    def test_both_set_direction_long(self):
        msg = format_sl_tp_set("NK", "long", "ETH", Decimal("2000"), Decimal("3000"))
        assert "LONG" in msg
        assert "🟢" in msg

    def test_both_set_direction_short(self):
        msg = format_sl_tp_set("NK", "short", "ETH", Decimal("2000"), Decimal("1000"))
        assert "SHORT" in msg
        assert "🔴" in msg

    def test_sl_only(self):
        msg = format_sl_tp_set("NK", "long", "BTC", Decimal("45000"), None)
        assert "SL:" in msg
        assert "TP:" not in msg
        assert "🛡" in msg

    def test_tp_only(self):
        msg = format_sl_tp_set("NK", "long", "BTC", None, Decimal("60000"))
        assert "TP:" in msg
        assert "SL:" not in msg
        assert "🛡" in msg

    def test_both_none_returns_empty_string(self):
        result = format_sl_tp_set("NK", "long", "BTC", None, None)
        assert result == ""

    def test_contains_market_symbol(self):
        msg = format_sl_tp_set("NK", "long", "HYPE", Decimal("50"), Decimal("80"))
        assert "HYPE" in msg

    def test_source_name_header(self):
        msg = format_sl_tp_set("My NK pool", "long", "BTC", Decimal("45000"), Decimal("60000"))
        assert msg.startswith("📍 My NK pool")

    def test_no_source_name_no_header_pin(self):
        msg = format_sl_tp_set("", "long", "BTC", Decimal("45000"), Decimal("60000"))
        assert not msg.startswith("📍")

    def test_pool_url_footer(self):
        msg = format_sl_tp_set("NK", "long", "BTC", Decimal("45000"), Decimal("60000"),
                               pool_url="https://example.com/pool")
        assert msg.endswith("https://example.com/pool")

    def test_no_pool_url_no_trailing_newline(self):
        msg = format_sl_tp_set("NK", "long", "BTC", Decimal("45000"), Decimal("60000"), pool_url="")
        assert not msg.endswith("\n")

    def test_price_formatting_large(self):
        msg = format_sl_tp_set("NK", "long", "BTC", Decimal("50000"), Decimal("60000"))
        assert "$50,000.00" in msg
        assert "$60,000.00" in msg

    def test_price_formatting_small(self):
        msg = format_sl_tp_set("NK", "long", "SUI", Decimal("1.25"), Decimal("2.50"))
        assert "$1.2500" in msg
        assert "$2.5000" in msg


# ---------------------------------------------------------------------------
# format_reduce_aggregate
# ---------------------------------------------------------------------------

class TestFormatReduceAggregate:
    def _pos(self):
        return make_position(
            market_symbol="SOL", side="long", size="50",
            avg_entry_price="150",
        )

    def test_contains_reduced(self):
        msg = format_reduce_aggregate(self._pos(), Decimal("3000"), 5, Decimal("150"), 5.0, "", "pool")
        assert "Reduced" in msg

    def test_contains_remaining_notional(self):
        msg = format_reduce_aggregate(self._pos(), Decimal("3000"), 5, None, None, "", "pool")
        assert "remaining" in msg or "Remaining" in msg or "$7,500" in msg

    def test_pnl_shown_when_present(self):
        msg = format_reduce_aggregate(self._pos(), Decimal("3000"), 5, Decimal("150"), 5.0, "", "pool")
        assert "P&L:" in msg
        assert "+$150" in msg

    def test_pnl_none_not_shown(self):
        msg = format_reduce_aggregate(self._pos(), Decimal("3000"), 5, None, None, "", "pool")
        assert "P&L:" not in msg

    def test_sl_tp_appended(self):
        msg = format_reduce_aggregate(
            self._pos(), Decimal("3000"), 5, None, None, "",
            sl=Decimal("130"), tp=Decimal("180"),
        )
        assert "SL:" in msg
        assert "TP:" in msg
