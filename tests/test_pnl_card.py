"""Tests for pnl_card.calculate_pnl — exchange-reported vs fallback PnL calculation
and accumulated multi-fill PnL aggregation."""

from decimal import Decimal
from typing import Optional

import pytest

from src.pnl_card import calculate_pnl
from src.types import Event, EventKind, Position, Trade
from tests.conftest import make_position, make_trade


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_close_event(
    *,
    side: str = "long",
    entry_price: str = "100",
    exit_price: str = "110",
    size: str = "10",
    realized_pnl: Optional[str] = None,
) -> Event:
    """Build a minimal CLOSE Event for testing calculate_pnl."""
    pos = make_position(
        market_id=1,
        market_symbol="BTC",
        side=side,
        size=size,
        avg_entry_price=entry_price,
    )
    trade = make_trade(
        trade_id=99,
        market_id=1,
        market_symbol="BTC",
        side="short" if side == "long" else "long",  # closing side
        size=size,
        price=exit_price,
        realized_pnl=realized_pnl,
    )
    return Event(
        kind=EventKind.CLOSE,
        trade=trade,
        position_before=pos,
        position_after=None,
    )


# ---------------------------------------------------------------------------
# Exchange-reported PnL (HL closedPnl path)
# ---------------------------------------------------------------------------

class TestCalculatePnlExchangeReported:
    def test_uses_realized_pnl_when_set(self):
        """HL fill carries closedPnl — use it directly, don't recalculate."""
        ev = make_close_event(
            side="long",
            entry_price="100",
            exit_price="110",
            size="10",
            realized_pnl="95",   # exchange says $95, not the manual $100
        )
        result = calculate_pnl(ev)
        assert result == Decimal("95")

    def test_negative_realized_pnl(self):
        """Loss trade — exchange-reported negative PnL preserved correctly."""
        ev = make_close_event(
            side="long",
            entry_price="100",
            exit_price="90",
            size="10",
            realized_pnl="-87.50",
        )
        result = calculate_pnl(ev)
        assert result == Decimal("-87.50")

    def test_zero_realized_pnl(self):
        """Zero PnL (breakeven) — preserved as zero, not as None."""
        ev = make_close_event(
            side="long",
            entry_price="100",
            exit_price="100",
            size="10",
            realized_pnl="0",
        )
        result = calculate_pnl(ev)
        assert result == Decimal("0")


# ---------------------------------------------------------------------------
# accumulated_pnl from prior REDUCE fills
# ---------------------------------------------------------------------------

class TestAccumulatedPnl:
    def test_accumulated_added_to_realized_pnl(self):
        """Position closed in 2 steps: REDUCE ($30 PnL) then final CLOSE ($50 PnL).
        PnL card must show $80 total, not just the last fill's $50."""
        ev = make_close_event(
            side="long",
            entry_price="100",
            exit_price="110",
            size="5",
            realized_pnl="50",   # final closing fill PnL
        )
        accumulated = Decimal("30")   # from prior REDUCE fills
        result = calculate_pnl(ev, accumulated_pnl=accumulated)
        assert result == Decimal("80")

    def test_accumulated_none_is_backward_compatible(self):
        """No prior REDUCE fills (position closed in one shot) — unchanged behaviour."""
        ev = make_close_event(
            side="long",
            entry_price="100",
            exit_price="110",
            size="10",
            realized_pnl="100",
        )
        result = calculate_pnl(ev, accumulated_pnl=None)
        assert result == Decimal("100")

    def test_accumulated_zero_is_valid(self):
        """Explicit Decimal('0') accumulated — still works, adds 0."""
        ev = make_close_event(
            side="long",
            entry_price="100",
            exit_price="110",
            size="10",
            realized_pnl="100",
        )
        result = calculate_pnl(ev, accumulated_pnl=Decimal("0"))
        assert result == Decimal("100")

    def test_accumulated_negative_reduces_total(self):
        """Some REDUCE fills were losing; final close profitable — show net PnL."""
        ev = make_close_event(
            side="long",
            entry_price="100",
            exit_price="120",
            size="5",
            realized_pnl="100",   # last fill profit
        )
        accumulated = Decimal("-40")  # earlier partial closes were losses
        result = calculate_pnl(ev, accumulated_pnl=accumulated)
        assert result == Decimal("60")

    def test_accumulated_makes_winning_trade_losing(self):
        """Net loss even though final fill was a win."""
        ev = make_close_event(
            side="long",
            entry_price="100",
            exit_price="110",
            size="2",
            realized_pnl="20",
        )
        accumulated = Decimal("-100")  # earlier REDUCE fills were heavily losing
        result = calculate_pnl(ev, accumulated_pnl=accumulated)
        assert result == Decimal("-80")


# ---------------------------------------------------------------------------
# Fallback calculation (Lighter — no per-fill realized_pnl)
# ---------------------------------------------------------------------------

class TestCalculatePnlFallback:
    def test_long_profit_fallback(self):
        """Lighter long: exit > entry → positive PnL calculated from prices."""
        ev = make_close_event(
            side="long",
            entry_price="100",
            exit_price="120",
            size="10",
            realized_pnl=None,   # Lighter has no per-fill PnL
        )
        result = calculate_pnl(ev)
        # (120 - 100) * 10 = 200
        assert result == Decimal("200")

    def test_long_loss_fallback(self):
        """Lighter long: exit < entry → negative PnL."""
        ev = make_close_event(
            side="long",
            entry_price="100",
            exit_price="80",
            size="10",
            realized_pnl=None,
        )
        result = calculate_pnl(ev)
        # (80 - 100) * 10 = -200
        assert result == Decimal("-200")

    def test_short_profit_fallback(self):
        """Lighter short: exit < entry → positive PnL."""
        ev = make_close_event(
            side="short",
            entry_price="100",
            exit_price="80",
            size="10",
            realized_pnl=None,
        )
        result = calculate_pnl(ev)
        # (100 - 80) * 10 = 200
        assert result == Decimal("200")

    def test_short_loss_fallback(self):
        """Lighter short: exit > entry → negative PnL."""
        ev = make_close_event(
            side="short",
            entry_price="100",
            exit_price="120",
            size="10",
            realized_pnl=None,
        )
        result = calculate_pnl(ev)
        # (100 - 120) * 10 = -200
        assert result == Decimal("-200")

    def test_fallback_uses_fill_size_not_position_size(self):
        """For a partial-close scenario: trade.size is the fill amount.
        When position was partly reduced before, trade.size < original position.size.
        The fallback must use trade.size (not position.size) so math is correct."""
        pos = make_position(
            market_id=1, market_symbol="ETH",
            side="long", size="5",  # remaining position at time of close
            avg_entry_price="1000",
        )
        trade = make_trade(
            trade_id=99, market_id=1, market_symbol="ETH",
            side="short", size="5", price="1200",  # closing fill = remaining 5
            realized_pnl=None,
        )
        ev = Event(
            kind=EventKind.CLOSE,
            trade=trade,
            position_before=pos,
            position_after=None,
        )
        result = calculate_pnl(ev)
        # (1200 - 1000) * 5 = 1000
        assert result == Decimal("1000")

    def test_fallback_with_accumulated_pnl(self):
        """Lighter: fallback PnL + accumulated from prior reduces."""
        ev = make_close_event(
            side="long",
            entry_price="100",
            exit_price="110",
            size="5",
            realized_pnl=None,
        )
        # (110 - 100) * 5 = 50 from this fill
        # + 80 from prior reduce fills
        result = calculate_pnl(ev, accumulated_pnl=Decimal("80"))
        assert result == Decimal("130")

    def test_fallback_none_when_no_position_and_no_accumulated(self):
        """No position_before (corrupted state) and no exchange PnL → None."""
        trade = make_trade(
            trade_id=1, market_id=1, side="short", size="5", price="110",
            realized_pnl=None,
        )
        ev = Event(
            kind=EventKind.CLOSE,
            trade=trade,
            position_before=None,   # no position info
            position_after=None,
        )
        result = calculate_pnl(ev)
        assert result is None

    def test_fallback_returns_accumulated_when_no_position(self):
        """No position_before but we DO have accumulated PnL — return what we have."""
        trade = make_trade(
            trade_id=1, market_id=1, side="short", size="5", price="110",
            realized_pnl=None,
        )
        ev = Event(
            kind=EventKind.CLOSE,
            trade=trade,
            position_before=None,
            position_after=None,
        )
        result = calculate_pnl(ev, accumulated_pnl=Decimal("75"))
        assert result == Decimal("75")


# ---------------------------------------------------------------------------
# Multi-fill position lifecycle integration test
# ---------------------------------------------------------------------------

class TestMultiFillLifecycle:
    """Simulate the full flow: REDUCE fills accumulate, then CLOSE adds final fill."""

    def test_three_fill_close_accumulates_correctly(self):
        """Position closed via 3 fills.
        Fill 1 (REDUCE): +$30 realized_pnl
        Fill 2 (REDUCE): +$40 realized_pnl
        Fill 3 (CLOSE):  +$50 realized_pnl — PnL card should show $120 total.
        """
        # Simulate what the reduce buffer accumulates across fills 1 and 2
        accumulated = Decimal("30") + Decimal("40")   # = $70

        # Fill 3 is the CLOSE event
        ev_close = make_close_event(
            side="long",
            entry_price="100",
            exit_price="115",
            size="2",
            realized_pnl="50",   # fill 3's PnL
        )
        total = calculate_pnl(ev_close, accumulated_pnl=accumulated)
        assert total == Decimal("120")

    def test_single_fill_close_no_accumulation(self):
        """Position closed in a single fill — no prior REDUCEs, accumulated_pnl=None."""
        ev_close = make_close_event(
            side="long",
            entry_price="100",
            exit_price="115",
            size="10",
            realized_pnl="150",
        )
        total = calculate_pnl(ev_close, accumulated_pnl=None)
        assert total == Decimal("150")

    def test_accumulated_loss_with_winning_close(self):
        """Position was losing on the reduces but recovered — show net."""
        accumulated = Decimal("-200")  # prior reduces were losing

        ev_close = make_close_event(
            side="long",
            entry_price="100",
            exit_price="120",
            size="10",
            realized_pnl="250",  # big recovery on final close
        )
        total = calculate_pnl(ev_close, accumulated_pnl=accumulated)
        assert total == Decimal("50")  # net $50 win
