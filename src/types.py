from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal, Optional

Side = Literal["long", "short"]


@dataclass(frozen=True)
class Trade:
    """A single trade on the pool, normalized from Lighter's REST/WS schema.

    Lighter's trade record has no native `side` field — it's derived by comparing
    the pool's account_index with ask_account_id / bid_account_id. Leverage and
    reduce_only flags are NOT on the trade record; leverage is fetched from the
    position snapshot at event time.
    """

    trade_id: int
    timestamp: datetime
    market_id: int
    market_symbol: str  # resolved from order_book metadata at startup
    side: Side
    size: Decimal
    price: Decimal
    tx_hash: str = ""
    source: str = ""  # name of the tracked pool/wallet this trade belongs to

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
    source: str = ""  # name of the tracked pool/wallet this position belongs to

    @property
    def notional_usd(self) -> Decimal:
        return self.size * self.avg_entry_price


class EventKind(str, Enum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"
    SIZE_CHANGE = "SIZE_CHANGE"


@dataclass
class Event:
    kind: EventKind
    trade: Trade
    position_before: Optional[Position]
    position_after: Optional[Position]
    # Filled in by orchestrator from position snapshot at trade time. May stay
    # None if the snapshot fetch failed — formatter will omit leverage line.
    leverage: Optional[float] = None
