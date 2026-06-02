"""Stats card generator — PNG image summarising trade analytics for Telegram.

Mirrors the dark-theme style of pnl_card.py. Returns PNG bytes or None if
Pillow is not installed.
"""

from __future__ import annotations

import io
from typing import Optional

try:
    from PIL import Image, ImageDraw
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

from .pnl_card import _load_font

# ---------------------------------------------------------------------------
# Layout constants (match pnl_card.py palette)
# ---------------------------------------------------------------------------

W, H = 920, 500

_BG        = "#0d1117"
_CARD_BG   = "#161b22"
_GREEN     = "#22c55e"
_RED       = "#ef4444"
_TEXT_PRI  = "#f0f6fc"
_TEXT_SEC  = "#8b949e"
_DIVIDER   = "#21262d"
_ACCENT    = "#93c5fd"   # blue, used for sparkline line when profit_factor is neutral


def _fmt_pnl(v: float | None, dollars: bool = True) -> str:
    """Format a PnL value as ±$x,xxx.xx (or just ±x.xx% if dollars=False)."""
    if v is None:
        return "—"
    sign = "+" if v >= 0 else "−"
    if dollars:
        return f"{sign}${abs(v):,.2f}"
    return f"{sign}{abs(v):.2f}"


def render_stats_card(
    stats: dict,
    title: str = "Trade Stats",
    source_label: str = "",
) -> Optional[bytes]:
    """Render a PNG stats card and return the bytes, or None if Pillow is unavailable.

    The card is ~900x500 px, dark theme, with:
      - Title row
      - KPI grid (Net P&L big + coloured, Win rate, Profit factor, # trades, Avg win, Avg loss)
      - Equity-curve sparkline drawn with draw.line
    """
    if not PIL_AVAILABLE:
        return None

    n_trades = stats.get("n_trades", 0)

    img  = Image.new("RGB", (W, H), _BG)
    draw = ImageDraw.Draw(img)

    # Left accent strip
    total_pnl = stats.get("total_pnl", 0.0) or 0.0
    accent = _GREEN if total_pnl >= 0 else _RED
    draw.rectangle([(0, 0), (7, H)], fill=accent)

    # Card background
    draw.rounded_rectangle([(18, 14), (W - 14, H - 14)], radius=18, fill=_CARD_BG)

    # Fonts
    f_huge  = _load_font(64, bold=True)
    f_big   = _load_font(28, bold=True)
    f_med   = _load_font(22, bold=True)
    f_norm  = _load_font(18)
    f_small = _load_font(15)
    f_tiny  = _load_font(13)

    # ---- Title ----
    header = title
    if source_label:
        header = f"{title}  ·  {source_label}"
    draw.text((42, 28), header, font=f_med, fill=_TEXT_SEC)

    if n_trades == 0:
        # Minimal "no data" card
        msg = "No closed trades yet."
        bbox = draw.textbbox((0, 0), msg, font=f_big)
        tw = bbox[2] - bbox[0]
        draw.text(((W - tw) // 2, H // 2 - 20), msg, font=f_big, fill=_TEXT_SEC)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        buf.seek(0)
        return buf.getvalue()

    # ---- Big Net P&L ----
    pnl_str = _fmt_pnl(total_pnl)
    bbox = draw.textbbox((0, 0), pnl_str, font=f_huge)
    tw = bbox[2] - bbox[0]
    draw.text(((W - tw) // 2, 58), pnl_str, font=f_huge, fill=accent)

    # ---- KPI grid (6 cells in 3 cols x 2 rows) ----
    draw.line([(42, 148), (W - 42, 148)], fill=_DIVIDER, width=1)

    win_rate  = stats.get("win_rate", 0.0) or 0.0
    pf_val    = stats.get("profit_factor")
    avg_win   = stats.get("avg_win", 0.0) or 0.0
    avg_loss  = stats.get("avg_loss", 0.0) or 0.0

    pf_str = f"{pf_val:.2f}" if pf_val is not None else "N/A"

    kpis = [
        ("WIN RATE",       f"{win_rate:.1f}%"),
        ("PROFIT FACTOR",  pf_str),
        ("TRADES",         str(n_trades)),
        ("AVG WIN",        _fmt_pnl(avg_win)),
        ("AVG LOSS",       _fmt_pnl(avg_loss)),
        ("MAX DRAWDOWN",   _fmt_pnl(stats.get("max_drawdown", 0.0))),
    ]

    col_count = 3
    col_w = (W - 84) // col_count
    y_row1 = 158
    y_row2 = 220

    for i, (label, value) in enumerate(kpis):
        col = i % col_count
        row = i // col_count
        x = 42 + col * col_w
        y = y_row1 if row == 0 else y_row2
        draw.text((x, y),      label, font=f_tiny, fill=_TEXT_SEC)
        # Colour avg_loss and max_drawdown red
        val_color = _TEXT_PRI
        if label in ("AVG LOSS", "MAX DRAWDOWN") and value.startswith("−"):
            val_color = _RED
        elif label == "AVG WIN" and value.startswith("+"):
            val_color = _GREEN
        draw.text((x, y + 16), value, font=f_norm, fill=val_color)

    # ---- Divider above sparkline ----
    y_divider = 278
    draw.line([(42, y_divider), (W - 42, y_divider)], fill=_DIVIDER, width=1)

    # ---- Equity curve sparkline ----
    curve = stats.get("equity_curve") or []
    spark_label = "Equity curve"
    draw.text((42, y_divider + 8), spark_label, font=f_tiny, fill=_TEXT_SEC)

    spark_x0 = 42
    spark_x1 = W - 42
    spark_y0 = y_divider + 26
    spark_y1 = H - 42
    spark_w  = spark_x1 - spark_x0
    spark_h  = spark_y1 - spark_y0

    if len(curve) >= 2:
        values = [pt["cum_pnl"] for pt in curve]
        v_min  = min(values)
        v_max  = max(values)
        v_range = v_max - v_min
        if v_range == 0:
            v_range = 1.0  # prevent division by zero for flat curve

        def _map_x(i: int) -> int:
            return spark_x0 + int(i / (len(curve) - 1) * spark_w)

        def _map_y(v: float) -> int:
            # Invert: higher value → lower y
            return spark_y1 - int((v - v_min) / v_range * spark_h)

        pts = [(_map_x(i), _map_y(v)) for i, v in enumerate(values)]
        line_color = _GREEN if values[-1] >= 0 else _RED

        # Draw the polyline segment by segment
        for j in range(len(pts) - 1):
            draw.line([pts[j], pts[j + 1]], fill=line_color, width=2)

        # Zero line if v_min < 0 < v_max
        if v_min < 0 < v_max:
            y_zero = _map_y(0.0)
            draw.line([(spark_x0, y_zero), (spark_x1, y_zero)], fill=_DIVIDER, width=1)
    elif len(curve) == 1:
        # Single point — just a horizontal dash
        mid_y = (spark_y0 + spark_y1) // 2
        draw.line([(spark_x0, mid_y), (spark_x1, mid_y)], fill=_TEXT_SEC, width=1)
    else:
        no_data = "No data"
        bbox = draw.textbbox((0, 0), no_data, font=f_small)
        tw = bbox[2] - bbox[0]
        mid_y = (spark_y0 + spark_y1) // 2
        draw.text(((W - tw) // 2, mid_y - 8), no_data, font=f_small, fill=_TEXT_SEC)

    # ---- Watermark ----
    wm = "NK Capital"
    draw.text((W - 130, H - 28), wm, font=f_tiny, fill=_DIVIDER)

    # ---- Export ----
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf.getvalue()
