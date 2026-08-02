"""Canonical lifecycle holding-time calculations and display formatting."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def holding_duration_ms(opened_at: Any, closed_at: Any | None) -> int | None:
    """Return a non-negative exact duration for a closed lifecycle."""
    if not opened_at or not closed_at:
        return None
    return max(0, int(round((parse_timestamp(closed_at) - parse_timestamp(opened_at)).total_seconds() * 1000)))


def format_holding_duration(duration_ms: int | float | None) -> str:
    if duration_ms is None:
        return "—"
    seconds = max(0, int(round(float(duration_ms) / 1000)))
    if seconds < 60:
        return "<1m" if seconds == 0 else f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m" if seconds == 0 else f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h" if minutes == 0 else f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d" if hours == 0 else f"{days}d {hours}h"
