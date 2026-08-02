from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO

import pytest
from PIL import Image

pytest.importorskip("plotly")

from architecture_v2.domain.accounting import project_account
from architecture_v2.domain.charts import Candle, build_trade_chart_spec
from architecture_v2.domain.models import Execution, ExecutionSide, PositionSide
from architecture_v2.tracker.plotly_chart import render_plotly_chart_png


UTC = timezone.utc


def _spec(*, with_volume: bool):
    start = datetime(2026, 8, 2, 12, tzinfo=UTC)
    executions = [
        Execution.create(
            account_id="hl-main",
            exchange="hyperliquid",
            market_key="hyperliquid:BTC",
            position_side=PositionSide.BOTH,
            native_trade_id="open",
            occurred_at=start + timedelta(minutes=1),
            side=ExecutionSide.BUY,
            quantity=Decimal("1"),
            price=Decimal("100"),
        ),
        Execution.create(
            account_id="hl-main",
            exchange="hyperliquid",
            market_key="hyperliquid:BTC",
            position_side=PositionSide.BOTH,
            native_trade_id="close",
            occurred_at=start + timedelta(minutes=4),
            side=ExecutionSide.SELL,
            quantity=Decimal("1"),
            price=Decimal("110"),
        ),
    ]
    projection = project_account("hl-main", executions)
    candles = [
        Candle(
            opened_at=start + timedelta(minutes=index),
            open=Decimal("100") + index,
            high=Decimal("103") + index,
            low=Decimal("98") + index,
            close=Decimal("101") + index,
            volume=Decimal("10") + index if with_volume else None,
        )
        for index in range(7)
    ]
    return build_trade_chart_spec(
        projection.lifecycles[0],
        projection,
        candles=candles,
        interval_seconds=60,
        candle_provenance="test:public-candles",
    )


@pytest.mark.parametrize("with_volume", [True, False])
def test_plotly_renderer_exports_exchange_style_png(with_volume):
    rendered = render_plotly_chart_png(_spec(with_volume=with_volume))
    image = Image.open(BytesIO(rendered))

    assert rendered.startswith(b"\x89PNG\r\n\x1a\n")
    assert image.size == (1200, 700)
