"""Daily + weekly P&L recap. Skeleton — fills in once lighter_client is real.

Reads pool metadata (current equity, position list) and computes P&L over a
window. Posts via the same telegram/twitter posters used for trade events.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .lighter_client import LighterClient

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Recap:
    window: str  # "day" or "week"
    pnl_usd: float
    pnl_pct: float
    trades: int
    wins: int
    losses: int


async def compute(lighter: LighterClient, window: str) -> Recap:
    """Compute P&L over window ('day' | 'week')."""
    raise NotImplementedError(
        "Phase E: implement once lighter_client.fetch_pool_metadata + trade history are wired"
    )


def format_recap(r: Recap, pool_url: str) -> str:
    label = "Daily" if r.window == "day" else "Weekly"
    sign = "+" if r.pnl_usd >= 0 else ""
    return (
        f"{label} recap\n"
        f"P&L: {sign}${r.pnl_usd:,.0f} ({sign}{r.pnl_pct:.2f}%)\n"
        f"Trades: {r.trades}  W/L: {r.wins}/{r.losses}\n"
        f"{pool_url}"
    )
