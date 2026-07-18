from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Optional


Side = Literal["buy", "sell"]
Direction = Literal["long", "short"]
FundingStatus = Literal["complete", "unknown"]


ZERO = Decimal("0")


@dataclass(frozen=True)
class RawFill:
    source: str
    account: str
    symbol: str
    fill_id: str
    timestamp: datetime
    side: Side
    qty: Decimal
    price: Decimal
    order_id: Optional[str] = None
    fee: Decimal = ZERO
    fee_token: str = "USDC"
    exchange_realized_pnl: Optional[Decimal] = None
    funding: Optional[Decimal] = None
    sequence: Optional[int] = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def signed_qty(self) -> Decimal:
        return self.qty if self.side == "buy" else -self.qty

    @property
    def notional(self) -> Decimal:
        return self.qty * self.price


@dataclass
class PositionState:
    source: str
    account: str
    symbol: str
    direction: Direction
    qty: Decimal
    avg_entry: Decimal
    open_fees_unallocated: Decimal
    opened_at: datetime
    fill_ids: list[str] = field(default_factory=list)

    @property
    def signed_qty(self) -> Decimal:
        return self.qty if self.direction == "long" else -self.qty


@dataclass(frozen=True)
class Realization:
    source: str
    account: str
    symbol: str
    fill_id: str
    timestamp: datetime
    direction: Direction
    closed_qty: Decimal
    entry_price: Decimal
    exit_price: Decimal
    gross_pnl: Decimal
    exchange_realized_pnl: Optional[Decimal]
    allocated_open_fee: Decimal
    close_fee: Decimal
    funding: Decimal
    funding_status: FundingStatus
    net_pnl: Decimal

    @property
    def cost_basis(self) -> Decimal:
        return self.entry_price * self.closed_qty

    @property
    def fees(self) -> Decimal:
        return self.allocated_open_fee + self.close_fee


@dataclass(frozen=True)
class RoundTrip:
    id: str
    source: str
    account: str
    symbol: str
    direction: Direction
    opened_at: datetime
    closed_at: datetime
    entry_fill_ids: tuple[str, ...]
    exit_fill_ids: tuple[str, ...]
    realizations: tuple[Realization, ...]

    @property
    def closed_qty(self) -> Decimal:
        return sum((r.closed_qty for r in self.realizations), ZERO)

    @property
    def cost_basis(self) -> Decimal:
        return sum((r.cost_basis for r in self.realizations), ZERO)

    @property
    def avg_entry(self) -> Optional[Decimal]:
        qty = self.closed_qty
        return self.cost_basis / qty if qty else None

    @property
    def avg_exit(self) -> Optional[Decimal]:
        qty = self.closed_qty
        if not qty:
            return None
        exit_notional = sum((r.exit_price * r.closed_qty for r in self.realizations), ZERO)
        return exit_notional / qty

    @property
    def gross_pnl(self) -> Decimal:
        return sum((r.gross_pnl for r in self.realizations), ZERO)

    @property
    def net_pnl(self) -> Decimal:
        return sum((r.net_pnl for r in self.realizations), ZERO)

    @property
    def fees(self) -> Decimal:
        return sum((r.fees for r in self.realizations), ZERO)

    @property
    def funding(self) -> Decimal:
        return sum((r.funding for r in self.realizations), ZERO)

    @property
    def funding_status(self) -> FundingStatus:
        return "unknown" if any(r.funding_status == "unknown" for r in self.realizations) else "complete"

    @property
    def return_on_cost(self) -> Optional[Decimal]:
        return (self.net_pnl / self.cost_basis * Decimal("100")) if self.cost_basis else None

    @property
    def is_win(self) -> bool:
        return self.net_pnl > ZERO


@dataclass(frozen=True)
class OpenPosition:
    source: str
    account: str
    symbol: str
    direction: Direction
    qty: Decimal
    avg_entry: Decimal
    opened_at: datetime
    open_fees_unallocated: Decimal
    fill_ids: tuple[str, ...]

    @property
    def cost_basis(self) -> Decimal:
        return self.qty * self.avg_entry

