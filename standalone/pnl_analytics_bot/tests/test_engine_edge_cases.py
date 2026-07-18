from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from standalone.pnl_analytics_bot.core.engine import PnlReconstructor
from standalone.pnl_analytics_bot.core.metrics import compute_analytics, filter_round_trips
from standalone.pnl_analytics_bot.core.models import RawFill


def t(minute: int) -> datetime:
    return datetime(2026, 6, 1, 0, minute, tzinfo=timezone.utc)


def f(
    fill_id: str,
    minute: int,
    side: str,
    qty: str,
    price: str,
    *,
    source: str = "Lighter",
    account: str = "acct",
    symbol: str = "BTC",
    fee: str = "0",
    exchange_realized_pnl: str | None = None,
    funding: str | None = "0",
) -> RawFill:
    return RawFill(
        source=source,
        account=account,
        symbol=symbol,
        fill_id=fill_id,
        timestamp=t(minute),
        side=side,  # type: ignore[arg-type]
        qty=Decimal(qty),
        price=Decimal(price),
        fee=Decimal(fee),
        exchange_realized_pnl=Decimal(exchange_realized_pnl) if exchange_realized_pnl is not None else None,
        funding=Decimal(funding) if funding is not None else None,
        sequence=int(fill_id),
        raw={"id": fill_id},
    )


def r(fills):
    return PnlReconstructor().reconstruct(fills)


def test_simple_long_win_and_loss():
    result = r([
        f("1", 1, "buy", "1", "100", symbol="A"),
        f("2", 2, "sell", "1", "120", symbol="A"),
        f("3", 3, "buy", "1", "100", symbol="B"),
        f("4", 4, "sell", "1", "80", symbol="B"),
    ])
    assert [rt.net_pnl for rt in result.round_trips] == [Decimal("20"), Decimal("-20")]
    analytics = compute_analytics(result.round_trips)
    assert analytics["closed_trades"] == 2
    assert analytics["wins"] == 1
    assert analytics["win_rate"] == "50.00000000"


def test_simple_short_win_and_loss():
    result = r([
        f("1", 1, "sell", "2", "100", symbol="A"),
        f("2", 2, "buy", "2", "80", symbol="A"),
        f("3", 3, "sell", "2", "100", symbol="B"),
        f("4", 4, "buy", "2", "120", symbol="B"),
    ])
    assert [rt.net_pnl for rt in result.round_trips] == [Decimal("40"), Decimal("-40")]


def test_partials_before_final_close_count_as_one_trade():
    result = r([
        f("1", 1, "buy", "3", "100"),
        f("2", 2, "sell", "1", "90"),
        f("3", 3, "sell", "2", "120"),
    ])
    rt = result.round_trips[0]
    assert len(rt.realizations) == 2
    assert rt.net_pnl == Decimal("30")
    assert rt.return_on_cost == Decimal("10.0")
    assert compute_analytics(result.round_trips)["closed_trades"] == 1


def test_final_fill_profitable_but_total_round_trip_losing():
    result = r([
        f("1", 1, "buy", "3", "100"),
        f("2", 2, "sell", "2", "70"),
        f("3", 3, "sell", "1", "130"),
    ])
    rt = result.round_trips[0]
    assert rt.realizations[-1].gross_pnl == Decimal("30")
    assert rt.net_pnl == Decimal("-30")
    assert not rt.is_win


def test_final_fill_losing_but_total_round_trip_profitable():
    result = r([
        f("1", 1, "buy", "3", "100"),
        f("2", 2, "sell", "2", "130"),
        f("3", 3, "sell", "1", "80"),
    ])
    rt = result.round_trips[0]
    assert rt.realizations[-1].gross_pnl == Decimal("-20")
    assert rt.net_pnl == Decimal("40")
    assert rt.is_win


def test_multiple_exit_prices_use_vwap_and_cost_basis_percent():
    result = r([
        f("1", 1, "buy", "4", "100"),
        f("2", 2, "sell", "1", "110"),
        f("3", 3, "sell", "3", "130"),
    ])
    rt = result.round_trips[0]
    assert rt.avg_exit == Decimal("125")
    assert rt.cost_basis == Decimal("400")
    assert rt.net_pnl == Decimal("100")
    assert rt.return_on_cost == Decimal("25.00")


def test_scale_in_before_scale_out_uses_weighted_average_entry():
    result = r([
        f("1", 1, "buy", "1", "100"),
        f("2", 2, "buy", "3", "200"),
        f("3", 3, "sell", "4", "175"),
    ])
    rt = result.round_trips[0]
    assert rt.avg_entry == Decimal("175")
    assert rt.net_pnl == Decimal("0")
    assert not rt.is_win


def test_full_position_flip_closes_old_trade_and_opens_remainder():
    result = r([
        f("1", 1, "buy", "2", "100"),
        f("2", 2, "sell", "3", "90"),
    ])
    assert len(result.round_trips) == 1
    assert result.round_trips[0].net_pnl == Decimal("-20")
    assert len(result.open_positions) == 1
    assert result.open_positions[0].direction == "short"
    assert result.open_positions[0].qty == Decimal("1")
    assert result.open_positions[0].avg_entry == Decimal("90")


def test_same_symbol_reopened_after_close_is_new_round_trip():
    result = r([
        f("1", 1, "buy", "1", "100"),
        f("2", 2, "sell", "1", "110"),
        f("3", 3, "buy", "1", "200"),
        f("4", 4, "sell", "1", "190"),
    ])
    assert len(result.round_trips) == 2
    assert [rt.net_pnl for rt in result.round_trips] == [Decimal("10"), Decimal("-10")]


def test_interleaved_symbols_and_sources_are_independent():
    result = r([
        f("1", 1, "buy", "1", "100", source="Lighter", symbol="BTC"),
        f("2", 2, "buy", "1", "50", source="Hyperliquid", symbol="ETH"),
        f("3", 3, "sell", "1", "60", source="Hyperliquid", symbol="ETH"),
        f("4", 4, "sell", "1", "120", source="Lighter", symbol="BTC"),
    ])
    assert sorted((rt.source, rt.symbol, rt.net_pnl) for rt in result.round_trips) == [
        ("Hyperliquid", "ETH", Decimal("10")),
        ("Lighter", "BTC", Decimal("20")),
    ]


def test_zero_pnl_breakeven_counts_as_non_win():
    result = r([f("1", 1, "buy", "1", "100"), f("2", 2, "sell", "1", "100")])
    analytics = compute_analytics(result.round_trips)
    assert result.round_trips[0].net_pnl == Decimal("0")
    assert analytics["wins"] == 0
    assert analytics["losses"] == 0
    assert analytics["breakevens"] == 1
    assert analytics["non_wins"] == 1


def test_hyperliquid_exchange_pnl_and_fees_are_net_basis():
    result = r([
        f("1", 1, "buy", "1", "100", source="Hyperliquid", fee="1", exchange_realized_pnl="0"),
        f("2", 2, "sell", "1", "120", source="Hyperliquid", fee="2", exchange_realized_pnl="19"),
    ])
    rt = result.round_trips[0]
    assert rt.gross_pnl == Decimal("20")
    assert rt.net_pnl == Decimal("16")
    assert rt.fees == Decimal("3")
    assert len(result.mismatches) == 1


def test_lighter_missing_funding_marks_unknown_but_still_calculates():
    result = r([f("1", 1, "buy", "1", "100", funding=None), f("2", 2, "sell", "1", "110", funding=None)])
    rt = result.round_trips[0]
    assert rt.net_pnl == Decimal("10")
    assert rt.funding_status == "unknown"


def test_funding_present_is_included_in_net_pnl():
    result = r([f("1", 1, "buy", "1", "100"), f("2", 2, "sell", "1", "110", funding="-3")])
    assert result.round_trips[0].net_pnl == Decimal("7")


def test_duplicate_fills_are_skipped_and_out_of_order_is_sorted():
    fills = [
        f("2", 2, "sell", "1", "120"),
        f("1", 1, "buy", "1", "100"),
        f("2", 2, "sell", "1", "120"),
    ]
    result = r(fills)
    assert result.duplicates_skipped == 1
    assert result.round_trips[0].net_pnl == Decimal("20")


def test_cutoff_modes_are_configurable():
    result = r([
        f("1", 1, "buy", "2", "100"),
        f("2", 2, "sell", "1", "90"),
        f("3", 3, "sell", "1", "130"),
    ])
    start = t(3)
    assert len(filter_round_trips(result.round_trips, start=start, cutoff_mode="raw-fill")) == 1
    assert len(filter_round_trips(result.round_trips, start=start, cutoff_mode="full-round-trip")) == 0


def test_invalid_cutoff_mode_raises():
    with pytest.raises(ValueError):
        filter_round_trips([], start=t(1), cutoff_mode="bad")


def test_open_positions_are_excluded_from_closed_win_rate():
    result = r([
        f("1", 1, "buy", "1", "100", symbol="OPEN"),
        f("2", 2, "buy", "1", "100", symbol="CLOSED"),
        f("3", 3, "sell", "1", "120", symbol="CLOSED"),
    ])
    analytics = compute_analytics(result.round_trips, result.open_positions)
    assert analytics["closed_trades"] == 1
    assert analytics["open_positions"] == 1
    assert analytics["win_rate"] == "100.00000000"
