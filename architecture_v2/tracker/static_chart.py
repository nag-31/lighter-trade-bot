from __future__ import annotations

import io
from datetime import datetime
from decimal import Decimal

from PIL import Image, ImageDraw, ImageFont

from architecture_v2.domain.charts import TradeChartSpec
from architecture_v2.domain.models import ExecutionSide


BUY_COLOR = (34, 197, 94)
SELL_COLOR = (239, 68, 68)
BG_COLOR = (9, 12, 18)
PANEL_COLOR = (18, 24, 35)
GRID_COLOR = (40, 52, 73)
TEXT_COLOR = (232, 237, 245)
MUTED_COLOR = (150, 164, 184)
CANDLE_UP = (88, 214, 167)
CANDLE_DOWN = (251, 113, 133)
ENTRY_COLOR = (96, 165, 250)
EXIT_COLOR = (250, 204, 21)


def _money(value: Decimal) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):,.2f}"


def _dashed_horizontal(
    draw: ImageDraw.ImageDraw,
    *,
    y: int,
    x0: int,
    x1: int,
    color: tuple[int, int, int],
) -> None:
    for start in range(x0, x1, 14):
        draw.line((start, y, min(start + 7, x1), y), fill=color, width=1)


def render_trade_chart_png(
    spec: TradeChartSpec,
    *,
    width: int = 1200,
    height: int = 700,
) -> bytes:
    """Render a deterministic lifecycle execution chart from one shared spec."""
    if width < 800 or height < 480:
        raise ValueError("chart dimensions must be at least 800x480")

    image = Image.new("RGB", (width, height), BG_COLOR)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()

    left, right = 76, width - 30
    top, bottom = 112, height - 76
    plot_width = right - left
    plot_height = bottom - top
    draw.rounded_rectangle(
        (20, 18, width - 20, height - 22),
        radius=14,
        fill=PANEL_COLOR,
        outline=GRID_COLOR,
        width=1,
    )

    pnl_color = BUY_COLOR if spec.realized_pnl >= 0 else SELL_COLOR
    title = (
        f"{spec.direction.value} {spec.market_key}  "
        f"{_money(spec.realized_pnl)}"
    )
    draw.text((42, 36), title, fill=TEXT_COLOR, font=font)
    status = (
        f"{spec.opened_at.isoformat()} -> "
        f"{spec.closed_at.isoformat() if spec.closed_at else 'ACTIVE'}  "
        f"{spec.interval_seconds // 60 or 1}m"
    )
    draw.text((42, 58), status, fill=MUTED_COLOR, font=font)
    draw.rectangle((42, 82, 142, 86), fill=pnl_color)

    price_values: list[Decimal] = []
    for candle in spec.candles:
        price_values.extend((candle.high, candle.low))
    for marker in spec.markers:
        price_values.append(marker.price_vwap)
    price_values.append(spec.entry_vwap)
    if spec.exit_vwap is not None:
        price_values.append(spec.exit_vwap)
    if not price_values:
        price_values = [Decimal("0"), Decimal("1")]
    price_min = min(price_values)
    price_max = max(price_values)
    price_range = price_max - price_min
    if not price_range:
        price_range = max(abs(price_max) * Decimal("0.01"), Decimal("1"))
    price_min -= price_range * Decimal("0.08")
    price_max += price_range * Decimal("0.08")
    price_range = price_max - price_min

    def y_for(value: Decimal) -> int:
        ratio = float((value - price_min) / price_range)
        return bottom - int(ratio * plot_height)

    times: list[datetime] = [item.opened_at for item in spec.candles]
    times.extend(marker.first_at for marker in spec.markers)
    times.extend(marker.last_at for marker in spec.markers)
    time_start = min(times) if times else spec.opened_at
    time_end = max(times) if times else (spec.closed_at or spec.opened_at)
    time_span = max(1.0, (time_end - time_start).total_seconds())

    def x_for(value: datetime) -> int:
        return left + int(
            (value - time_start).total_seconds() / time_span * plot_width
        )

    # Price grid and labels.
    for index in range(6):
        y = top + int(index / 5 * plot_height)
        draw.line((left, y, right, y), fill=GRID_COLOR, width=1)
        price = price_max - price_range * Decimal(index) / Decimal("5")
        draw.text((right - 68, y + 3), f"{price:,.4f}", fill=MUTED_COLOR, font=font)

    # Candles. When they are unavailable, execution markers still form a
    # truthful price/time timeline.
    candle_width = max(
        3,
        min(14, int(plot_width / max(1, len(spec.candles)) * 0.6)),
    )
    for candle in spec.candles:
        x = x_for(candle.opened_at)
        color = CANDLE_UP if candle.close >= candle.open else CANDLE_DOWN
        draw.line((x, y_for(candle.high), x, y_for(candle.low)), fill=color, width=1)
        y_open = y_for(candle.open)
        y_close = y_for(candle.close)
        body_top = min(y_open, y_close)
        body_bottom = max(y_open, y_close)
        if body_bottom == body_top:
            body_bottom += 1
        draw.rectangle(
            (
                x - candle_width // 2,
                body_top,
                x + candle_width // 2,
                body_bottom,
            ),
            fill=color,
        )

    # With no candle provider, the execution markers are the chart's only
    # price/time series. Connect them so a two-fill trade (such as a small
    # priced token like LDO) cannot look like an empty canvas in Telegram's
    # thumbnail. The marker triangles remain the semantic event labels.
    execution_points = [
        (x_for(marker.first_at), y_for(marker.price_vwap))
        for marker in spec.markers
    ]
    if execution_points:
        path_color = BUY_COLOR if spec.direction.value == "LONG" else SELL_COLOR
        if len(execution_points) > 1:
            draw.line(execution_points, fill=path_color, width=3)
        for x, y in execution_points:
            draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill=path_color)

    if not spec.candles:
        notice_left = right - 232
        notice_top = top + 8
        draw.rounded_rectangle(
            (notice_left, notice_top, right - 8, notice_top + 22),
            radius=5,
            fill=BG_COLOR,
            outline=GRID_COLOR,
            width=1,
        )
        draw.text(
            (notice_left + 8, notice_top + 6),
            "EXECUTION-ONLY / NO CANDLES",
            fill=MUTED_COLOR,
            font=font,
        )

    # Entry and exit VWAP are review overlays, not accounting entries.
    entry_y = y_for(spec.entry_vwap)
    _dashed_horizontal(
        draw, y=entry_y, x0=left, x1=right, color=ENTRY_COLOR
    )
    draw.text((left + 5, entry_y - 14), "ENTRY VWAP", fill=ENTRY_COLOR, font=font)
    if spec.exit_vwap is not None:
        exit_y = y_for(spec.exit_vwap)
        _dashed_horizontal(
            draw, y=exit_y, x0=left, x1=right, color=EXIT_COLOR
        )
        draw.text((left + 5, exit_y + 3), "EXIT VWAP", fill=EXIT_COLOR, font=font)

    for index, marker in enumerate(spec.markers):
        x = x_for(marker.first_at)
        y = y_for(marker.price_vwap)
        label = marker.action.value.replace("_", " ")
        if marker.raw_fill_count > 1:
            label += f" x{marker.raw_fill_count}"
        if marker.side is ExecutionSide.BUY:
            color = BUY_COLOR
            points = ((x, y - 13), (x - 8, y + 1), (x + 8, y + 1))
            label_y = y + 5 + (index % 2) * 13
        else:
            color = SELL_COLOR
            points = ((x, y + 13), (x - 8, y - 1), (x + 8, y - 1))
            label_y = y - 27 - (index % 2) * 13
        draw.polygon(points, fill=color)
        label_x = max(left, min(x - 30, right - 130))
        draw.text((label_x, label_y), label, fill=color, font=font)

    footer = (
        f"Candles: {spec.candle_provenance}  |  "
        f"Coverage: {spec.completeness}  |  "
        f"Chart spec v{spec.version}"
    )
    draw.text((42, height - 52), footer, fill=MUTED_COLOR, font=font)

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=9)
    return output.getvalue()
