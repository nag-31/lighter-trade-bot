from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal, Optional

Side = Literal["long", "short"]


@dataclass
class OpenOrder:
    """A single resting/pending order on any exchange source."""

    source: str
    market_id: int
    market_symbol: str
    side: Side
    order_kind: str                  # "limit" | "stop_loss" | "take_profit"
    price: Optional[Decimal]         # limit price (None for pure market triggers)
    trigger_px: Optional[Decimal]    # trigger price for stop/tp
    size: Optional[Decimal]
    reduce_only: bool = False
    order_id: Optional[int] = None   # exchange order id — dedup + flash key


@dataclass(frozen=True)
class Trade:
    """A single normalised fill from any exchange source.

    `realized_pnl` is populated for HL fills that carry `closedPnl`.
    Lighter fills don't expose per-fill PnL so it stays None there.
    """

    trade_id: int
    timestamp: datetime
    market_id: int
    market_symbol: str
    side: Side
    size: Decimal
    price: Decimal
    tx_hash: str = ""
    source: str = ""
    realized_pnl: Optional[Decimal] = None  # HL closedPnl; None for Lighter
    dir: Optional[str] = None               # HL intent string e.g. "Open Long", "Close Short"; None for Lighter
    closed_pnl: Optional[Decimal] = None    # Alias for realized_pnl from HL fill; None for Lighter

    @property
    def notional_usd(self) -> Decimal:
        return self.size * self.price


@dataclass
class Position:
    market_id: int
    market_symbol: str
    side: Side
    size: Decimal
    avg_entry_price: Decimal
    source: str = ""
    unrealized_pnl: Optional[Decimal] = None   # from clearinghouseState
    liquidation_px: Optional[Decimal] = None    # from clearinghouseState

    @property
    def notional_usd(self) -> Decimal:
        return self.size * self.avg_entry_price


class EventKind(str, Enum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"
    SIZE_CHANGE = "SIZE_CHANGE"   # same-side add
    REDUCE = "REDUCE"             # opposite-side partial close (position still open)


@dataclass
class Event:
    kind: EventKind
    trade: Trade
    position_before: Optional[Position]
    position_after: Optional[Position]
    leverage: Optional[float] = None
