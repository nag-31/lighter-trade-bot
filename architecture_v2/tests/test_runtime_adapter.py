from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from architecture_v2.adapters.runtime_trade import RuntimeTradeAdapter
from architecture_v2.domain.models import ExecutionSide, PositionSide


def runtime_trade(**overrides):
    values = {
        "trade_id": 123,
        "timestamp": datetime(2026, 7, 1, tzinfo=timezone.utc),
        "market_id": 7,
        "market_symbol": "BTC",
        "side": "long",
        "size": Decimal("0.25"),
        "price": Decimal("100000"),
        "source": "Display name",
        "source_id": "hl-main",
        "exchange": "hyperliquid",
        "native_trade_id": "native-123",
        "position_side": "BOTH",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_runtime_buy_fill_normalizes_to_v2_execution() -> None:
    execution = RuntimeTradeAdapter().normalize(runtime_trade())

    assert execution.account_id == "hl-main"
    assert execution.market_key == "hyperliquid:default:BTC"
    assert execution.side is ExecutionSide.BUY
    assert execution.position_side is PositionSide.BOTH
    assert execution.native_trade_id == "native-123"
    assert execution.quantity == Decimal("0.25")


def test_runtime_sell_and_hedge_side_are_preserved() -> None:
    execution = RuntimeTradeAdapter().normalize(
        runtime_trade(side="short", position_side="SHORT")
    )

    assert execution.side is ExecutionSide.SELL
    assert execution.position_side is PositionSide.SHORT


def test_adapter_requires_stable_source_id_not_display_name() -> None:
    with pytest.raises(ValueError, match="source_id"):
        RuntimeTradeAdapter().normalize(
            runtime_trade(source_id="", source="Renamable label")
        )


def test_adapter_accepts_explicit_boundary_identity_for_legacy_trade() -> None:
    execution = RuntimeTradeAdapter().normalize(
        runtime_trade(source_id="", exchange="", native_trade_id=""),
        source_id="lighter-pool-1",
        exchange="lighter",
        namespace="main",
    )

    assert execution.account_id == "lighter-pool-1"
    assert execution.market_key == "lighter:main:BTC"
    assert execution.native_trade_id == "123"


def test_hip3_symbol_keeps_dex_namespace_without_double_prefix() -> None:
    execution = RuntimeTradeAdapter().normalize(
        runtime_trade(market_symbol="xyz:XYZ100"),
        namespace="xyz",
    )

    assert execution.market_key == "hyperliquid:xyz:XYZ100"


def test_adapter_rejects_unknown_runtime_side() -> None:
    with pytest.raises(ValueError, match="side"):
        RuntimeTradeAdapter().normalize(runtime_trade(side="flat"))


def test_fee_can_be_supplied_by_boundary_without_mutating_runtime_trade() -> None:
    trade = runtime_trade()
    execution = RuntimeTradeAdapter().normalize(trade, fee=Decimal("1.25"))

    assert execution.fee == Decimal("1.25")
    assert not hasattr(trade, "fee")
