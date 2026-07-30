from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Mapping

from .identity import require_identity, stable_uid


ZERO = Decimal("0")


class ExecutionSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class PositionSide(str, Enum):
    BOTH = "BOTH"
    LONG = "LONG"
    SHORT = "SHORT"


class PositionDirection(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class LifecycleStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


def aware_utc(value: datetime, field: str = "occurred_at") -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def positive(value: Decimal, field: str) -> Decimal:
    decimal = Decimal(value)
    if not decimal.is_finite() or decimal <= ZERO:
        raise ValueError(f"{field} must be a positive finite decimal")
    return decimal


def non_negative(value: Decimal, field: str) -> Decimal:
    decimal = Decimal(value)
    if not decimal.is_finite() or decimal < ZERO:
        raise ValueError(f"{field} must be a non-negative finite decimal")
    return decimal


@dataclass(frozen=True, slots=True)
class Execution:
    execution_uid: str
    account_id: str
    exchange: str
    market_key: str
    position_side: PositionSide
    native_trade_id: str
    occurred_at: datetime
    side: ExecutionSide
    quantity: Decimal
    price: Decimal
    fee: Decimal = ZERO

    @classmethod
    def create(
        cls,
        *,
        account_id: str,
        exchange: str,
        market_key: str,
        position_side: PositionSide,
        native_trade_id: str,
        occurred_at: datetime,
        side: ExecutionSide,
        quantity: Decimal,
        price: Decimal,
        fee: Decimal = ZERO,
    ) -> "Execution":
        account = require_identity(account_id, "account_id")
        venue = require_identity(exchange, "exchange").lower()
        market = require_identity(market_key, "market_key")
        native = require_identity(native_trade_id, "native_trade_id")
        normalized_position_side = PositionSide(position_side)
        normalized_side = ExecutionSide(side)
        return cls(
            execution_uid=stable_uid(
                "execution",
                account,
                venue,
                market,
                normalized_position_side.value,
                native,
            ),
            account_id=account,
            exchange=venue,
            market_key=market,
            position_side=normalized_position_side,
            native_trade_id=native,
            occurred_at=aware_utc(occurred_at),
            side=normalized_side,
            quantity=positive(quantity, "quantity"),
            price=positive(price, "price"),
            fee=non_negative(fee, "fee"),
        )

    @property
    def position_key(self) -> str:
        return (
            f"{self.account_id}:{self.market_key}:{self.position_side.value}"
        )


@dataclass(frozen=True, slots=True)
class Realization:
    realization_uid: str
    execution_uid: str
    lifecycle_uid: str
    account_id: str
    market_key: str
    position_side: PositionSide
    direction: PositionDirection
    occurred_at: datetime
    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    gross_pnl: Decimal
    fees: Decimal
    funding: Decimal
    net_pnl: Decimal
    kind: str


@dataclass(frozen=True, slots=True)
class Lifecycle:
    lifecycle_uid: str
    account_id: str
    position_key: str
    market_key: str
    position_side: PositionSide
    direction: PositionDirection
    opened_at: datetime
    closed_at: datetime | None
    status: LifecycleStatus
    entry_vwap: Decimal
    exit_vwap: Decimal | None
    max_quantity: Decimal
    closed_quantity: Decimal
    gross_pnl: Decimal
    fees: Decimal
    funding: Decimal
    realized_pnl: Decimal
    execution_uids: tuple[str, ...]
    realization_uids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PositionState:
    account_id: str
    position_key: str
    market_key: str
    position_side: PositionSide
    direction: PositionDirection
    quantity: Decimal
    average_entry_price: Decimal
    entry_fee_basis: Decimal
    lifecycle_uid: str
    opened_at: datetime


@dataclass(frozen=True, slots=True)
class AccountProjection:
    account_id: str
    executions: tuple[Execution, ...]
    realizations: tuple[Realization, ...]
    lifecycles: tuple[Lifecycle, ...]
    open_positions: tuple[PositionState, ...]

    @property
    def execution_count(self) -> int:
        return len(self.executions)


@dataclass(frozen=True, slots=True)
class PortfolioProjection:
    accounts: Mapping[str, AccountProjection]
    executions: tuple[Execution, ...]
    realizations: tuple[Realization, ...]
    lifecycles: tuple[Lifecycle, ...]
    open_positions: tuple[PositionState, ...]
