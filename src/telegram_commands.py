"""Pure formatting and parsing helpers for owner-only Telegram commands."""

from __future__ import annotations

from datetime import datetime
from typing import Any


MAX_TELEGRAM_TEXT = 3900
MAX_LIST_ROWS = 25


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


def format_help() -> str:
    return "\n".join(
        [
            "Trade tracker commands",
            "",
            "/positions [source] — live open positions",
            "/orders [source] — cached open orders",
            "/trades [n] [source] — completed round trips",
            "/fills [n] [source] — recent executions/events",
            "/pnl [today|7d|30d|all] [source] — performance",
            "/stats — performance summary + card",
            "/risk [source] — exposure and concentration",
            "/sources — configured account status",
            "/health — bot component health",
            "/dashboard — dashboard link",
            "/version — deployed version and uptime",
            "/help — this message",
            "",
            "Examples: /trades 10 HL · /positions lighter · /pnl 7d",
            "Lists are capped at 25. Commands are read-only and owner-only.",
        ]
    )


def format_positions(rows: list[dict], source: str = "") -> str:
    selected = [row for row in rows if _source_matches(row, source)]
    title = f"Open positions{f' — {source}' if source else ''}"
    if not selected:
        return f"{title}\nNo matching open positions."
    lines = [f"{title} · {len(selected)}"]
    for row in selected:
        stale = " · STALE" if row.get("stale") else ""
        lines.extend(
            [
                "",
                (
                    f"{row.get('source') or row.get('source_id') or '?'} · "
                    f"{row.get('market_symbol') or '?'} "
                    f"{str(row.get('side') or '').upper()}{stale}"
                ),
                (
                    f"Size {_size(row.get('size'))} · "
                    f"Entry {_price(row.get('avg_entry_price'))} · "
                    f"Notional {_money(row.get('notional_usd'))}"
                ),
                (
                    f"uPnL {_money(row.get('unrealized_pnl'), signed=True)} · "
                    f"Liq {_price(row.get('liquidation_px'))}"
                ),
            ]
        )
    return "\n".join(lines)


def format_orders(rows: list[dict], source: str = "") -> str:
    selected = [row for row in rows if _source_matches(row, source)]
    title = f"Open orders{f' — {source}' if source else ''}"
    if not selected:
        return (
            f"{title}\nNo cached matching orders.\n"
            "Note: Lighter may reject public order reads; Binance open-order "
            "reads are not yet available."
        )
    lines = [f"{title} · {len(selected)}"]
    for row in selected:
        price = row.get("trigger_px") or row.get("price")
        lines.extend(
            [
                "",
                (
                    f"{row.get('source') or row.get('source_id') or '?'} · "
                    f"{row.get('market_symbol') or '?'} "
                    f"{str(row.get('side') or '').upper()}"
                ),
                (
                    f"{str(row.get('order_kind') or 'order').replace('_', ' ').title()} "
                    f"@ {_price(price)} · Size {_size(row.get('size'))} · "
                    f"Notional {_money(row.get('notional'))}"
                ),
            ]
        )
    return "\n".join(lines)


def format_trades(rows: list[dict], count: int, source: str = "") -> str:
    selected = [row for row in rows if _source_matches(row, source)][:count]
    title = f"Last {count} completed trades{f' — {source}' if source else ''}"
    if not selected:
        return f"{title}\nNo matching completed trades."
    lines = [title]
    for row in selected:
        entry = row.get("entry_disp", row.get("entry"))
        exit_price = row.get("exit_disp", row.get("exit"))
        lines.extend(
            [
                "",
                (
                    f"{row.get('source') or row.get('source_id') or '?'} · "
                    f"{row.get('market_symbol') or '?'} "
                    f"{str(row.get('side') or '').upper()} · "
                    f"PnL {_money(row.get('pnl'), signed=True)}"
                ),
                (
                    f"{_price(entry)} → {_price(exit_price)} · "
                    f"{row.get('ts_disp') or row.get('ts') or ''}"
                ),
            ]
        )
    return "\n".join(lines)


def format_fills(rows: list[dict], count: int, source: str = "") -> str:
    selected = [row for row in rows if _source_matches(row, source)][:count]
    title = f"Last {count} fills/events{f' — {source}' if source else ''}"
    if not selected:
        return f"{title}\nNo matching fills."
    lines = [title]
    for row in selected:
        lines.extend(
            [
                "",
                (
                    f"{row.get('source') or row.get('source_id') or '?'} · "
                    f"{row.get('market_symbol') or '?'} "
                    f"{str(row.get('side') or '').upper()} · "
                    f"{str(row.get('kind') or 'fill').upper()}"
                ),
                (
                    f"Price {_price(row.get('price'))} · "
                    f"Size {_size(row.get('size'))} · "
                    f"Notional {_money(row.get('notional'))}"
                ),
                str(row.get("ts") or ""),
            ]
        )
    return "\n".join(lines)


def format_risk(rows: list[dict], source: str = "") -> str:
    selected = [row for row in rows if _source_matches(row, source)]
    title = f"Risk snapshot{f' — {source}' if source else ''}"
    if not selected:
        return f"{title}\nNo matching open positions."
    notionals = [(_num(row.get("notional_usd")) or 0.0, row) for row in selected]
    total = sum(value for value, _row in notionals)
    long_total = sum(
        value for value, row in notionals if str(row.get("side")).lower() == "long"
    )
    short_total = total - long_total
    upnl = sum(_num(row.get("unrealized_pnl")) or 0.0 for row in selected)
    notionals.sort(key=lambda pair: pair[0], reverse=True)
    lines = [
        title,
        f"Positions: {len(selected)}",
        f"Gross exposure: {_money(total)}",
        f"Long: {_money(long_total)} · Short: {_money(short_total)}",
        f"Combined uPnL: {_money(upnl, signed=True)}",
        "",
        "Largest exposures:",
    ]
    for value, row in notionals[:5]:
        share = (value / total * 100.0) if total else 0.0
        lines.append(
            f"• {row.get('source') or '?'} {row.get('market_symbol') or '?'} "
            f"{str(row.get('side') or '').upper()} · {_money(value)} ({share:.1f}%)"
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

