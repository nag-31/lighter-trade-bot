from datetime import datetime, timezone
from decimal import Decimal

from src.execution_chart import render_legacy_execution_chart
from src.types import Position, Trade


def _trade(trade_id: int, minute: int, price: str, size: str) -> Trade:
    return Trade(
        trade_id=trade_id,
        timestamp=datetime(2026, 8, 2, 12, minute, tzinfo=timezone.utc),
        market_id=1,
        market_symbol="BTC",
        side="long",
        size=Decimal(size),
        price=Decimal(price),
        source_id="hl-main",
        exchange="hyperliquid",
        native_trade_id=str(trade_id),
        position_side="BOTH",
    )


def test_legacy_close_inputs_render_execution_only_chart_with_markers():
    close = _trade(3, 3, "110", "6")
    position = Position(
        market_id=1,
        market_symbol="BTC",
        side="long",
        size=Decimal("10"),
        avg_entry_price=Decimal("100"),
        source="HL",
        source_id="hl-main",
        exchange="hyperliquid",
        position_side="BOTH",
    )
    chart = render_legacy_execution_chart(
        source_id="hl-main",
        source_name="HL",
        exchange="hyperliquid",
        market_symbol="BTC",
        position=position,
        close_trade=close,
        reduced_size=Decimal("6"),
        fill_price=Decimal("110"),
        realized_pnl=Decimal("60"),
        partial_rows=[
            {
                "ts": "2026-08-02T12:02:00+00:00",
                "size": "4",
                "entry": "100",
                "exit": "105",
                "pnl": "20",
                "trade_id": 2,
            }
        ],
    )

    assert chart.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(chart) > 10_000

