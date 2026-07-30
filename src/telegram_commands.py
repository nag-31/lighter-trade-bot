"""Pure formatting and parsing helpers for Telegram community commands."""

from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Any


MAX_TELEGRAM_TEXT = 3900
MAX_LIST_ROWS = 25

COMMUNITY_COMMANDS = frozenset(
    {
        "start", "help", "commands", "about", "status", "positions",
        "trades", "latest", "pnl", "today", "weekly", "stats",
        "performance", "coin", "leaderboard",
    }
)
OWNER_COMMANDS = frozenset(
    {
        "orders", "fills", "risk", "sources", "health", "dashboard",
        "version",
    }
)


def command_output_chat(
    message: dict,
    *,
    owner_id: int | None,
    discussion_chat_id: int | None,
    channel_id: str = "",
) -> str | None:
    """Return the safe output chat for an authorized command message.

    Any real user can use commands in a private chat with the bot or in the one
    configured discussion group. Replies stay in the originating chat and are
    never redirected into the broadcast channel. Other groups and anonymous
    administrators are ignored.
    """
    sender_id = (message.get("from") or {}).get("id")
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    chat_type = str(chat.get("type") or "")
    if sender_id is None:
        return None
    if chat_type == "private" and chat_id == sender_id:
        return str(chat_id)
    if (
        discussion_chat_id is not None
        and chat_type in {"group", "supergroup"}
        and chat_id == discussion_chat_id
    ):
        return str(chat_id)
    return None


def command_is_allowed(command: str, *, is_owner: bool) -> bool:
    """Return whether a parsed command is available to this sender."""
    value = str(command or "").lower()
    return value in COMMUNITY_COMMANDS or (is_owner and value in OWNER_COMMANDS)


def parse_command(text: str) -> tuple[str, list[str]] | None:
    """Parse `/command args` and tolerate Telegram's `/command@bot` form."""
    value = str(text or "").strip()
    if not value.startswith("/"):
        return None
    parts = value.split()
    command = parts[0][1:].split("@", 1)[0].lower()
    if not command:
        return None
    return command, parts[1:]


def parse_count_and_source(
    args: list[str], default: int = 10
) -> tuple[int, str]:
    count = default
    rest = list(args)
    if rest:
        try:
            count = int(rest[0])
            rest = rest[1:]
        except ValueError:
            pass
    count = max(1, min(MAX_LIST_ROWS, count))
    return count, " ".join(rest).strip()


def split_message(text: str, limit: int = MAX_TELEGRAM_TEXT) -> list[str]:
    """Split long Telegram replies on line boundaries."""
    value = str(text or "")
    if len(value) <= limit:
        return [value]
    chunks: list[str] = []
    current = ""
    for line in value.splitlines():
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current = line
    if current:
        chunks.append(current)
    return chunks or [value[:limit]]


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _money(value: Any, *, signed: bool = False) -> str:
    number = _num(value)
    if number is None:
        return "—"
    sign = "+" if signed and number >= 0 else ""
    return f"{sign}${number:,.2f}"


def _price(value: Any) -> str:
    number = _num(value)
    if number is None:
        return "—"
    if abs(number) >= 1000:
        return f"${number:,.2f}"
    if abs(number) >= 1:
        return f"${number:,.4f}".rstrip("0").rstrip(".")
    return f"${number:,.8f}".rstrip("0").rstrip(".")


def _size(value: Any) -> str:
    number = _num(value)
    if number is None:
        return "—"
    return f"{number:,.8f}".rstrip("0").rstrip(".")


def _text(value: Any, fallback: str = "?") -> str:
    return escape(str(value or fallback))


def _side(value: Any) -> str:
    side = str(value or "").lower()
    if side == "long":
        return "🟢 <b>LONG</b>"
    if side == "short":
        return "🔴 <b>SHORT</b>"
    return "⚪ <b>UNKNOWN</b>"


def _pnl(value: Any, label: str = "P&amp;L") -> str:
    number = _num(value)
    marker = "⚪" if number is None else ("🟢" if number >= 0 else "🔴")
    return f"{marker} {label}: <b>{_money(value, signed=True)}</b>"


def _source_matches(row: dict, query: str) -> bool:
    if not query:
        return True
    needle = query.lower().strip()
    values = (
        row.get("source"),
        row.get("source_id"),
        row.get("exchange"),
        row.get("name"),
        row.get("id"),
    )
    return any(needle in str(value or "").lower() for value in values)


def format_help(*, owner: bool = False) -> str:
    lines = [
        "Crypto Scientist tracker commands",
        "",
        "/positions [source] — live positions",
        "/trades [n] [source] — completed trades",
        "/latest [n] — latest completed trades",
        "/pnl [today|7d|30d|all] [source] — performance",
        "/today — today's performance",
        "/weekly — seven-day performance",
        "/stats — full performance summary + card",
        "/coin BTC [today|7d|30d|all] — coin performance",
        "/leaderboard [today|7d|30d|all] — top coins",
        "/status — public bot status",
        "/about — what the tracker reports",
        "/help — this message",
    ]
    if owner:
        lines.extend(
            [
                "",
                "Owner tools",
                "/orders [source] — cached open orders",
                "/fills [n] [source] — recent executions/events",
                "/risk [source] — exposure and concentration",
                "/sources — configured account status",
                "/health — component diagnostics",
                "/dashboard — private dashboard link",
                "/version — deployed version and uptime",
            ]
        )
    lines.extend(
        [
            "",
            "Examples: /coin BTC 7d · /trades 5 HL · /positions lighter",
            "Lists are capped at 25. Prices and sizes may be privacy-adjusted.",
        ]
    )
    return "\n".join(lines)


def format_about() -> str:
    return "\n".join(
        [
            "Crypto Scientist trade tracker",
            "",
            "Read-only updates for live positions, completed trades and performance.",
            "Multiple fills from one position lifecycle are grouped into one trade "
            "when the position closes.",
            "Exact realized P&L is retained; selected prices, sizes and times may "
            "be privacy-adjusted.",
            "Nothing sent to this bot can place, modify or close a trade.",
        ]
    )


def format_public_status(
    *,
    ready: bool,
    active_sources: int,
    open_positions: int,
    updated_at: str,
) -> str:
    state = "ONLINE" if ready else "DEGRADED"
    return "\n".join(
        [
            f"Tracker status — {state}",
            f"Sources: {active_sources} · Open positions: {open_positions}",
            f"Updated: {updated_at}",
            "Commands are read-only.",
        ]
    )


def format_leaderboard(stats: dict, window: str) -> str:
    rows = list(stats.get("by_symbol") or [])
    if not rows:
        return f"Coin leaderboard — {window}\nNo completed trades."
    lines = [f"Coin leaderboard — {window}"]
    for index, row in enumerate(rows[:10], start=1):
        lines.append(
            f"{index}. {row.get('symbol') or '?'} · "
            f"{_money(row.get('pnl'), signed=True)} · "
            f"{int(row.get('n') or row.get('trades') or 0)} trades"
        )
    return "\n".join(lines)


def format_positions(rows: list[dict], source: str = "") -> str:
    selected = [row for row in rows if _source_matches(row, source)]
    source_title = f" · {_text(source)}" if source else ""
    title = f"OPEN POSITIONS{source_title}"
    if not selected:
        return f"📊 <b>{title}</b>\nNo matching open positions."
    lines = [f"📊 <b>{title} · {len(selected)}</b>"]
    for row in selected:
        stale = "  ·  ⚠️ <b>STALE</b>" if row.get("stale") else ""
        source_name = _text(row.get("source") or row.get("source_id"))
        symbol = _text(row.get("market_symbol"))
        lines.extend(
            [
                "",
                f"📍 <b>{symbol}</b>  ·  {source_name}",
                f"{_side(row.get('side'))}{stale}",
                (
                    f"💼 Position: <b>{_money(row.get('notional_usd'))}</b>"
                    f"  ·  Size: <b>{_size(row.get('size'))}</b>"
                ),
                (
                    f"🎯 Entry: <b>{_price(row.get('avg_entry_price'))}</b>"
                    f"  ·  ⚠️ Liq: <b>{_price(row.get('liquidation_px'))}</b>"
                ),
                _pnl(row.get("unrealized_pnl"), "uPnL"),
            ]
        )
    return "\n".join(lines)


def format_orders(rows: list[dict], source: str = "") -> str:
    selected = [row for row in rows if _source_matches(row, source)]
    title = f"OPEN ORDERS{f' · {_text(source)}' if source else ''}"
    if not selected:
        return (
            f"📋 <b>{title}</b>\nNo cached matching orders.\n"
            "Note: Lighter may reject public order reads; Binance open-order "
            "reads are not yet available."
        )
    lines = [f"📋 <b>{title} · {len(selected)}</b>"]
    for row in selected:
        price = row.get("trigger_px") or row.get("price")
        lines.extend(
            [
                "",
                (
                    f"📍 <b>{_text(row.get('market_symbol'))}</b> · "
                    f"{_text(row.get('source') or row.get('source_id'))}"
                ),
                _side(row.get("side")),
                (
                    f"🧾 <b>{_text(str(row.get('order_kind') or 'order').replace('_', ' ').title())}</b>"
                    f" @ <b>{_price(price)}</b>\n"
                    f"💼 Position: <b>{_money(row.get('notional'))}</b>"
                    f" · Size: <b>{_size(row.get('size'))}</b>"
                ),
            ]
        )
    return "\n".join(lines)


def format_trades(rows: list[dict], count: int, source: str = "") -> str:
    selected = [row for row in rows if _source_matches(row, source)][:count]
    title = f"LAST {count} COMPLETED TRADES{f' · {_text(source)}' if source else ''}"
    if not selected:
        return f"📒 <b>{title}</b>\nNo matching completed trades."
    lines = [f"📒 <b>{title}</b>"]
    for row in selected:
        entry = row.get("entry_disp", row.get("entry"))
        exit_price = row.get("exit_disp", row.get("exit"))
        lines.extend(
            [
                "",
                (
                    f"📍 <b>{_text(row.get('market_symbol'))}</b> · "
                    f"{_text(row.get('source') or row.get('source_id'))}"
                ),
                f"{_side(row.get('side'))}  ·  {_pnl(row.get('pnl'))}",
                (
                    f"🎯 <b>{_price(entry)}</b> → <b>{_price(exit_price)}</b>"
                    f"  ·  {_text(row.get('ts_disp') or row.get('ts'), '')}"
                ),
            ]
        )
    return "\n".join(lines)


def format_fills(rows: list[dict], count: int, source: str = "") -> str:
    selected = [row for row in rows if _source_matches(row, source)][:count]
    title = f"LAST {count} FILLS/EVENTS{f' · {_text(source)}' if source else ''}"
    if not selected:
        return f"🧾 <b>{title}</b>\nNo matching fills."
    lines = [f"🧾 <b>{title}</b>"]
    for row in selected:
        lines.extend(
            [
                "",
                (
                    f"📍 <b>{_text(row.get('market_symbol'))}</b> · "
                    f"{_text(row.get('source') or row.get('source_id'))}"
                ),
                (
                    f"{_side(row.get('side'))} · "
                    f"<b>{_text(str(row.get('kind') or 'fill').upper())}</b>"
                ),
                (
                    f"🎯 Price: <b>{_price(row.get('price'))}</b>"
                    f" · Size: <b>{_size(row.get('size'))}</b>\n"
                    f"💼 Position: <b>{_money(row.get('notional'))}</b>"
                ),
                _text(row.get("ts"), ""),
            ]
        )
    return "\n".join(lines)


def format_risk(rows: list[dict], source: str = "") -> str:
    selected = [row for row in rows if _source_matches(row, source)]
    title = f"RISK SNAPSHOT{f' · {_text(source)}' if source else ''}"
    if not selected:
        return f"🛡 <b>{title}</b>\nNo matching open positions."
    notionals = [(_num(row.get("notional_usd")) or 0.0, row) for row in selected]
    total = sum(value for value, _row in notionals)
    long_total = sum(
        value for value, row in notionals if str(row.get("side")).lower() == "long"
    )
    short_total = total - long_total
    upnl = sum(_num(row.get("unrealized_pnl")) or 0.0 for row in selected)
    notionals.sort(key=lambda pair: pair[0], reverse=True)
    lines = [
        f"🛡 <b>{title}</b>",
        f"Positions: <b>{len(selected)}</b>",
        f"💼 Gross exposure: <b>{_money(total)}</b>",
        f"🟢 Long: <b>{_money(long_total)}</b> · 🔴 Short: <b>{_money(short_total)}</b>",
        _pnl(upnl, "Combined uPnL"),
        "",
        "Largest exposures:",
    ]
    for value, row in notionals[:5]:
        share = (value / total * 100.0) if total else 0.0
        lines.append(
            f"• {_text(row.get('source'))} <b>{_text(row.get('market_symbol'))}</b> "
            f"{_side(row.get('side'))} · <b>{_money(value)}</b> ({share:.1f}%)"
        )
    if any(row.get("stale") for row in selected):
        lines.append("Warning: one or more snapshots are stale.")
    return "\n".join(lines)


def format_sources(details: list[dict], health: dict) -> str:
    status_by_component = {
        row.get("component"): row for row in health.get("components", [])
    }
    lines = [f"Sources · {len(details)} active"]
    for source in details:
        component = status_by_component.get(f"source:{source.get('id')}", {})
        status = str(component.get("status") or "unknown").upper()
        lines.append(
            f"• {source.get('name') or source.get('id')} "
            f"[{source.get('exchange') or '?'}] — {status}"
        )
    disabled = [
        row
        for row in health.get("components", [])
        if str(row.get("component", "")).startswith("source:")
        and row.get("status") == "disabled"
    ]
    for row in disabled:
        lines.append(
            f"• {row.get('component', '').removeprefix('source:')} — DISABLED "
            f"({row.get('detail') or 'not configured'})"
        )
    return "\n".join(lines)


def format_health(health: dict) -> str:
    state = "READY" if health.get("ready") else "DEGRADED"
    components = health.get("components", [])
    problems = [
        row for row in components if row.get("status") in {"down", "degraded"}
    ]
    lines = [
        f"Bot health — {state}",
        f"Components: {len(components)} · Problems: {len(problems)}",
        f"Started: {health.get('started_at') or 'unknown'}",
    ]
    for row in problems[:10]:
        detail = row.get("error") or row.get("detail") or "unknown error"
        lines.append(f"• {row.get('component')} — {str(row.get('status')).upper()}: {detail}")
    return "\n".join(lines)


def filter_rows_by_source(rows: list[dict], source: str) -> list[dict]:
    return [row for row in rows if _source_matches(row, source)]
