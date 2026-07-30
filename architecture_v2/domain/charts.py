from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from .identity import stable_uid
from .models import (
    ZERO,
    AccountProjection,
    Execution,
    ExecutionSide,
    Lifecycle,
    PositionDirection,
    Realization,
    aware_utc,
    non_negative,
    positive,
)


class MarkerAction(str, Enum):
    OPEN_LONG = "OPEN_LONG"
    ADD_LONG = "ADD_LONG"
    PARTIAL_EXIT_LONG = "PARTIAL_EXIT_LONG"
    CLOSE_LONG = "CLOSE_LONG"
    REVERSAL_CLOSE_LONG = "REVERSAL_CLOSE_LONG"
    OPEN_SHORT = "OPEN_SHORT"
    ADD_SHORT = "ADD_SHORT"
    PARTIAL_EXIT_SHORT = "PARTIAL_EXIT_SHORT"
    CLOSE_SHORT = "CLOSE_SHORT"
    REVERSAL_CLOSE_SHORT = "REVERSAL_CLOSE_SHORT"


@dataclass(frozen=True, slots=True)
class Candle:
    opened_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "opened_at", aware_utc(self.opened_at))
        for field_name in ("open", "high", "low", "close"):
            object.__setattr__(
                self,
                field_name,
                positive(getattr(self, field_name), field_name),
            )
        if self.high < max(self.open, self.close) or self.low > min(
            self.open, self.close
        ):
            raise ValueError("candle OHLC range is inconsistent")
        if self.low > self.high:
            raise ValueError("candle low cannot exceed high")
        if self.volume is not None:
            object.__setattr__(
                self, "volume", non_negative(self.volume, "volume")
            )


@dataclass(frozen=True, slots=True)
class ExecutionMarker:
    marker_uid: str
    action: MarkerAction
    side: ExecutionSide
    first_at: datetime
    last_at: datetime
    price_vwap: Decimal
    quantity: Decimal
    raw_fill_count: int
    realization_pnl: Decimal | None
    execution_uids: tuple[str, ...]
    realization_uids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TradeChartSpec:
    version: str
    lifecycle_uid: str
    account_id: str
    market_key: str
    direction: PositionDirection
    opened_at: datetime
    closed_at: datetime | None
    interval_seconds: int
    candles: tuple[Candle, ...]
    markers: tuple[ExecutionMarker, ...]
    entry_vwap: Decimal
    exit_vwap: Decimal | None
    realized_pnl: Decimal
    candle_provenance: str
    completeness: str


_INTERVALS = (
    60,
    180,
    300,
    900,
    1800,
    3600,
    7200,
    14400,
    28800,
    43200,
    86400,
)


def select_interval_seconds(
    opened_at: datetime,
    closed_at: datetime,
    *,
    target_candles: int = 180,
) -> int:
    start = aware_utc(opened_at, "opened_at")
    end = aware_utc(closed_at, "closed_at")
    if end < start:
        raise ValueError("closed_at cannot precede opened_at")
    if target_candles <= 0:
        raise ValueError("target_candles must be positive")
    seconds = max(1, int((end - start).total_seconds()))
    for interval in _INTERVALS:
        if (seconds + interval - 1) // interval <= target_candles:
            return interval
    return _INTERVALS[-1]


def _action(
    lifecycle: Lifecycle,
    execution: Execution,
    realization: Realization | None,
    *,
    first: bool,
) -> MarkerAction:
    suffix = lifecycle.direction.value
    if realization is not None:
        prefix = {
            "PARTIAL": "PARTIAL_EXIT",
            "FULL": "CLOSE",
            "REVERSAL_CLOSE": "REVERSAL_CLOSE",
        }[realization.kind]
    else:
        prefix = "OPEN" if first else "ADD"
    return MarkerAction(f"{prefix}_{suffix}")


@dataclass(slots=True)
class _MarkerGroup:
    action: MarkerAction
    side: ExecutionSide
    candle_bucket: int
    executions: list[Execution]
    realizations: list[Realization]


def _freeze_group(group: _MarkerGroup) -> ExecutionMarker:
    quantity = sum((item.quantity for item in group.executions), ZERO)
    vwap = (
        sum((item.price * item.quantity for item in group.executions), ZERO)
        / quantity
    )
    realization_pnl = (
        sum((item.net_pnl for item in group.realizations), ZERO)
        if group.realizations
        else None
    )
    execution_uids = tuple(item.execution_uid for item in group.executions)
    realization_uids = tuple(
        item.realization_uid for item in group.realizations
    )
    return ExecutionMarker(
        marker_uid=stable_uid(
            "chart_marker",
            group.action.value,
            *execution_uids,
        ),
        action=group.action,
        side=group.side,
        first_at=group.executions[0].occurred_at,
        last_at=group.executions[-1].occurred_at,
        price_vwap=vwap,
        quantity=quantity,
        raw_fill_count=len(group.executions),
        realization_pnl=realization_pnl,
        execution_uids=execution_uids,
        realization_uids=realization_uids,
    )


def build_trade_chart_spec(
    lifecycle: Lifecycle,
    projection: AccountProjection,
    *,
    candles: list[Candle] | tuple[Candle, ...],
    interval_seconds: int,
    candle_provenance: str,
    batch_threshold_seconds: int = 120,
) -> TradeChartSpec:
    if lifecycle.account_id != projection.account_id:
        raise ValueError("lifecycle and projection accounts differ")
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    if batch_threshold_seconds < 0:
        raise ValueError("batch_threshold_seconds cannot be negative")

    executions_by_uid = {
        item.execution_uid: item for item in projection.executions
    }
    lifecycle_executions = [
        executions_by_uid[uid]
        for uid in lifecycle.execution_uids
        if uid in executions_by_uid
    ]
    if len(lifecycle_executions) != len(lifecycle.execution_uids):
        raise ValueError("lifecycle references an unavailable execution")
    lifecycle_executions.sort(
        key=lambda item: (item.occurred_at, item.execution_uid)
    )
    realization_by_execution = {
        item.execution_uid: item
        for item in projection.realizations
        if item.lifecycle_uid == lifecycle.lifecycle_uid
    }

    groups: list[_MarkerGroup] = []
    for index, execution in enumerate(lifecycle_executions):
        realization = realization_by_execution.get(execution.execution_uid)
        action = _action(
            lifecycle,
            execution,
            realization,
            first=index == 0,
        )
        bucket = int(execution.occurred_at.timestamp()) // interval_seconds
        can_join = bool(groups) and (
            groups[-1].action is action
            and groups[-1].side is execution.side
            and groups[-1].candle_bucket == bucket
            and (
                execution.occurred_at
                - groups[-1].executions[-1].occurred_at
            ).total_seconds()
            <= batch_threshold_seconds
        )
        if not can_join:
            groups.append(
                _MarkerGroup(
                    action=action,
                    side=execution.side,
                    candle_bucket=bucket,
                    executions=[],
                    realizations=[],
                )
            )
        groups[-1].executions.append(execution)
        if realization is not None:
            groups[-1].realizations.append(realization)

    ordered_candles = tuple(sorted(candles, key=lambda item: item.opened_at))
    if not ordered_candles:
        completeness = "execution_only"
    else:
        candle_start = ordered_candles[0].opened_at
        candle_end = ordered_candles[-1].opened_at.timestamp() + interval_seconds
        covers = all(
            candle_start <= execution.occurred_at
            and execution.occurred_at.timestamp() < candle_end
            for execution in lifecycle_executions
        )
        completeness = "complete" if covers else "partial"

    return TradeChartSpec(
        version="1",
        lifecycle_uid=lifecycle.lifecycle_uid,
        account_id=lifecycle.account_id,
        market_key=lifecycle.market_key,
        direction=lifecycle.direction,
        opened_at=lifecycle.opened_at,
        closed_at=lifecycle.closed_at,
        interval_seconds=interval_seconds,
        candles=ordered_candles,
        markers=tuple(_freeze_group(group) for group in groups),
        entry_vwap=lifecycle.entry_vwap,
        exit_vwap=lifecycle.exit_vwap,
        realized_pnl=lifecycle.realized_pnl,
        candle_provenance=str(candle_provenance),
        completeness=completeness,
    )
