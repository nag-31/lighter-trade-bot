from __future__ import annotations

from decimal import Decimal
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..core.models import RoundTrip


W, H = 920, 500
BG = (15, 20, 28)
PANEL = (24, 30, 39)
TEXT = (230, 236, 245)
MUTED = (145, 155, 170)
GREEN = (34, 197, 94)
RED = (248, 72, 72)
DIV = (42, 50, 62)


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    names = ["arialbd.ttf", "DejaVuSans-Bold.ttf"] if bold else ["arial.ttf", "DejaVuSans.ttf"]
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _money(v: Decimal) -> str:
    sign = "+" if v >= 0 else "-"
    return f"{sign}${abs(v):,.2f}"


def _pct(v: Decimal | None) -> str:
    if v is None:
        return "n/a"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}%"


def render_round_trip_card(rt: RoundTrip, *, output_path: Path | None = None) -> bytes:
    accent = GREEN if rt.is_win else RED
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([(18, 16), (W - 18, H - 16)], radius=8, fill=PANEL)
    draw.rectangle([(0, 0), (8, H)], fill=accent)

    f_title = _font(26, True)
    f_label = _font(15)
    f_huge = _font(72, True)
    f_big = _font(34, True)
    f_small = _font(20)
    f_tiny = _font(13)

    draw.text((42, 34), f"{rt.source} · {rt.symbol}", font=f_title, fill=MUTED)
    draw.text((42, 68), f"{rt.direction.upper()} · CLOSED ROUND TRIP", font=f_small, fill=accent)

    pnl = _money(rt.net_pnl)
    pnl_box = draw.textbbox((0, 0), pnl, font=f_huge)
    draw.text(((W - (pnl_box[2] - pnl_box[0])) // 2, 108), pnl, font=f_huge, fill=accent)

    roc = f"Return on cost  {_pct(rt.return_on_cost)}"
    roc_box = draw.textbbox((0, 0), roc, font=f_big)
    draw.text(((W - (roc_box[2] - roc_box[0])) // 2, 200), roc, font=f_big, fill=accent)

    draw.line([(42, 270), (W - 42, 270)], fill=DIV, width=2)
    details = [
        ("ENTRY VWAP", f"${(rt.avg_entry or Decimal('0')):,.4f}"),
        ("EXIT VWAP", f"${(rt.avg_exit or Decimal('0')):,.4f}"),
        ("CLOSED SIZE", f"{rt.closed_qty.normalize()}"),
        ("COST BASIS", f"${rt.cost_basis:,.2f}"),
        ("FEES", f"${rt.fees:,.2f}"),
    ]
    col_w = (W - 84) // len(details)
    for i, (label, value) in enumerate(details):
        x = 42 + i * col_w
        draw.text((x, 292), label, font=f_tiny, fill=MUTED)
        draw.text((x, 314), value, font=f_label, fill=TEXT)

    draw.line([(42, 364), (W - 42, 364)], fill=DIV, width=2)
    status = "Funding complete" if rt.funding_status == "complete" else "Funding unknown"
    draw.text((42, 386), status, font=f_small, fill=MUTED)
    draw.text((42, 420), f"Fills: {len(rt.entry_fill_ids)} entry, {len(rt.exit_fill_ids)} exit", font=f_small, fill=MUTED)
    draw.text((W - 220, H - 44), "Standalone PnL Analytics", font=f_tiny, fill=MUTED)

    buf = BytesIO()
    img.save(buf, format="PNG")
    data = buf.getvalue()
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(data)
    return data

