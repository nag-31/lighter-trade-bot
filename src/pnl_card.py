"""PnL card generator.

Generates a styled PNG image for CLOSE trade events and sends it to Telegram
as a photo so it renders inline in the channel.

Dependencies: Pillow (optional — falls back to plain text if not installed).
"""

from __future__ import annotations

import io
import random
from collections import deque
from decimal import Decimal
from typing import Optional

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

from .types import Event, Position

# ---------------------------------------------------------------------------
# Psychology quotes
# ---------------------------------------------------------------------------

_QUOTES_GREEN = [
    "Discipline is the bridge between goals and accomplishment.",
    "The market rewards patience. You earned this one.",
    "Plan the trade, trade the plan. Well executed.",
    "Process over outcome. You did it right.",
    "Every green trade is a vote of confidence in your system.",
    "Risk managed, profit taken. That's the whole game.",
    "Consistency beats intensity — keep stacking.",
    "A good trade follows your rules. Profit is just confirmation.",
    "The best traders are disciplined, not lucky.",
    "Cut losses short, let winners run. You did both.",
    "Execution is everything. Today it was perfect.",
    "The market paid you for following your rules.",
    "Size right, entry right, exit right. Nothing left to chance.",
]

_QUOTES_RED = [
    "Never risk more than you can afford to lose.",
    "Cut losses quickly — capital preservation is everything.",
    "A loss is tuition. What did the market teach you today?",
    "The best traders lose small and win big. Size always matters.",
    "No stop-loss, no survival. Always protect your capital.",
    "One bad trade never defines a great trader.",
    "Revenge trading destroys accounts. Step away and reset.",
    "Risk management is the only edge fully within your control.",
    "Journal this trade — understanding losses prevents repeating them.",
    "Losers add to losing positions. Winners cut and move on.",
    "Your job is to manage risk. The profits take care of themselves.",
    "Losing is part of the game. Losing big is a choice.",
    "The market will be here tomorrow. Preserve capital to play again.",
]

# ---------------------------------------------------------------------------
# Rolling win/loss tracker (last 50 closed trades, resets on restart)
# ---------------------------------------------------------------------------

_score_window: deque[bool] = deque(maxlen=50)


def record_result(is_win: bool) -> tuple[int, int]:
    """Record a close result and return (wins, total) from recent window."""
    _score_window.append(is_win)
    wins = sum(_score_window)
    total = len(_score_window)
    return wins, total


# ---------------------------------------------------------------------------
# PnL helpers
# ---------------------------------------------------------------------------

def calculate_pnl(event: Event) -> Optional[Decimal]:
    """Realized PnL for a CLOSE event.

    Uses trade.realized_pnl when available (HL fills carry closedPnl).
    For Lighter (no per-fill PnL), calculates from entry/exit prices.
    """
    if event.trade.realized_pnl is not None:
        return event.trade.realized_pnl
    pos = event.position_before
    if pos is None or pos.avg_entry_price == 0:
        return None
    t = event.trade
    if pos.side == "long":
        return (t.price - pos.avg_entry_price) * pos.size
    else:
        return (pos.avg_entry_price - t.price) * pos.size


# ---------------------------------------------------------------------------
# Font loader (tries common system paths on Ubuntu + Windows)
# ---------------------------------------------------------------------------

def _load_font(size: int, bold: bool = False) -> "ImageFont.FreeTypeFont":
    if bold:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/ubuntu/Ubuntu-Bold.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
            "C:/Windows/Fonts/calibrib.ttf",
        ]
    else:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
            "C:/Windows/Fonts/arial.ttf",
            "C:/Windows/Fonts/calibri.ttf",
        ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (IOError, OSError):
            continue
    # Last resort — PIL bitmap default (small, no size control)
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Card layout constants
# ---------------------------------------------------------------------------

W, H = 920, 500

_BG          = "#0d1117"
_CARD_BG     = "#161b22"
_GREEN       = "#22c55e"
_GREEN_DIM   = "#14532d"
_RED         = "#ef4444"
_RED_DIM     = "#7f1d1d"
_TEXT_PRI    = "#f0f6fc"
_TEXT_SEC    = "#8b949e"
_TEXT_QUOTE  = "#93c5fd"
_DIVIDER     = "#21262d"


# ---------------------------------------------------------------------------
# Card generator
# ---------------------------------------------------------------------------

def generate_pnl_card(
    event: Event,
    source_name: str,
    wins: int,
    total: int,
) -> Optional[bytes]:
    """Return PNG bytes for the PnL card, or None if Pillow is unavailable."""
    if not PIL_AVAILABLE:
        return None

    pos = event.position_before
    t = event.trade
    if pos is None:
        return None

    pnl = calculate_pnl(event)
    is_win = pnl is not None and pnl > 0
    accent    = _GREEN if is_win else _RED
    accent_dim = _GREEN_DIM if is_win else _RED_DIM

    # ---- canvas ----
    img  = Image.new("RGB", (W, H), _BG)
    draw = ImageDraw.Draw(img)

    # Left accent strip
    draw.rectangle([(0, 0), (7, H)], fill=accent)

    # Card background
    draw.rounded_rectangle([(18, 14), (W - 14, H - 14)], radius=18, fill=_CARD_BG)

    # ---- fonts ----
    f_huge  = _load_font(80, bold=True)
    f_big   = _load_font(32, bold=True)
    f_med   = _load_font(24, bold=True)
    f_norm  = _load_font(20)
    f_small = _load_font(17)
    f_tiny  = _load_font(14)

    # ---- header: source · market ----
    side_emoji = "🟢" if pos.side == "long" else "🔴"
    header_txt = f"{source_name}  ·  {pos.market_symbol}"
    draw.text((42, 32), header_txt, font=f_med, fill=_TEXT_SEC)

    direction_txt = ("LONG  ·  CLOSED" if pos.side == "long" else "SHORT  ·  CLOSED")
    draw.text((42, 64), direction_txt, font=f_norm, fill=accent)

    # ---- big P&L ----
    if pnl is not None:
        sign     = "+" if pnl >= 0 else "−"
        pnl_str  = f"{sign}${abs(pnl):,.2f}"
        bbox     = draw.textbbox((0, 0), pnl_str, font=f_huge)
        tw       = bbox[2] - bbox[0]
        x_pnl    = (W - tw) // 2
        draw.text((x_pnl, 96), pnl_str, font=f_huge, fill=accent)

        # % change
        if pos.avg_entry_price and pos.avg_entry_price != 0:
            if pos.side == "long":
                pct = float((t.price - pos.avg_entry_price) / pos.avg_entry_price * 100)
            else:
                pct = float((pos.avg_entry_price - t.price) / pos.avg_entry_price * 100)
            pct_sign = "+" if pct >= 0 else ""
            pct_str  = f"{pct_sign}{pct:.2f}%"
            bbox2    = draw.textbbox((0, 0), pct_str, font=f_big)
            tw2      = bbox2[2] - bbox2[0]
            draw.text(((W - tw2) // 2, 192), pct_str, font=f_big, fill=accent)

    # ---- divider ----
    draw.line([(42, 248), (W - 42, 248)], fill=_DIVIDER, width=2)

    # ---- trade detail pills ----
    def _fmt_px(p: Decimal) -> str:
        return f"${p:,.2f}" if p >= 1000 else f"${p:,.4f}"

    details = [
        ("ENTRY",    _fmt_px(pos.avg_entry_price)),
        ("EXIT",     _fmt_px(t.price)),
        ("SIZE",     f"{pos.size:,.4f}"),
        ("NOTIONAL", f"${pos.notional_usd:,.0f}"),
    ]
    col_w = (W - 84) // len(details)
    for i, (label, value) in enumerate(details):
        x = 42 + i * col_w
        draw.text((x, 260), label, font=f_tiny, fill=_TEXT_SEC)
        draw.text((x, 278), value, font=f_small, fill=_TEXT_PRI)

    # ---- divider ----
    draw.line([(42, 316), (W - 42, 316)], fill=_DIVIDER, width=2)

    # ---- psychology quote ----
    quote = random.choice(_QUOTES_GREEN if is_win else _QUOTES_RED)
    draw.text((42, 326), "❝", font=f_small, fill=_TEXT_QUOTE)

    # Word-wrap the quote
    words   = quote.split()
    lines: list[str] = []
    line    = ""
    max_w   = W - 180
    for word in words:
        test = (line + " " + word).strip()
        bb   = draw.textbbox((0, 0), test, font=f_small)
        if bb[2] - bb[0] > max_w:
            if line:
                lines.append(line)
            line = word
        else:
            line = test
    if line:
        lines.append(line)

    y_q = 328
    for i, ln in enumerate(lines):
        draw.text((66, y_q + i * 22), ln, font=f_small, fill=_TEXT_QUOTE)

    # ---- win-rate bar ----
    y_bar = H - 54
    score_txt = f"Win rate  {wins}/{total}" if total > 0 else "Win rate  —/—"
    draw.text((42, y_bar), score_txt, font=f_small, fill=_TEXT_SEC)

    if total > 0:
        pct_w   = wins / total
        bar_x   = 220
        bar_end = W - 90
        bar_w   = bar_end - bar_x
        bar_y   = y_bar + 7
        bar_h   = 11
        draw.rounded_rectangle(
            [(bar_x, bar_y), (bar_x + bar_w, bar_y + bar_h)],
            radius=6, fill=_DIVIDER,
        )
        fill_px = int(bar_w * pct_w)
        if fill_px > 0:
            draw.rounded_rectangle(
                [(bar_x, bar_y), (bar_x + fill_px, bar_y + bar_h)],
                radius=6, fill=accent,
            )
        pct_lbl = f"{pct_w * 100:.0f}%"
        draw.text((bar_end + 8, y_bar), pct_lbl, font=f_small, fill=accent)

    # ---- watermark ----
    wm = "NK Capital"
    draw.text((W - 130, H - 28), wm, font=f_tiny, fill=_DIVIDER)

    # ---- export ----
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf.getvalue()
