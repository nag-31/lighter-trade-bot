"""Build V2 execution charts from the legacy dashboard's close-card inputs.

The adapter is deliberately read-only. It turns the fills already used to
render a PnL card into a V2 ``TradeChartSpec`` and leaves accounting untouched.
When candle data is not available, the chart truthfully labels itself
``execution-only`` instead of inventing market candles.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
import re
from typing import Mapping, Sequence

from architecture_v2.domain.charts import (
    Candle,
    build_trade_chart_spec,
    select_interval_seconds,
)
from architecture_v2.domain.models import (
    AccountProjection,
    Execution,
    ExecutionSide,
    Lifecycle,
    LifecycleStatus,
    PositionDirection,
    PositionSide,
    Realization,
)
from architecture_v2.tracker.static_chart import render_trade_chart_png

from .types import Position, Trade


def _timestamp(value: object, fallback: datetime) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        parsed = fallback
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _decimal(value: object, fallback: Decimal = Decimal("0")) -> Decimal:
    try:
        return Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return fallback


def _positive(value: Decimal, fallback: Decimal = Decimal("1")) -> Decimal:
    return value if value > 0 else fallback


def _native_id(value: object) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value))
    return cleaned.strip("_.-") or "legacy_fill"


def render_legacy_execution_chart(
    *,
    source_id: str,
    source_name: str,
    exchange: str,
    market_symbol: str,
    position: Position,
    close_trade: Trade,
    reduced_size: Decimal,
    fill_price: Decimal,
    realized_pnl: Decimal | None,
    pnl_override: Decimal | None = None,
    partial_rows: Sequence[Mapping[str, object]] = (),
    opening_trade: Trade | None = None,
    candles: Sequence[Candle] = (),
    candle_provenance: str = "execution-only",
) -> bytes:
    """Render one lifecycle chart using the legacy close-card data.

    When a public candle provider is available, its normalized OHLC rows are
    included in the same chart spec. If it is unavailable, the V2 renderer
    still shows the entry, partial exits, and final close on a truthful
    execution timeline and labels the footer ``execution-only``.
    """
    direction = (
        PositionDirection.LONG
        if position.side == "long"
        else PositionDirection.SHORT
    )
    v2_position_side = PositionSide(position.position_side or "BOTH")
    lifecycle_uid = (
        f"legacy:{source_id}:{close_trade.market_id}:"
        f"{close_trade.position_side}:{close_trade.event_uid(source_id)}"
    )
    account_id = source_id or source_name
    market_key = f"{exchange or source_name}:{market_symbol}"
    close_at = close_trade.timestamp.astimezone(timezone.utc)

    rows = list(partial_rows)
    row_times = [_timestamp(row.get("ts"), close_at) for row in rows]
    if opening_trade is not None:
        opened_at = opening_trade.timestamp.astimezone(timezone.utc)
    elif row_times:
        opened_at = min(row_times) - timedelta(seconds=1)
    else:
        opened_at = close_at - timedelta(seconds=1)
    if opened_at >= close_at:
        opened_at = close_at - timedelta(seconds=1)

    executions: list[Execution] = []
    realizations: list[Realization] = []

    open_side = ExecutionSide.BUY if direction is PositionDirection.LONG else ExecutionSide.SELL
    close_side = ExecutionSide.SELL if direction is PositionDirection.LONG else ExecutionSide.BUY

    open_quantity = _positive(position.size, _positive(reduced_size))
    open_price = _positive(position.avg_entry_price, _positive(fill_price))
    open_native_id = (
        f"chart_open_{source_id}_{close_trade.market_id}_{opening_trade.trade_id}"
        if opening_trade is not None
        else f"chart_open_{source_id}_{close_trade.market_id}"
    )
    executions.append(
        Execution.create(
            account_id=account_id,
            exchange=exchange or source_name,
            market_key=market_key,
            position_side=v2_position_side,
            native_trade_id=_native_id(open_native_id),
            occurred_at=opened_at,
            side=open_side,
            quantity=open_quantity,
            price=open_price,
        )
    )

    exit_quantity = Decimal("0")
    exit_notional = Decimal("0")
    known_pnl = Decimal("0")
    pnl_known = True
    for index, row in enumerate(rows):
        quantity = _positive(_decimal(row.get("size")))
        price = _positive(_decimal(row.get("exit"), fill_price))
        occurred_at = _timestamp(row.get("ts"), close_at)
        native_id = _native_id(
            row.get("native_trade_id")
            or row.get("trade_id")
            or row.get("event_uid")
            or f"{lifecycle_uid}:partial:{index}"
        )
        execution = Execution.create(
            account_id=account_id,
            exchange=exchange or source_name,
            market_key=market_key,
            position_side=v2_position_side,
            native_trade_id=native_id,
            occurred_at=occurred_at,
            side=close_side,
            quantity=quantity,
            price=price,
        )
        executions.append(execution)
        exit_quantity += quantity
        exit_notional += quantity * price
        row_pnl = row.get("pnl")
        if row_pnl is None:
            pnl_known = False
        else:
            known_pnl += _decimal(row_pnl)
            realizations.append(
                Realization(
                    realization_uid=f"{lifecycle_uid}:realization:{native_id}",
                    execution_uid=execution.execution_uid,
                    lifecycle_uid=lifecycle_uid,
                    account_id=account_id,
                    market_key=market_key,
                    position_side=v2_position_side,
                    direction=direction,
                    occurred_at=occurred_at,
                    quantity=quantity,
                    entry_price=_positive(_decimal(row.get("entry"), open_price)),
                    exit_price=price,
                    gross_pnl=_decimal(row_pnl),
                    fees=Decimal("0"),
                    funding=Decimal("0"),
                    net_pnl=_decimal(row_pnl),
                    kind="PARTIAL",
                )
            )

    close_quantity = _positive(reduced_size)
    close_execution = Execution.create(
        account_id=account_id,
        exchange=exchange or source_name,
        market_key=market_key,
        position_side=v2_position_side,
        native_trade_id=_native_id(close_trade.native_trade_id or close_trade.trade_id),
        occurred_at=close_at,
        side=close_side,
        quantity=close_quantity,
        price=_positive(fill_price),
    )
    executions.append(close_execution)
    exit_quantity += close_quantity
    exit_notional += close_quantity * _positive(fill_price)
    if realized_pnl is None:
        pnl_known = False
    else:
        known_pnl += realized_pnl
        realizations.append(
            Realization(
                realization_uid=f"{lifecycle_uid}:realization:{close_execution.native_trade_id}",
                execution_uid=close_execution.execution_uid,
                lifecycle_uid=lifecycle_uid,
                account_id=account_id,
                market_key=market_key,
                position_side=v2_position_side,
                direction=direction,
                occurred_at=close_at,
                quantity=close_quantity,
                entry_price=open_price,
                exit_price=_positive(fill_price),
                gross_pnl=realized_pnl,
                fees=Decimal("0"),
                funding=Decimal("0"),
                net_pnl=realized_pnl,
                kind="FULL",
            )
        )

    chart_pnl = pnl_override if pnl_override is not None else known_pnl
    if not pnl_known and pnl_override is None:
        chart_pnl = Decimal("0")
    exit_vwap = exit_notional / exit_quantity if exit_quantity else None
    lifecycle = Lifecycle(
        lifecycle_uid=lifecycle_uid,
        account_id=account_id,
        position_key=f"{account_id}:{market_key}:{v2_position_side.value}",
        market_key=market_key,
        position_side=v2_position_side,
        direction=direction,
        opened_at=opened_at,
        closed_at=close_at,
        status=LifecycleStatus.CLOSED,
        entry_vwap=open_price,
        exit_vwap=exit_vwap,
        max_quantity=open_quantity,
        closed_quantity=exit_quantity,
        gross_pnl=chart_pnl,
        fees=Decimal("0"),
        funding=Decimal("0"),
        realized_pnl=chart_pnl,
        execution_uids=tuple(item.execution_uid for item in executions),
        realization_uids=tuple(item.realization_uid for item in realizations),
    )
    projection = AccountProjection(
        account_id=account_id,
        executions=tuple(executions),
        realizations=tuple(realizations),
        lifecycles=(lifecycle,),
        open_positions=(),
    )
    interval = select_interval_seconds(opened_at, close_at)
    spec = build_trade_chart_spec(
        lifecycle,
        projection,
        candles=tuple(candles),
        interval_seconds=interval,
        candle_provenance=candle_provenance,
    )
    # The exchange-style chart is a detachable presentation plugin.  Set
    # CHART_STYLE=classic (or CHART_STYLE=off) to unplug it and retain the
    # deterministic Pillow renderer without changing accounting or delivery.
    chart_style = os.getenv("CHART_STYLE", "exchange").strip().lower()
    renderer = os.getenv("CHART_RENDERER", "auto").strip().lower()
    if chart_style in {"classic", "legacy", "off", "disabled"}:
        return render_trade_chart_png(spec)
    if renderer in {"auto", "plotly"}:
        try:
            from architecture_v2.tracker.plotly_chart import (
                render_plotly_chart_png,
            )

            return render_plotly_chart_png(spec)
        except Exception:
            if renderer == "plotly":
                raise
    return render_trade_chart_png(spec)
