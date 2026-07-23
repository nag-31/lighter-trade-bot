from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal, Optional

Side = Literal["long", "short"]
PositionSide = Literal["BOTH", "LONG", "SHORT"]


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
    start_position: Optional[Decimal] = None  # HL startPosition: signed position size BEFORE this fill; None for Lighter
    # Stable v2 identity fields. Defaults preserve compatibility with legacy
    # clients/tests while adapters progressively populate native identities.
    source_id: str = ""
    exchange: str = ""
    native_trade_id: str = ""
    position_side: PositionSide = "BOTH"

    @property
    def notional_usd(self) -> Decimal:
        return self.size * self.price

    def event_uid(self, source_id: str = "") -> str:
        """Return a source- and market-scoped idempotency key for this fill."""
        sid = source_id or self.source_id or self.source
        native = self.native_trade_id or str(self.trade_id)
        return f"{sid}|{self.market_id}|{self.position_side}|{native}"


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
    source_id: str = ""
    exchange: str = ""
    position_side: PositionSide = "BOTH"
    stale: bool = False
    stale_since: Optional[datetime] = None

    @property
    def notional_usd(self) -> Decimal:
        return self.size * self.avg_entry_price

    @property
    def position_key(self) -> str:
        return f"{self.market_id}:{self.position_side}"


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
