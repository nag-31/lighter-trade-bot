from __future__ import annotations

from decimal import Decimal
from typing import Any

from architecture_v2.domain.models import (
    Execution,
    ExecutionSide,
    PositionSide,
)


class RuntimeTradeAdapter:
    """Translate the current runtime Trade shape at an explicit V2 seam.

    This module intentionally uses structural attribute access instead of
    importing ``src.types``. V2 remains independently testable and current
    production code does not depend on V2 until a controlled shadow cutover.
    """

    def normalize(
        self,
        trade: Any,
        *,
        source_id: str = "",
        exchange: str = "",
        namespace: str = "",
        fee: Decimal = Decimal("0"),
    ) -> Execution:
        account = str(source_id or getattr(trade, "source_id", "") or "").strip()
        if not account:
            raise ValueError("source_id is required at the V2 boundary")

        venue = str(exchange or getattr(trade, "exchange", "") or "").strip().lower()
        if not venue:
            raise ValueError("exchange is required at the V2 boundary")

        raw_symbol = str(getattr(trade, "market_symbol", "") or "").strip()
        if not raw_symbol:
            raise ValueError("market_symbol is required")
        symbol_namespace = str(namespace or "").strip().lower()
        symbol = raw_symbol
        if ":" in raw_symbol:
            embedded_namespace, embedded_symbol = raw_symbol.split(":", 1)
            if not symbol_namespace:
                symbol_namespace = embedded_namespace.lower()
            if symbol_namespace == embedded_namespace.lower():
                symbol = embedded_symbol
        symbol_namespace = symbol_namespace or "default"
        market_key = f"{venue}:{symbol_namespace}:{symbol.upper()}"

        raw_side = str(getattr(trade, "side", "") or "").strip().lower()
        if raw_side in {"long", "buy", "b", "bid"}:
            side = ExecutionSide.BUY
        elif raw_side in {"short", "sell", "a", "ask"}:
            side = ExecutionSide.SELL
        else:
            raise ValueError(f"unsupported runtime side: {raw_side or '(blank)'}")

        native = str(
            getattr(trade, "native_trade_id", "")
            or getattr(trade, "trade_id", "")
            or ""
        ).strip()
        if not native:
            raise ValueError("native_trade_id is required")

        return Execution.create(
            account_id=account,
            exchange=venue,
            market_key=market_key,
            position_side=PositionSide(
                str(getattr(trade, "position_side", "BOTH") or "BOTH").upper()
            ),
            native_trade_id=native,
            occurred_at=getattr(trade, "timestamp"),
            side=side,
            quantity=Decimal(getattr(trade, "size")),
            price=Decimal(getattr(trade, "price")),
            fee=Decimal(fee),
        )
