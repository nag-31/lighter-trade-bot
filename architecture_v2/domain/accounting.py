from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from .identity import stable_uid
from .models import (
    ZERO,
    AccountProjection,
    Execution,
    ExecutionSide,
    Lifecycle,
    LifecycleStatus,
    PositionDirection,
    PositionSide,
    PositionState,
    Realization,
)


class AccountingInvariantError(ValueError):
    """Raised when normalized executions cannot form a valid position history."""


@dataclass(slots=True)
class _OpenLifecycle:
    lifecycle_uid: str
    account_id: str
    position_key: str
    market_key: str
    position_side: PositionSide
    direction: PositionDirection
    opened_at: datetime
    quantity: Decimal
    average_entry_price: Decimal
    entry_fee_basis: Decimal
    entry_quantity: Decimal
    entry_notional: Decimal
    max_quantity: Decimal
    exit_quantity: Decimal = ZERO
    exit_notional: Decimal = ZERO
    gross_pnl: Decimal = ZERO
    fees: Decimal = ZERO
    funding: Decimal = ZERO
    execution_uids: list[str] = field(default_factory=list)
    realization_uids: list[str] = field(default_factory=list)


def _opening_direction(execution: Execution) -> PositionDirection:
    if execution.position_side is PositionSide.LONG:
        if execution.side is not ExecutionSide.BUY:
            raise AccountingInvariantError("LONG position must open with BUY")
        return PositionDirection.LONG
    if execution.position_side is PositionSide.SHORT:
        if execution.side is not ExecutionSide.SELL:
            raise AccountingInvariantError("SHORT position must open with SELL")
        return PositionDirection.SHORT
    return (
        PositionDirection.LONG
        if execution.side is ExecutionSide.BUY
        else PositionDirection.SHORT
    )


def _increases(position: _OpenLifecycle, execution: Execution) -> bool:
    if position.direction is PositionDirection.LONG:
        return execution.side is ExecutionSide.BUY
    return execution.side is ExecutionSide.SELL


def _open_position(
    execution: Execution,
    *,
    direction: PositionDirection,
    quantity: Decimal,
    fee: Decimal,
    leg: int,
) -> _OpenLifecycle:
    lifecycle_uid = stable_uid(
        "lifecycle",
        execution.account_id,
        execution.position_key,
        execution.execution_uid,
        leg,
    )
    return _OpenLifecycle(
        lifecycle_uid=lifecycle_uid,
        account_id=execution.account_id,
        position_key=execution.position_key,
        market_key=execution.market_key,
        position_side=execution.position_side,
        direction=direction,
        opened_at=execution.occurred_at,
        quantity=quantity,
        average_entry_price=execution.price,
        entry_fee_basis=fee,
        entry_quantity=quantity,
        entry_notional=execution.price * quantity,
        max_quantity=quantity,
        execution_uids=[execution.execution_uid],
    )


def _freeze_lifecycle(
    position: _OpenLifecycle,
    *,
    closed_at=None,
) -> Lifecycle:
    closed = closed_at is not None
    duration_ms = (
        max(0, int(round((closed_at - position.opened_at).total_seconds() * 1000)))
        if closed_at is not None else None
    )
    return Lifecycle(
        lifecycle_uid=position.lifecycle_uid,
        account_id=position.account_id,
        position_key=position.position_key,
        market_key=position.market_key,
        position_side=position.position_side,
        direction=position.direction,
        opened_at=position.opened_at,
        closed_at=closed_at,
        holding_duration_ms=duration_ms,
        holding_duration_basis="exact" if closed else "unavailable",
        status=LifecycleStatus.CLOSED if closed else LifecycleStatus.OPEN,
        entry_vwap=position.entry_notional / position.entry_quantity,
        exit_vwap=(
            position.exit_notional / position.exit_quantity
            if position.exit_quantity
            else None
        ),
        max_quantity=position.max_quantity,
        closed_quantity=position.exit_quantity,
        gross_pnl=position.gross_pnl,
        fees=position.fees,
        funding=position.funding,
        realized_pnl=position.gross_pnl - position.fees + position.funding,
        execution_uids=tuple(position.execution_uids),
        realization_uids=tuple(position.realization_uids),
    )


def project_account(
    account_id: str,
    executions: list[Execution] | tuple[Execution, ...],
) -> AccountProjection:
    """Project one account independently from normalized immutable executions."""
    unique: dict[str, Execution] = {}
    for execution in executions:
        if execution.account_id != account_id:
            raise AccountingInvariantError(
                f"execution {execution.execution_uid} belongs to "
                f"{execution.account_id}, not {account_id}"
            )
        prior = unique.get(execution.execution_uid)
        if prior is not None and prior != execution:
            raise AccountingInvariantError(
                f"execution UID collision: {execution.execution_uid}"
            )
        unique[execution.execution_uid] = execution

    ordered = tuple(
        sorted(unique.values(), key=lambda item: (item.occurred_at, item.execution_uid))
    )
    open_by_key: dict[str, _OpenLifecycle] = {}
    completed: list[Lifecycle] = []
    realizations: list[Realization] = []

    for execution in ordered:
        key = execution.position_key
        position = open_by_key.get(key)
        if position is None:
            open_by_key[key] = _open_position(
                execution,
                direction=_opening_direction(execution),
                quantity=execution.quantity,
                fee=execution.fee,
                leg=0,
            )
            continue

        if execution.execution_uid not in position.execution_uids:
            position.execution_uids.append(execution.execution_uid)

        if _increases(position, execution):
            new_quantity = position.quantity + execution.quantity
            position.average_entry_price = (
                position.average_entry_price * position.quantity
                + execution.price * execution.quantity
            ) / new_quantity
            position.quantity = new_quantity
            position.entry_fee_basis += execution.fee
            position.entry_quantity += execution.quantity
            position.entry_notional += execution.price * execution.quantity
            position.max_quantity = max(position.max_quantity, new_quantity)
            continue

        close_quantity = min(position.quantity, execution.quantity)
        if (
            execution.position_side is not PositionSide.BOTH
            and execution.quantity > position.quantity
        ):
            raise AccountingInvariantError(
                "explicit hedge-side close cannot cross through zero"
            )

        entry_fee = (
            position.entry_fee_basis * close_quantity / position.quantity
        )
        exit_fee = execution.fee * close_quantity / execution.quantity
        if position.direction is PositionDirection.LONG:
            gross_pnl = (
                execution.price - position.average_entry_price
            ) * close_quantity
        else:
            gross_pnl = (
                position.average_entry_price - execution.price
            ) * close_quantity

        remaining_execution = execution.quantity - close_quantity
        closes_position = close_quantity == position.quantity
        kind = (
            "REVERSAL_CLOSE"
            if closes_position and remaining_execution > ZERO
            else "FULL"
            if closes_position
            else "PARTIAL"
        )
        realization_uid = stable_uid(
            "realization",
            position.lifecycle_uid,
            execution.execution_uid,
            len(position.realization_uids),
        )
        realization = Realization(
            realization_uid=realization_uid,
            execution_uid=execution.execution_uid,
            lifecycle_uid=position.lifecycle_uid,
            account_id=execution.account_id,
            market_key=execution.market_key,
            position_side=execution.position_side,
            direction=position.direction,
            occurred_at=execution.occurred_at,
            quantity=close_quantity,
            entry_price=position.average_entry_price,
            exit_price=execution.price,
            gross_pnl=gross_pnl,
            fees=entry_fee + exit_fee,
            funding=ZERO,
            net_pnl=gross_pnl - entry_fee - exit_fee,
            kind=kind,
        )
        realizations.append(realization)
        position.realization_uids.append(realization_uid)
        position.exit_quantity += close_quantity
        position.exit_notional += execution.price * close_quantity
        position.gross_pnl += gross_pnl
        position.fees += entry_fee + exit_fee
        position.entry_fee_basis -= entry_fee
        position.quantity -= close_quantity

        if not closes_position:
            continue

        completed.append(
            _freeze_lifecycle(position, closed_at=execution.occurred_at)
        )
        del open_by_key[key]

        if remaining_execution > ZERO:
            reversal_direction = (
                PositionDirection.SHORT
                if position.direction is PositionDirection.LONG
                else PositionDirection.LONG
            )
            remaining_fee = execution.fee - exit_fee
            open_by_key[key] = _open_position(
                execution,
                direction=reversal_direction,
                quantity=remaining_execution,
                fee=remaining_fee,
                leg=1,
            )

    open_lifecycles = [_freeze_lifecycle(item) for item in open_by_key.values()]
    all_lifecycles = tuple(
        sorted(
            [*completed, *open_lifecycles],
            key=lambda item: (item.opened_at, item.lifecycle_uid),
        )
    )
    open_positions = tuple(
        PositionState(
            account_id=item.account_id,
            position_key=item.position_key,
            market_key=item.market_key,
            position_side=item.position_side,
            direction=item.direction,
            quantity=item.quantity,
            average_entry_price=item.average_entry_price,
            entry_fee_basis=item.entry_fee_basis,
            lifecycle_uid=item.lifecycle_uid,
            opened_at=item.opened_at,
        )
        for item in sorted(open_by_key.values(), key=lambda pos: pos.position_key)
    )
    return AccountProjection(
        account_id=account_id,
        executions=ordered,
        realizations=tuple(realizations),
        lifecycles=all_lifecycles,
        open_positions=open_positions,
    )
