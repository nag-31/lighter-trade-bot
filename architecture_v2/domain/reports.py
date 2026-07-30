from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping

from architecture_v2 import ACCOUNTING_VERSION

from .models import ZERO, Lifecycle, LifecycleStatus, PortfolioProjection, Realization


@dataclass(frozen=True, slots=True)
class MetricSlice:
    realized_pnl: Decimal
    realization_count: int
    trades_closed: int
    wins: int
    losses: int
    win_rate: Decimal | None
    closed_lifecycle_pnl: Decimal


@dataclass(frozen=True, slots=True)
class EquityPoint:
    occurred_at: datetime
    realization_uid: str
    pnl: Decimal
    cumulative_pnl: Decimal


@dataclass(frozen=True, slots=True)
class AccountingPeriodReport:
    start_at: datetime | None
    end_at: datetime | None
    timezone: str
    realized_pnl: Decimal
    gross_profit: Decimal
    gross_loss: Decimal
    realization_count: int
    trades_closed: int
    wins: int
    losses: int
    breakeven: int
    win_rate: Decimal | None
    profit_factor: Decimal | None
    closed_lifecycle_pnl: Decimal
    open_positions: int
    equity_curve: tuple[EquityPoint, ...]
    by_account: Mapping[str, MetricSlice]
    accounting_version: str
    accounting_basis: Mapping[str, str]


def _inside(
    occurred_at: datetime,
    start_at: datetime | None,
    end_at: datetime | None,
) -> bool:
    if start_at is not None and occurred_at < start_at:
        return False
    if end_at is not None and occurred_at >= end_at:
        return False
    return True


def _slice(
    realizations: list[Realization],
    lifecycles: list[Lifecycle],
) -> MetricSlice:
    realized = sum((item.net_pnl for item in realizations), ZERO)
    closed = [
        item
        for item in lifecycles
        if item.status is LifecycleStatus.CLOSED
    ]
    wins = sum(item.realized_pnl > ZERO for item in closed)
    losses = sum(item.realized_pnl < ZERO for item in closed)
    return MetricSlice(
        realized_pnl=realized,
        realization_count=len(realizations),
        trades_closed=len(closed),
        wins=wins,
        losses=losses,
        win_rate=(
            Decimal(wins) / Decimal(len(closed)) * Decimal("100")
            if closed
            else None
        ),
        closed_lifecycle_pnl=sum(
            (item.realized_pnl for item in closed), ZERO
        ),
    )


def build_period_report(
    portfolio: PortfolioProjection,
    *,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    timezone: str = "UTC",
) -> AccountingPeriodReport:
    """Build the one report contract used by every future V2 consumer."""
    if start_at is not None and (
        start_at.tzinfo is None or start_at.utcoffset() is None
    ):
        raise ValueError("start_at must be timezone-aware")
    if end_at is not None and (
        end_at.tzinfo is None or end_at.utcoffset() is None
    ):
        raise ValueError("end_at must be timezone-aware")
    if start_at is not None and end_at is not None and start_at >= end_at:
        raise ValueError("start_at must be earlier than end_at")

    period_realizations = sorted(
        (
            item
            for item in portfolio.realizations
            if _inside(item.occurred_at, start_at, end_at)
        ),
        key=lambda item: (item.occurred_at, item.realization_uid),
    )
    period_lifecycles = [
        item
        for item in portfolio.lifecycles
        if item.closed_at is not None
        and _inside(item.closed_at, start_at, end_at)
    ]
    total = _slice(period_realizations, period_lifecycles)
    lifecycle_values = [item.realized_pnl for item in period_lifecycles]
    positive = sum((value for value in lifecycle_values if value > ZERO), ZERO)
    negative = sum((-value for value in lifecycle_values if value < ZERO), ZERO)

    cumulative = ZERO
    equity: list[EquityPoint] = []
    for item in period_realizations:
        cumulative += item.net_pnl
        equity.append(
            EquityPoint(
                occurred_at=item.occurred_at,
                realization_uid=item.realization_uid,
                pnl=item.net_pnl,
                cumulative_pnl=cumulative,
            )
        )

    by_account: dict[str, MetricSlice] = {}
    for account_id in portfolio.accounts:
        by_account[account_id] = _slice(
            [
                item
                for item in period_realizations
                if item.account_id == account_id
            ],
            [
                item
                for item in period_lifecycles
                if item.account_id == account_id
            ],
        )

    return AccountingPeriodReport(
        start_at=start_at,
        end_at=end_at,
        timezone=timezone,
        realized_pnl=total.realized_pnl,
        gross_profit=sum(
            (item.net_pnl for item in period_realizations if item.net_pnl > ZERO),
            ZERO,
        ),
        gross_loss=sum(
            (-item.net_pnl for item in period_realizations if item.net_pnl < ZERO),
            ZERO,
        ),
        realization_count=total.realization_count,
        trades_closed=total.trades_closed,
        wins=total.wins,
        losses=total.losses,
        breakeven=total.trades_closed - total.wins - total.losses,
        win_rate=total.win_rate,
        profit_factor=(positive / negative if negative else None),
        closed_lifecycle_pnl=total.closed_lifecycle_pnl,
        open_positions=len(portfolio.open_positions),
        equity_curve=tuple(equity),
        by_account=MappingProxyType(by_account),
        accounting_version=ACCOUNTING_VERSION,
        accounting_basis=MappingProxyType(
            {
                "pnl": "realization_fill_time",
                "trades": "lifecycle_close_time",
            }
        ),
    )
