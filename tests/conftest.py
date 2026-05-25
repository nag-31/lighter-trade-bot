"""Shared fixtures and helpers for all test modules."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.types import Position, Trade


T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def make_trade(
    *,
    trade_id: int = 1,
    market_id: int = 0,
    market_symbol: str = "BTC",
    side: str = "long",
    size: str = "1",
    price: str = "100",
    realized_pnl: str | None = None,
    source: str = "test",
) -> Trade:
    return Trade(
        trade_id=trade_id,
        timestamp=T0,
        market_id=market_id,
        market_symbol=market_symbol,
        side=side,
        size=Decimal(size),
        price=Decimal(price),
        source=source,
        realized_pnl=Decimal(realized_pnl) if realized_pnl is not None else None,
    )


def make_position(
    *,
    market_id: int = 0,
    market_symbol: str = "BTC",
    side: str = "long",
    size: str = "1",
    avg_entry_price: str = "100",
    source: str = "test",
) -> Position:
    return Position(
        market_id=market_id,
        market_symbol=market_symbol,
        side=side,
        size=Decimal(size),
        avg_entry_price=Decimal(avg_entry_price),
        source=source,
    )
