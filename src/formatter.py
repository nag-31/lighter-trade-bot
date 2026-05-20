from decimal import Decimal
from typing import Optional

from .types import Event, EventKind, Position


def _fmt_price(p: Decimal) -> str:
    if p >= 1000:
        return f"${p:,.2f}"
    if p >= 1:
        return f"${p:,.4f}"
    return f"${p:,.6f}"


def _fmt_size(s: Decimal) -> str:
    if s >= 1:
        return f"{s:,.4f}"
    return f"{s:,.6f}"


def _verb(kind: EventKind) -> str:
    return {EventKind.OPEN: "Opened", EventKind.CLOSE: "Closed", EventKind.SIZE_CHANGE: "Updated"}[kind]


def _direction_emoji(side: str) -> str:
    return "🟢 LONG" if side == "long" else "🔴 SHORT"


def _header(source_name: str) -> str:
    """Leading label line so each alert identifies which pool/wallet it's from."""
    return f"📍 {source_name}\n" if source_name else ""


def format_event(event: Event, pool_url: str, source_name: str = "") -> str:
    t = event.trade
    direction = _direction_emoji(t.side)
    verb = _verb(event.kind)

    if event.kind == EventKind.CLOSE and event.position_before is not None:
        pos = event.position_before
        notional_str = f"${pos.notional_usd:,.0f}"
        body = (
            f"{verb} {direction} {pos.market_symbol}\n"
            f"Exit: {_fmt_price(t.price)}  |  Size: {_fmt_size(pos.size)}\n"
            f"Notional: {notional_str}"
        )
    else:
        notional = t.size * t.price
        notional_str = f"${notional:,.0f}"
        body = (
            f"{verb} {direction} {t.market_symbol}\n"
            f"Price: {_fmt_price(t.price)}  |  Size: {_fmt_size(t.size)}\n"
            f"Notional: {notional_str}"
        )

    if event.leverage is not None:
        body += f"  |  {event.leverage:g}x"

    return f"{_header(source_name)}{body}\n{pool_url}"


def format_aggregate(
    position: Position,
    net_added_usd: Decimal,
    n_fills: int,
    leverage: Optional[float],
    pool_url: str,
    source_name: str = "",
) -> str:
    """Message for a batched SIZE_CHANGE: N fills → resulting position."""
    direction = _direction_emoji(position.side)
    fill_word = "fill" if n_fills == 1 else "fills"
    body = (
        f"Added {direction} {position.market_symbol}\n"
        f"+${net_added_usd:,.0f} across {n_fills} {fill_word} → position now ${position.notional_usd:,.0f}\n"
        f"Avg entry: {_fmt_price(position.avg_entry_price)}"
    )
    if leverage is not None:
        body += f"  |  {leverage:g}x"
    return f"{_header(source_name)}{body}\n{pool_url}"
