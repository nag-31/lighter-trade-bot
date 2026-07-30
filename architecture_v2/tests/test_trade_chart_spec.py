from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from architecture_v2.domain.accounting import project_account
from architecture_v2.domain.charts import (
    Candle,
    MarkerAction,
    build_trade_chart_spec,
    select_interval_seconds,
)
from architecture_v2.domain.models import Execution, ExecutionSide, PositionSide


UTC = timezone.utc


def at(minute: int) -> datetime:
    return datetime(2026, 7, 1, 12, minute, tzinfo=UTC)


def fill(
    native_id: str,
    *,
    when: datetime,
    side: ExecutionSide,
    qty: str,
    price: str,
) -> Execution:
    return Execution.create(
        account_id="hl-main",
        exchange="hyperliquid",
        market_key="hyperliquid:default:BTC",
        position_side=PositionSide.BOTH,
        native_trade_id=native_id,
        occurred_at=when,
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
    )


def candles(count: int = 10) -> list[Candle]:
    return [
        Candle(
            opened_at=at(0) + timedelta(minutes=index),
            open=Decimal("99"),
            high=Decimal("111"),
            low=Decimal("98"),
            close=Decimal("105"),
            volume=Decimal("10"),
        )
        for index in range(count)
    ]


def test_long_chart_labels_open_partial_and_close_with_buy_sell_semantics() -> None:
    projection = project_account(
        "hl-main",
        [
            fill("open", when=at(1), side=ExecutionSide.BUY, qty="10", price="100"),
            fill("partial", when=at(3), side=ExecutionSide.SELL, qty="4", price="110"),
            fill("close", when=at(5), side=ExecutionSide.SELL, qty="6", price="105"),
        ],
    )
    lifecycle = projection.lifecycles[0]

    spec = build_trade_chart_spec(
        lifecycle,
        projection,
        candles=candles(),
        interval_seconds=60,
        candle_provenance="hyperliquid:candleSnapshot",
    )

    assert [marker.action for marker in spec.markers] == [
        MarkerAction.OPEN_LONG,
        MarkerAction.PARTIAL_EXIT_LONG,
        MarkerAction.CLOSE_LONG,
    ]
    assert [marker.side for marker in spec.markers] == [
        ExecutionSide.BUY,
        ExecutionSide.SELL,
        ExecutionSide.SELL,
    ]
    assert spec.realized_pnl == Decimal("70")
    assert spec.lifecycle_uid == lifecycle.lifecycle_uid


def test_short_close_is_buy_close_short_not_long_entry() -> None:
    projection = project_account(
        "hl-main",
        [
            fill("open", when=at(1), side=ExecutionSide.SELL, qty="2", price="100"),
            fill("close", when=at(4), side=ExecutionSide.BUY, qty="2", price="90"),
        ],
    )

    spec = build_trade_chart_spec(
        projection.lifecycles[0],
        projection,
        candles=candles(),
        interval_seconds=60,
        candle_provenance="hyperliquid:candleSnapshot",
    )

    assert spec.markers[0].action is MarkerAction.OPEN_SHORT
    assert spec.markers[0].side is ExecutionSide.SELL
    assert spec.markers[1].action is MarkerAction.CLOSE_SHORT
    assert spec.markers[1].side is ExecutionSide.BUY


def test_dense_add_fills_group_by_action_candle_and_threshold() -> None:
    projection = project_account(
        "hl-main",
        [
            fill("open", when=at(1), side=ExecutionSide.BUY, qty="1", price="100"),
            fill(
                "add-1",
                when=at(2) + timedelta(seconds=5),
                side=ExecutionSide.BUY,
                qty="2",
                price="101",
            ),
            fill(
                "add-2",
                when=at(2) + timedelta(seconds=35),
                side=ExecutionSide.BUY,
                qty="1",
                price="103",
            ),
            fill("close", when=at(5), side=ExecutionSide.SELL, qty="4", price="110"),
        ],
    )

    spec = build_trade_chart_spec(
        projection.lifecycles[0],
        projection,
        candles=candles(),
        interval_seconds=60,
        candle_provenance="hyperliquid:candleSnapshot",
        batch_threshold_seconds=120,
    )

    assert len(spec.markers) == 3
    grouped = spec.markers[1]
    assert grouped.action is MarkerAction.ADD_LONG
    assert grouped.raw_fill_count == 2
    assert grouped.quantity == Decimal("3")
    assert grouped.price_vwap == Decimal("101.6666666666666666666666667")
    assert grouped.execution_uids == (
        projection.executions[1].execution_uid,
        projection.executions[2].execution_uid,
    )


def test_same_candle_open_and_add_remain_separate_semantic_markers() -> None:
    projection = project_account(
        "hl-main",
        [
            fill(
                "open",
                when=at(1) + timedelta(seconds=5),
                side=ExecutionSide.BUY,
                qty="1",
                price="100",
            ),
            fill(
                "add",
                when=at(1) + timedelta(seconds=20),
                side=ExecutionSide.BUY,
                qty="1",
                price="101",
            ),
        ],
    )

    spec = build_trade_chart_spec(
        projection.lifecycles[0],
        projection,
        candles=candles(),
        interval_seconds=60,
        candle_provenance="hyperliquid:candleSnapshot",
    )

    assert [marker.action for marker in spec.markers] == [
        MarkerAction.OPEN_LONG,
        MarkerAction.ADD_LONG,
    ]


def test_reversal_execution_has_close_action_in_old_trade_and_open_in_new() -> None:
    projection = project_account(
        "hl-main",
        [
            fill("open-long", when=at(1), side=ExecutionSide.BUY, qty="2", price="100"),
            fill("reverse", when=at(3), side=ExecutionSide.SELL, qty="3", price="110"),
            fill("close-short", when=at(5), side=ExecutionSide.BUY, qty="1", price="100"),
        ],
    )
    old_spec = build_trade_chart_spec(
        projection.lifecycles[0],
        projection,
        candles=candles(),
        interval_seconds=60,
        candle_provenance="hyperliquid:candleSnapshot",
    )
    new_spec = build_trade_chart_spec(
        projection.lifecycles[1],
        projection,
        candles=candles(),
        interval_seconds=60,
        candle_provenance="hyperliquid:candleSnapshot",
    )

    assert old_spec.markers[-1].action is MarkerAction.REVERSAL_CLOSE_LONG
    assert new_spec.markers[0].action is MarkerAction.OPEN_SHORT
    assert (
        old_spec.markers[-1].execution_uids
        == new_spec.markers[0].execution_uids
    )


def test_chart_contains_only_selected_lifecycle_executions() -> None:
    projection = project_account(
        "hl-main",
        [
            fill("open-1", when=at(1), side=ExecutionSide.BUY, qty="1", price="100"),
            fill("close-1", when=at(2), side=ExecutionSide.SELL, qty="1", price="110"),
            fill("open-2", when=at(4), side=ExecutionSide.BUY, qty="1", price="105"),
            fill("close-2", when=at(5), side=ExecutionSide.SELL, qty="1", price="106"),
        ],
    )

    first = build_trade_chart_spec(
        projection.lifecycles[0],
        projection,
        candles=candles(),
        interval_seconds=60,
        candle_provenance="hyperliquid:candleSnapshot",
    )

    assert len(first.markers) == 2
    assert {
        uid for marker in first.markers for uid in marker.execution_uids
    } == set(projection.lifecycles[0].execution_uids)


def test_interval_selector_uses_smallest_interval_under_target() -> None:
    opened = datetime(2026, 7, 1, tzinfo=UTC)

    assert select_interval_seconds(opened, opened + timedelta(minutes=10)) == 60
    assert select_interval_seconds(opened, opened + timedelta(days=2)) == 1800
    assert select_interval_seconds(opened, opened + timedelta(days=26)) == 14400


def test_spec_discloses_provenance_and_incomplete_candle_coverage() -> None:
    projection = project_account(
        "hl-main",
        [fill("open", when=at(1), side=ExecutionSide.BUY, qty="1", price="100")],
    )

    spec = build_trade_chart_spec(
        projection.lifecycles[0],
        projection,
        candles=[],
        interval_seconds=60,
        candle_provenance="execution-only",
    )

    assert spec.candle_provenance == "execution-only"
    assert spec.completeness == "execution_only"
    assert spec.candles == ()
