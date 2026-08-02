from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO

from PIL import Image

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


def test_execution_only_chart_draws_a_visible_path_for_small_price_markets(monkeypatch):
    monkeypatch.setenv("CHART_RENDERER", "pillow")
    close = _trade(3, 3, "0.34", "8368.7")
    position = Position(
        market_id=46,
        market_symbol="LDO",
        side="short",
        size=Decimal("8368.7"),
        avg_entry_price=Decimal("0.35848"),
        source="Lighter Wallet",
        source_id="lighter-wallet",
        exchange="lighter",
        position_side="BOTH",
    )
    chart = render_legacy_execution_chart(
        source_id="lighter-wallet",
        source_name="Lighter Wallet",
        exchange="lighter",
        market_symbol="LDO",
        position=position,
        close_trade=close,
        reduced_size=Decimal("8368.7"),
        fill_price=Decimal("0.34"),
        realized_pnl=Decimal("154.50"),
    )

    image = Image.open(BytesIO(chart)).convert("RGB")
    # SELL_COLOR is the execution path for a short lifecycle. A long path
    # spanning the plot should contain substantially more pixels than the
    # marker triangles alone, proving the execution-only state is visible.
    sell_pixels = sum(
        1 for pixel in image.get_flattened_data() if pixel == (239, 68, 68)
    )
    assert sell_pixels > 1_000


def test_exchange_style_chart_can_be_unplugged_without_changing_inputs(monkeypatch):
    monkeypatch.setenv("CHART_STYLE", "classic")
    monkeypatch.setenv("CHART_RENDERER", "plotly")
    close = _trade(3, 3, "110", "6")
    position = Position(
        market_id=1,
        market_symbol="BTC",
        side="long",
        size=Decimal("6"),
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
    )

    image = Image.open(BytesIO(chart))
    assert image.size == (1200, 700)
