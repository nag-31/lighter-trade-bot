"""Read-only V2 presentation adapter for the standalone Trade Journal.

The Journal database remains owned by the existing ingestion path.  This
module translates its lifecycle rows into the immutable V2 domain objects and
serializes one ``TradeChartSpec`` for the browser.  It deliberately does not
write V2 tables or recalculate stored PnL for the legacy consumer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import re
from typing import Any, Iterable

from holding_time import holding_duration_ms

from architecture_v2.domain.accounting import project_account
from architecture_v2.domain.charts import build_trade_chart_spec, select_interval_seconds
from architecture_v2.domain.models import Execution, ExecutionSide, PositionSide


def _timestamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _identity(value: Any, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.:@/-]+", "_", str(value or fallback)).strip("_.")
    return cleaned or fallback


def _execution_side(action: Any, direction: Any) -> ExecutionSide:
    """Map lifecycle semantics to transaction-side colors correctly."""
    normalized = str(action or "").upper()
    is_long = str(direction or "").upper() == "LONG" or normalized.endswith("_LONG")
    opening = normalized.startswith(("OPEN", "ADD"))
    if opening:
        return ExecutionSide.BUY if is_long else ExecutionSide.SELL
    return ExecutionSide.SELL if is_long else ExecutionSide.BUY


def _execution_rows(item: dict[str, Any]) -> list[Execution]:
    source = _identity(item.get("source"), "journal")
    symbol = _identity(item.get("symbol"), "unknown")
    account_id = f"journal:{source}:{symbol}:{item.get('id') or '0'}"
    exchange = source
    market_key = f"{exchange}:{symbol}"
    executions: list[Execution] = []
    for index, row in enumerate(item.get("executions") or ()):
        try:
            occurred_at = _timestamp(row.get("occurred_at"))
            quantity = _decimal(row.get("size") or row.get("quantity"))
            price = _decimal(row.get("price"))
            action = row.get("action") or row.get("batch_label") or ""
            native_id = str(row.get("execution_key") or row.get("native_trade_id") or f"fill-{index}")
            executions.append(
                Execution.create(
                    account_id=account_id,
                    exchange=exchange,
                    market_key=market_key,
                    position_side=PositionSide.BOTH,
                    native_trade_id=native_id,
                    occurred_at=occurred_at,
                    side=_execution_side(action, item.get("side")),
                    quantity=quantity,
                    price=price,
                )
            )
        except (AttributeError, TypeError, ValueError, ArithmeticError):
            continue
    return executions


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return _timestamp(value).isoformat()


def serialize_chart_spec(spec: Any) -> dict[str, Any]:
    return {
        "version": spec.version,
        "lifecycle_uid": spec.lifecycle_uid,
        "account_id": spec.account_id,
        "market_key": spec.market_key,
        "direction": spec.direction.value,
        "opened_at": spec.opened_at.isoformat(),
        "closed_at": spec.closed_at.isoformat() if spec.closed_at else None,
        "holding_duration_ms": holding_duration_ms(spec.opened_at, spec.closed_at),
        "holding_duration_basis": "exact" if spec.closed_at else "unavailable",
        "interval_seconds": spec.interval_seconds,
        "candles": [
            {
                "opened_at": candle.opened_at.isoformat(),
                "open": str(candle.open),
                "high": str(candle.high),
                "low": str(candle.low),
                "close": str(candle.close),
                "volume": str(candle.volume) if candle.volume is not None else None,
            }
            for candle in spec.candles
        ],
        "markers": [
            {
                "action": marker.action.value,
                "side": marker.side.value,
                "first_at": marker.first_at.isoformat(),
                "last_at": marker.last_at.isoformat(),
                "price_vwap": str(marker.price_vwap),
                "quantity": str(marker.quantity),
                "raw_fill_count": marker.raw_fill_count,
                "realization_pnl": str(marker.realization_pnl) if marker.realization_pnl is not None else None,
                "execution_uids": list(marker.execution_uids),
            }
            for marker in spec.markers
        ],
        "entry_vwap": str(spec.entry_vwap),
        "exit_vwap": str(spec.exit_vwap) if spec.exit_vwap is not None else None,
        "realized_pnl": str(spec.realized_pnl),
        "candle_provenance": spec.candle_provenance,
        "completeness": spec.completeness,
    }


def build_v2_lifecycles(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in items:
        if not item.get("is_lifecycle"):
            continue
        executions = _execution_rows(item)
        chart = None
        if executions:
            try:
                projection = project_account(executions[0].account_id, executions)
                lifecycle = projection.lifecycles[0]
                closed_at = lifecycle.closed_at or lifecycle.opened_at
                chart = serialize_chart_spec(
                    build_trade_chart_spec(
                        lifecycle,
                        projection,
                        candles=(),
                        interval_seconds=select_interval_seconds(lifecycle.opened_at, closed_at),
                        candle_provenance="execution-only:journal",
                    )
                )
            except (TypeError, ValueError, ArithmeticError, IndexError):
                chart = None
        result.append(
            {
                "id": item.get("id"),
                "lifecycle_key": item.get("lifecycle_key"),
                "source": item.get("source"),
                "symbol": item.get("symbol"),
                "side": item.get("side"),
                "status": item.get("status"),
                "opened_at": _iso(item.get("opened_at")),
                "closed_at": _iso(item.get("closed_at")),
                "holding_duration_ms": item.get("holding_duration_ms")
                    if item.get("holding_duration_ms") is not None
                    else holding_duration_ms(item.get("opened_at"), item.get("closed_at")),
                "holding_duration_basis": item.get("holding_duration_basis") or (
                    "exact" if item.get("closed_at") else "unavailable"
                ),
                "entry": item.get("entry_vwap"),
                "exit": item.get("exit_vwap"),
                "size": item.get("max_size"),
                "position_value": item.get("notional"),
                "pnl": item.get("pnl"),
                "pnl_pct": item.get("pnl_pct"),
                "fill_count": item.get("fill_count") or len(executions),
                "decision_id": item.get("decision_id"),
                "chart": chart,
                "execution_count": len(executions),
            }
        )
    return result
