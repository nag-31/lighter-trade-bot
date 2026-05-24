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


def _fmt_pnl(pnl: Decimal) -> str:
    sign = "+" if pnl >= 0 else ""
    return f"{sign}${pnl:,.2f}"


def _direction_emoji(side: str) -> str:
    return "🟢 LONG" if side == "long" else "🔴 SHORT"


def _header(source_name: str) -> str:
    return f"📍 {source_name}\n" if source_name else ""


def _footer(pool_url: str) -> str:
    return f"\n{pool_url}" if pool_url else ""


def format_event(event: Event, pool_url: str, source_name: str = "") -> str:
    t = event.trade
    direction = _direction_emoji(t.side)

    if event.kind == EventKind.CLOSE and event.position_before is not None:
        pos = event.position_before
        body = (
            f"Closed {_direction_emoji(pos.side)} {pos.market_symbol}\n"
            f"Exit: {_fmt_price(t.price)}  |  Size: {_fmt_size(pos.size)}\n"
            f"Notional: ${pos.notional_usd:,.0f}"
        )
        if event.leverage is not None:
            body += f"  |  {event.leverage:g}x"
        if t.realized_pnl is not None:
            body += f"\nP&L: {_fmt_pnl(t.realized_pnl)}"

    elif event.kind == EventKind.REDUCE and event.position_before is not None and event.position_after is not None:
        pos_b = event.position_before
        pos_a = event.position_after
        body = (
            f"Reduced {_direction_emoji(pos_b.side)} {pos_b.market_symbol}\n"
            f"−{_fmt_size(t.size)} @ {_fmt_price(t.price)}\n"
            f"Remaining: ${pos_a.notional_usd:,.0f}  (was ${pos_b.notional_usd:,.0f})"
        )
        if event.leverage is not None:
            body += f"  |  {event.leverage:g}x"
        if t.realized_pnl is not None:
            body += f"\nP&L: {_fmt_pnl(t.realized_pnl)}"

    else:
        notional = t.size * t.price
        verb = {EventKind.OPEN: "Opened", EventKind.SIZE_CHANGE: "Added to"}.get(event.kind, "Updated")
        body = (
            f"{verb} {direction} {t.market_symbol}\n"
            f"Price: {_fmt_price(t.price)}  |  Size: {_fmt_size(t.size)}\n"
            f"Notional: ${notional:,.0f}"
        )
        if event.leverage is not None:
            body += f"  |  {event.leverage:g}x"

    return f"{_header(source_name)}{body}{_footer(pool_url)}"


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
    return f"{_header(source_name)}{body}{_footer(pool_url)}"


def format_reduce_aggregate(
    position: Position,
    net_reduced_usd: Decimal,
    n_fills: int,
    realized_pnl: Optional[Decimal],
    leverage: Optional[float],
    pool_url: str,
    source_name: str = "",
) -> str:
    """Message for batched REDUCE fills: N partial-close fills → remaining position."""
    direction = _direction_emoji(position.side)
    fill_word = "fill" if n_fills == 1 else "fills"
    body = (
        f"Reduced {direction} {position.market_symbol}\n"
        f"−${net_reduced_usd:,.0f} across {n_fills} {fill_word} → remaining ${position.notional_usd:,.0f}"
    )
    if leverage is not None:
        body += f"  |  {leverage:g}x"
    if realized_pnl is not None:
        body += f"\nP&L: {_fmt_pnl(realized_pnl)}"
    return f"{_header(source_name)}{body}{_footer(pool_url)}"
