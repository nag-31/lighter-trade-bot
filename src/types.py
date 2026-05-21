from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal, Optional

Side = Literal["long", "short"]


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
