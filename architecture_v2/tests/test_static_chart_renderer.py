from __future__ import annotations

import hashlib
import io
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from PIL import Image

from architecture_v2.domain.accounting import project_account
from architecture_v2.domain.charts import Candle, build_trade_chart_spec
from architecture_v2.domain.models import Execution, ExecutionSide, PositionSide
from architecture_v2.tracker.static_chart import (
    BUY_COLOR,
    SELL_COLOR,
    render_trade_chart_png,
)


UTC = timezone.utc


def execution(
    native_id: str,
    *,
    minute: int,
    side: ExecutionSide,
    qty: str,
    price: str,
) -> Execution:
    return Execution.create(
        account_id="hl-main",
        exchange="hyperliquid",
        market_key="hyperliquid:default:BTC",
        position_side=PositionSide.BOTH,
        native_trade_id=native_id,
        occurred_at=datetime(2026, 7, 1, 12, minute, tzinfo=UTC),
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
    )


def chart_spec(*, with_candles: bool):
    projection = project_account(
        "hl-main",
        [
            execution("open", minute=1, side=ExecutionSide.BUY, qty="2", price="100"),
            execution("close", minute=5, side=ExecutionSide.SELL, qty="2", price="110"),
        ],
    )
    candle_rows = []
    if with_candles:
        for index in range(8):
            candle_rows.append(
                Candle(
                    opened_at=datetime(2026, 7, 1, 12, tzinfo=UTC)
                    + timedelta(minutes=index),
                    open=Decimal("100") + index,
                    high=Decimal("103") + index,
                    low=Decimal("98") + index,
                    close=Decimal("101") + index,
                    volume=Decimal("10") + index,
                )
            )
    return build_trade_chart_spec(
        projection.lifecycles[0],
        projection,
        candles=candle_rows,
        interval_seconds=60,
        candle_provenance=(
            "hyperliquid:candleSnapshot" if with_candles else "execution-only"
        ),
    )


def test_renderer_produces_readable_png_with_buy_and_sell_markers() -> None:
    rendered = render_trade_chart_png(chart_spec(with_candles=True))
    image = Image.open(io.BytesIO(rendered)).convert("RGB")
    colors = image.getcolors(maxcolors=image.width * image.height)
    color_counts = {color: count for count, color in colors or []}

    assert image.format is None  # converted image has decoded pixel data
    assert image.size == (1200, 700)
    assert rendered.startswith(b"\x89PNG\r\n\x1a\n")
    assert color_counts.get(BUY_COLOR, 0) > 10
    assert color_counts.get(SELL_COLOR, 0) > 10


def test_renderer_is_deterministic_for_same_chart_spec() -> None:
    spec = chart_spec(with_candles=True)

    first = render_trade_chart_png(spec)
    second = render_trade_chart_png(spec)

    assert hashlib.sha256(first).digest() == hashlib.sha256(second).digest()


def test_execution_only_fallback_still_renders_both_fills() -> None:
    spec = chart_spec(with_candles=False)

    rendered = render_trade_chart_png(spec, width=900, height=540)
    image = Image.open(io.BytesIO(rendered))

    assert image.size == (900, 540)
    assert spec.completeness == "execution_only"
    assert len(spec.markers) == 2


def test_renderer_rejects_dimensions_too_small_for_legible_card() -> None:
    spec = chart_spec(with_candles=True)

    try:
        render_trade_chart_png(spec, width=300, height=200)
    except ValueError as exc:
        assert "dimensions" in str(exc).lower()
    else:
        raise AssertionError("undersized execution chart should fail")
