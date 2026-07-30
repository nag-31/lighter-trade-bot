from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from architecture_v2.application.portfolio import project_portfolio
from architecture_v2.domain.accounting import project_account
from architecture_v2.domain.models import (
    Execution,
    ExecutionSide,
    LifecycleStatus,
    PositionDirection,
    PositionSide,
)
from architecture_v2.domain.reports import build_period_report


UTC = timezone.utc


def ts(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 7, day, hour, tzinfo=UTC)


def fill(
    native_id: str,
    *,
    account: str = "hl-main",
    market: str = "hyperliquid:default:BTC",
    at: datetime | None = None,
    side: ExecutionSide = ExecutionSide.BUY,
    qty: str = "1",
    price: str = "100",
    fee: str = "0",
    position_side: PositionSide = PositionSide.BOTH,
) -> Execution:
    return Execution.create(
        account_id=account,
        exchange=market.split(":", 1)[0],
        market_key=market,
        position_side=position_side,
        native_trade_id=native_id,
        occurred_at=at or ts(1),
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
        fee=Decimal(fee),
    )


def test_execution_identity_is_deterministic_and_account_scoped() -> None:
    first = fill("native-1")
    replay = fill("native-1")
    other_account = fill("native-1", account="hl-secondary")

    assert first.execution_uid == replay.execution_uid
    assert first.execution_uid != other_account.execution_uid


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("quantity", Decimal("0")),
        ("quantity", Decimal("-1")),
        ("price", Decimal("0")),
        ("fee", Decimal("-0.01")),
    ],
)
def test_execution_rejects_invalid_money_fields(field: str, value: Decimal) -> None:
    kwargs = {
        "account_id": "hl-main",
        "exchange": "hyperliquid",
        "market_key": "hyperliquid:default:BTC",
        "position_side": PositionSide.BOTH,
        "native_trade_id": "bad",
        "occurred_at": ts(1),
        "side": ExecutionSide.BUY,
        "quantity": Decimal("1"),
        "price": Decimal("100"),
        "fee": Decimal("0"),
    }
    kwargs[field] = value

    with pytest.raises(ValueError):
        Execution.create(**kwargs)


def test_execution_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone"):
        fill("naive", at=datetime(2026, 7, 1))


def test_duplicate_execution_is_accounted_once() -> None:
    opened = fill("open", qty="10", price="100")
    closed = fill(
        "close",
        at=ts(2),
        side=ExecutionSide.SELL,
        qty="10",
        price="110",
    )

    projection = project_account("hl-main", [opened, opened, closed, closed])

    assert projection.execution_count == 2
    assert len(projection.realizations) == 1
    assert projection.realizations[0].net_pnl == Decimal("100")
    assert len(projection.lifecycles) == 1


def test_partial_exits_book_pnl_at_fill_time_and_close_trade_once() -> None:
    projection = project_account(
        "hl-main",
        [
            fill("open", at=ts(1), qty="10", price="100"),
            fill(
                "partial",
                at=ts(10),
                side=ExecutionSide.SELL,
                qty="4",
                price="110",
            ),
            fill(
                "final",
                at=ts(20),
                side=ExecutionSide.SELL,
                qty="6",
                price="120",
            ),
        ],
    )

    assert [item.net_pnl for item in projection.realizations] == [
        Decimal("40"),
        Decimal("120"),
    ]
    assert projection.realizations[0].kind == "PARTIAL"
    assert projection.realizations[1].kind == "FULL"
    assert projection.lifecycles[0].realized_pnl == Decimal("160")
    assert projection.lifecycles[0].status is LifecycleStatus.CLOSED

    portfolio = project_portfolio(projection.executions)
    early = build_period_report(portfolio, start_at=ts(1), end_at=ts(15))
    late = build_period_report(portfolio, start_at=ts(15), end_at=ts(25))
    all_time = build_period_report(portfolio)

    assert early.realized_pnl == Decimal("40")
    assert early.realization_count == 1
    assert early.trades_closed == 0

    assert late.realized_pnl == Decimal("120")
    assert late.realization_count == 1
    assert late.trades_closed == 1
    assert late.wins == 1

    assert all_time.realized_pnl == Decimal("160")
    assert early.realized_pnl + late.realized_pnl == all_time.realized_pnl


def test_losing_final_fill_can_close_a_winning_lifecycle() -> None:
    portfolio = project_portfolio(
        [
            fill("open", at=ts(1), qty="10", price="100"),
            fill(
                "scale-out",
                at=ts(5),
                side=ExecutionSide.SELL,
                qty="9",
                price="120",
            ),
            fill(
                "dust-close",
                at=ts(20),
                side=ExecutionSide.SELL,
                qty="1",
                price="90",
            ),
        ]
    )

    report = build_period_report(portfolio, start_at=ts(15), end_at=ts(25))

    assert report.realized_pnl == Decimal("-10")
    assert report.trades_closed == 1
    assert report.wins == 1
    assert report.losses == 0
    assert report.closed_lifecycle_pnl == Decimal("170")


def test_open_lifecycle_partial_profit_counts_pnl_but_not_closed_trade() -> None:
    portfolio = project_portfolio(
        [
            fill("open", at=ts(1), qty="10", price="100"),
            fill(
                "partial",
                at=ts(2),
                side=ExecutionSide.SELL,
                qty="2",
                price="110",
            ),
        ]
    )

    report = build_period_report(portfolio)

    assert report.realized_pnl == Decimal("20")
    assert report.realization_count == 1
    assert report.trades_closed == 0
    assert report.open_positions == 1


def test_short_partial_and_full_close_use_short_pnl_direction() -> None:
    projection = project_account(
        "hl-main",
        [
            fill("short", at=ts(1), side=ExecutionSide.SELL, qty="5", price="100"),
            fill("buy-1", at=ts(2), side=ExecutionSide.BUY, qty="2", price="90"),
            fill("buy-2", at=ts(3), side=ExecutionSide.BUY, qty="3", price="80"),
        ],
    )

    assert projection.lifecycles[0].direction is PositionDirection.SHORT
    assert [r.net_pnl for r in projection.realizations] == [
        Decimal("20"),
        Decimal("60"),
    ]
    assert projection.lifecycles[0].realized_pnl == Decimal("80")


def test_reversal_closes_old_lifecycle_and_opens_a_new_one() -> None:
    projection = project_account(
        "hl-main",
        [
            fill("long", at=ts(1), qty="2", price="100"),
            fill(
                "reverse",
                at=ts(2),
                side=ExecutionSide.SELL,
                qty="3",
                price="110",
            ),
            fill("close-short", at=ts(3), side=ExecutionSide.BUY, qty="1", price="100"),
        ],
    )

    assert len(projection.lifecycles) == 2
    first, second = projection.lifecycles
    assert first.direction is PositionDirection.LONG
    assert first.realized_pnl == Decimal("20")
    assert first.closed_at == ts(2)
    assert second.direction is PositionDirection.SHORT
    assert second.opened_at == ts(2)
    assert second.realized_pnl == Decimal("10")
    assert all(item.status is LifecycleStatus.CLOSED for item in projection.lifecycles)
    assert [r.kind for r in projection.realizations] == ["REVERSAL_CLOSE", "FULL"]


def test_entry_and_exit_fees_are_allocated_to_realizations() -> None:
    projection = project_account(
        "hl-main",
        [
            fill("open", at=ts(1), qty="10", price="100", fee="10"),
            fill(
                "partial",
                at=ts(2),
                side=ExecutionSide.SELL,
                qty="4",
                price="110",
                fee="4",
            ),
            fill(
                "final",
                at=ts(3),
                side=ExecutionSide.SELL,
                qty="6",
                price="100",
                fee="6",
            ),
        ],
    )

    first, second = projection.realizations
    assert first.gross_pnl == Decimal("40")
    assert first.fees == Decimal("8")
    assert first.net_pnl == Decimal("32")
    assert second.gross_pnl == Decimal("0")
    assert second.fees == Decimal("12")
    assert second.net_pnl == Decimal("-12")
    assert projection.lifecycles[0].realized_pnl == Decimal("20")


def test_accounts_are_projected_independently_before_portfolio_composition() -> None:
    executions = [
        fill("a-open", account="account-a", qty="1", price="100"),
        fill(
            "a-close",
            account="account-a",
            at=ts(2),
            side=ExecutionSide.SELL,
            qty="1",
            price="110",
        ),
        fill(
            "b-short",
            account="account-b",
            at=ts(1),
            side=ExecutionSide.SELL,
            qty="1",
            price="100",
        ),
        fill(
            "b-close",
            account="account-b",
            at=ts(2),
            side=ExecutionSide.BUY,
            qty="1",
            price="120",
        ),
    ]

    portfolio = project_portfolio(executions)
    report = build_period_report(portfolio)

    assert set(portfolio.accounts) == {"account-a", "account-b"}
    assert len(portfolio.lifecycles) == 2
    assert report.realized_pnl == Decimal("-10")
    assert report.trades_closed == 2
    assert report.wins == 1
    assert report.losses == 1
    assert report.by_account["account-a"].realized_pnl == Decimal("10")
    assert report.by_account["account-b"].realized_pnl == Decimal("-20")


def test_portfolio_membership_filter_recalculates_without_deleting_history() -> None:
    executions = [
        fill("a-open", account="account-a", qty="1", price="100"),
        fill(
            "a-close",
            account="account-a",
            at=ts(2),
            side=ExecutionSide.SELL,
            qty="1",
            price="110",
        ),
        fill("b-open", account="account-b", qty="1", price="100"),
        fill(
            "b-close",
            account="account-b",
            at=ts(2),
            side=ExecutionSide.SELL,
            qty="1",
            price="130",
        ),
    ]

    all_accounts = project_portfolio(executions)
    only_a = project_portfolio(executions, included_accounts={"account-a"})

    assert build_period_report(all_accounts).realized_pnl == Decimal("40")
    assert build_period_report(only_a).realized_pnl == Decimal("10")
    assert len(all_accounts.executions) == 4
    assert len(only_a.executions) == 2


def test_explicit_long_and_short_position_sides_can_coexist() -> None:
    projection = project_account(
        "binance-main",
        [
            fill(
                "long-open",
                account="binance-main",
                market="binance:usdtm:BTCUSDT",
                position_side=PositionSide.LONG,
                side=ExecutionSide.BUY,
                qty="2",
                price="100",
            ),
            fill(
                "short-open",
                account="binance-main",
                market="binance:usdtm:BTCUSDT",
                position_side=PositionSide.SHORT,
                side=ExecutionSide.SELL,
                qty="3",
                price="100",
            ),
        ],
    )

    assert len(projection.open_positions) == 2
    assert {p.direction for p in projection.open_positions} == {
        PositionDirection.LONG,
        PositionDirection.SHORT,
    }


def test_period_end_is_exclusive() -> None:
    portfolio = project_portfolio(
        [
            fill("open", at=ts(1), qty="1", price="100"),
            fill(
                "close",
                at=ts(2),
                side=ExecutionSide.SELL,
                qty="1",
                price="110",
            ),
        ]
    )

    before = build_period_report(portfolio, start_at=ts(1), end_at=ts(2))
    from_close = build_period_report(portfolio, start_at=ts(2), end_at=ts(3))

    assert before.realized_pnl == Decimal("0")
    assert before.trades_closed == 0
    assert from_close.realized_pnl == Decimal("10")
    assert from_close.trades_closed == 1
