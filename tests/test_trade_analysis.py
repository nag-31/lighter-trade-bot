from datetime import datetime, timezone
from decimal import Decimal

from src.trade_analysis import (
    AnalysisFill,
    apply_candle_metrics,
    order_hyperliquid_fills,
    reconstruct_hyperliquid_round_trips,
)


T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def fill(
    fid: str,
    *,
    side: str,
    qty: str,
    price: str,
    start: str,
    pnl: str = "0",
    fee: str = "0",
    order: str | None = None,
) -> AnalysisFill:
    return AnalysisFill(
        source_id="hl-main",
        source_name="HL",
        symbol="BTC",
        fill_id=f"default:{fid}",
        order_id=order or fid,
        timestamp=T0,
        side=side,
        qty=Decimal(qty),
        price=Decimal(price),
        start_position=Decimal(start),
        closed_pnl=Decimal(pnl),
        fee=Decimal(fee),
        crossed=True,
    )


def test_same_timestamp_close_burst_follows_start_position_chain():
    rows = [
        fill("3", side="sell", qty="1", price="110", start="1", pnl="10"),
        fill("1", side="sell", qty="1", price="110", start="3", pnl="10"),
        fill("2", side="sell", qty="1", price="110", start="2", pnl="10"),
    ]
    ordered = order_hyperliquid_fills(rows)
    assert [row.start_position for row in ordered] == [
        Decimal("3"),
        Decimal("2"),
        Decimal("1"),
    ]


def test_reconstructs_scale_in_partial_exit_and_full_close_as_one_trade():
    rows = [
        fill("1", side="buy", qty="1", price="100", start="0", fee="0.10"),
        fill("2", side="buy", qty="1", price="110", start="1", fee="0.11"),
        fill(
            "3",
            side="sell",
            qty="1",
            price="120",
            start="2",
            pnl="15",
            fee="0.12",
        ),
        fill(
            "4",
            side="sell",
            qty="1",
            price="90",
            start="1",
            pnl="-15",
            fee="0.09",
        ),
    ]
    result = reconstruct_hyperliquid_round_trips(rows)
    assert len(result) == 1
    trade = result[0]
    assert trade.observed_open is True
    assert trade.avg_entry == Decimal("105")
    assert trade.avg_exit == Decimal("105")
    assert trade.gross_pnl == Decimal("0")
    assert trade.fees == Decimal("0.42")
    assert trade.net_pnl == Decimal("-0.42")
    assert trade.entry_action_count == 2
    assert trade.exit_action_count == 2
    assert [row["closed_pnl"] for row in trade.realization_evidence] == ["15", "-15"]


def test_flip_closes_old_trade_and_opens_new_trade():
    rows = [
        fill("1", side="buy", qty="2", price="100", start="0"),
        fill("2", side="sell", qty="3", price="110", start="2", pnl="20"),
        fill("3", side="buy", qty="1", price="100", start="-1", pnl="10"),
    ]
    result = reconstruct_hyperliquid_round_trips(rows)
    assert [(t.direction, t.gross_pnl) for t in result] == [
        ("long", Decimal("20")),
        ("short", Decimal("10")),
    ]


def test_candle_metrics_are_direction_aware():
    trade = reconstruct_hyperliquid_round_trips(
        [
            fill("1", side="buy", qty="1", price="100", start="0"),
            fill("2", side="sell", qty="1", price="105", start="1", pnl="5"),
        ]
    )[0]
    candles = [
        {
            "t": int(T0.timestamp() * 1000),
            "T": int(T0.timestamp() * 1000) + 60_000,
            "o": "100",
            "h": "110",
            "l": "95",
            "c": "105",
        }
    ]
    apply_candle_metrics(trade, candles, interval="1m")
    assert trade.mfe_pct == Decimal("10.0")
    assert trade.mae_pct == Decimal("-5.00")
    assert trade.capture_ratio == Decimal("0.5")
