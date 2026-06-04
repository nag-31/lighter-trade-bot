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


def _fmt_sl_tp(
    sl: Optional[Decimal],
    tp: Optional[Decimal],
    privacy=None,
    is_hl: bool = False,
    f: Optional[Decimal] = None,
    source_id: str = "",
) -> str:
    """Return '\nSL: $x  |  TP: $y' line, or '' if neither is set.

    When privacy params are provided for an HL source, prices are transformed
    before formatting.  The factor *f* must have been computed once by the
    caller and passed in so that all prices in the same message use the same
    jitter.
    """
    if privacy is not None and is_hl and f is not None:
        from .display_transform import disp_price
        sl = disp_price(privacy, is_hl, f, sl) if sl is not None else None
        tp = disp_price(privacy, is_hl, f, tp) if tp is not None else None

    parts = []
    if sl is not None:
        parts.append(f"SL: {_fmt_price(sl)}")
    if tp is not None:
        parts.append(f"TP: {_fmt_price(tp)}")
    return ("\n" + "  |  ".join(parts)) if parts else ""


def format_sl_tp_set(
    source_name: str,
    side: str,
    market_symbol: str,
    sl: Optional[Decimal],
    tp: Optional[Decimal],
    pool_url: str = "",
    privacy=None,
    is_hl: bool = False,
    anchor_entry: Optional[Decimal] = None,
    source_id: str = "",
) -> str:
    """One-time TG alert fired by the reconciler when SL/TP first becomes known.

    Layout:
        📍 {source}
        🛡 SL/TP set · 🟢 LONG {symbol}
        SL: $x  |  TP: $y
        {pool_url}
    Either sl or tp may be None; if both are None returns empty string.

    Optional privacy params transform prices for HL sources.
    """
    if sl is None and tp is None:
        return ""

    f: Optional[Decimal] = None
    if privacy is not None and is_hl and anchor_entry is not None:
        from .display_transform import price_factor
        f = price_factor(privacy, source_id, market_symbol, side, anchor_entry)

    if privacy is not None and is_hl and f is not None:
        from .display_transform import disp_price
        sl = disp_price(privacy, is_hl, f, sl) if sl is not None else None
        tp = disp_price(privacy, is_hl, f, tp) if tp is not None else None

    direction = _direction_emoji(side)
    parts = []
    if sl is not None:
        parts.append(f"SL: {_fmt_price(sl)}")
    if tp is not None:
        parts.append(f"TP: {_fmt_price(tp)}")
    sl_tp_line = "  |  ".join(parts)
    body = f"🛡 SL/TP set · {direction} {market_symbol}\n{sl_tp_line}"

    fn = ""
    if privacy is not None and is_hl:
        from .display_transform import footnote
        fn = footnote(privacy, is_hl)
    if fn:
        body += f"\n{fn}"

    return f"{_header(source_name)}{body}{_footer(pool_url)}"


def format_event(
    event: Event,
    pool_url: str,
    source_name: str = "",
    sl: Optional[Decimal] = None,
    tp: Optional[Decimal] = None,
    privacy=None,
    is_hl: bool = False,
    anchor_entry: Optional[Decimal] = None,
    source_id: str = "",
) -> str:
    t = event.trade
    direction = _direction_emoji(t.side)

    # Compute ONE jitter factor for all price fields in this message
    f: Optional[Decimal] = None
    if privacy is not None and is_hl and anchor_entry is not None:
        from .display_transform import price_factor
        f = price_factor(privacy, source_id, t.market_symbol, t.side, anchor_entry)

    def _dp(price: Decimal) -> Decimal:
        if privacy is not None and is_hl and f is not None:
            from .display_transform import disp_price
            return disp_price(privacy, is_hl, f, price)
        return price

    def _ds(size: Decimal) -> Decimal:
        if privacy is not None and is_hl:
            from .display_transform import disp_size
            return disp_size(privacy, is_hl, size)
        return size

    def _dn(notional: Decimal) -> Decimal:
        if privacy is not None and is_hl:
            from .display_transform import disp_notional
            return disp_notional(privacy, is_hl, notional)
        return notional

    if event.kind == EventKind.CLOSE and event.position_before is not None:
        pos = event.position_before
        disp_exit = _dp(t.price)
        disp_not  = _dn(pos.notional_usd)
        body = (
            f"Closed {_direction_emoji(pos.side)} {pos.market_symbol}\n"
            f"Exit: {_fmt_price(disp_exit)}  |  Notional: ${disp_not:,.0f}"
        )
        if event.leverage is not None:
            body += f"  |  {event.leverage:g}x"
        if t.realized_pnl is not None:
            body += f"\nP&L: {_fmt_pnl(t.realized_pnl)}"
        # No SL/TP on CLOSE — orders are cancelled when position closes

    elif event.kind == EventKind.REDUCE and event.position_before is not None and event.position_after is not None:
        pos_b = event.position_before
        pos_a = event.position_after
        disp_trade_price  = _dp(t.price)
        disp_not_a        = _dn(pos_a.notional_usd)
        disp_not_b        = _dn(pos_b.notional_usd)
        fill_notional     = _dn(t.size * t.price)
        body = (
            f"Reduced {_direction_emoji(pos_b.side)} {pos_b.market_symbol}\n"
            f"−${fill_notional:,.0f} @ {_fmt_price(disp_trade_price)}\n"
            f"Remaining: ${disp_not_a:,.0f}  (was ${disp_not_b:,.0f})"
        )
        if event.leverage is not None:
            body += f"  |  {event.leverage:g}x"
        if t.realized_pnl is not None:
            body += f"\nP&L: {_fmt_pnl(t.realized_pnl)}"
        body += _fmt_sl_tp(sl, tp, privacy=privacy, is_hl=is_hl, f=f, source_id=source_id)

    else:
        notional = t.size * t.price
        disp_trade_price = _dp(t.price)
        disp_not         = _dn(notional)
        verb = {EventKind.OPEN: "Opened", EventKind.SIZE_CHANGE: "Added to"}.get(event.kind, "Updated")
        body = (
            f"{verb} {direction} {t.market_symbol}\n"
            f"Price: {_fmt_price(disp_trade_price)}  |  Notional: ${disp_not:,.0f}"
        )
        if event.leverage is not None:
            body += f"  |  {event.leverage:g}x"
        body += _fmt_sl_tp(sl, tp, privacy=privacy, is_hl=is_hl, f=f, source_id=source_id)

    fn = ""
    if privacy is not None and is_hl:
        from .display_transform import footnote
        fn = footnote(privacy, is_hl)
    if fn:
        body += f"\n{fn}"

    return f"{_header(source_name)}{body}{_footer(pool_url)}"


def format_aggregate(
    position: Position,
    net_added_usd: Decimal,
    n_fills: int,
    leverage: Optional[float],
    pool_url: str,
    source_name: str = "",
    sl: Optional[Decimal] = None,
    tp: Optional[Decimal] = None,
    privacy=None,
    is_hl: bool = False,
    anchor_entry: Optional[Decimal] = None,
    source_id: str = "",
) -> str:
    """Message for a batched SIZE_CHANGE: N fills → resulting position."""
    direction = _direction_emoji(position.side)
    fill_word = "fill" if n_fills == 1 else "fills"

    # Compute jitter factor once
    f: Optional[Decimal] = None
    if privacy is not None and is_hl and anchor_entry is not None:
        from .display_transform import price_factor
        f = price_factor(privacy, source_id, position.market_symbol, position.side, anchor_entry)

    # Transform displayed values
    disp_avg_entry = position.avg_entry_price
    disp_not       = position.notional_usd
    disp_net_added = net_added_usd

    if privacy is not None and is_hl and f is not None:
        from .display_transform import disp_price, disp_notional
        disp_avg_entry = disp_price(privacy, is_hl, f, position.avg_entry_price)
        disp_not       = disp_notional(privacy, is_hl, position.notional_usd)
        disp_net_added = disp_notional(privacy, is_hl, net_added_usd)

    body = (
        f"Added {direction} {position.market_symbol}\n"
        f"+${disp_net_added:,.0f} across {n_fills} {fill_word} → position now ${disp_not:,.0f}\n"
        f"Avg entry: {_fmt_price(disp_avg_entry)}"
    )
    if leverage is not None:
        body += f"  |  {leverage:g}x"
    body += _fmt_sl_tp(sl, tp, privacy=privacy, is_hl=is_hl, f=f, source_id=source_id)

    fn = ""
    if privacy is not None and is_hl:
        from .display_transform import footnote
        fn = footnote(privacy, is_hl)
    if fn:
        body += f"\n{fn}"

    return f"{_header(source_name)}{body}{_footer(pool_url)}"


def format_reduce_aggregate(
    position: Position,
    net_reduced_usd: Decimal,
    n_fills: int,
    realized_pnl: Optional[Decimal],
    leverage: Optional[float],
    pool_url: str,
    source_name: str = "",
    sl: Optional[Decimal] = None,
    tp: Optional[Decimal] = None,
    privacy=None,
    is_hl: bool = False,
    anchor_entry: Optional[Decimal] = None,
    source_id: str = "",
) -> str:
    """Message for batched REDUCE fills: N partial-close fills → remaining position."""
    direction = _direction_emoji(position.side)
    fill_word = "fill" if n_fills == 1 else "fills"

    # Compute jitter factor once
    f: Optional[Decimal] = None
    if privacy is not None and is_hl and anchor_entry is not None:
        from .display_transform import price_factor
        f = price_factor(privacy, source_id, position.market_symbol, position.side, anchor_entry)

    # Transform displayed values (PnL stays EXACT)
    disp_not        = position.notional_usd
    disp_net_reduced = net_reduced_usd

    if privacy is not None and is_hl:
        from .display_transform import disp_notional
        disp_not         = disp_notional(privacy, is_hl, position.notional_usd)
        disp_net_reduced = disp_notional(privacy, is_hl, net_reduced_usd)

    body = (
        f"Reduced {direction} {position.market_symbol}\n"
        f"−${disp_net_reduced:,.0f} across {n_fills} {fill_word} → remaining ${disp_not:,.0f}"
    )
    if leverage is not None:
        body += f"  |  {leverage:g}x"
    if realized_pnl is not None:
        body += f"\nP&L: {_fmt_pnl(realized_pnl)}"
    body += _fmt_sl_tp(sl, tp, privacy=privacy, is_hl=is_hl, f=f, source_id=source_id)

    fn = ""
    if privacy is not None and is_hl:
        from .display_transform import footnote
        fn = footnote(privacy, is_hl)
    if fn:
        body += f"\n{fn}"

    return f"{_header(source_name)}{body}{_footer(pool_url)}"
