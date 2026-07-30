from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from types import MappingProxyType

from architecture_v2.application.evaluations import (
    compare_shadow_metrics,
    evaluate_portfolio,
)
from architecture_v2.application.portfolio import project_portfolio
from architecture_v2.domain.models import Execution, ExecutionSide, PositionSide
from architecture_v2.domain.reports import build_period_report


UTC = timezone.utc


def fill(
    native_id: str,
    *,
    account: str = "main",
    day: int,
    side: ExecutionSide,
    quantity: str,
    price: str,
) -> Execution:
    return Execution.create(
        account_id=account,
        exchange="hyperliquid",
        market_key="hyperliquid:default:BTC",
        position_side=PositionSide.BOTH,
        native_trade_id=native_id,
        occurred_at=datetime(2026, 7, day, tzinfo=UTC),
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(price),
    )


def valid_portfolio():
    return project_portfolio(
        [
            fill(
                "open",
                day=1,
                side=ExecutionSide.BUY,
                quantity="10",
                price="100",
            ),
            fill(
                "partial",
                day=2,
                side=ExecutionSide.SELL,
                quantity="4",
                price="110",
            ),
            fill(
                "final",
                day=3,
                side=ExecutionSide.SELL,
                quantity="6",
                price="120",
            ),
        ]
    )


def test_valid_projection_passes_every_invariant() -> None:
    result = evaluate_portfolio(valid_portfolio())

    assert result.ok
    assert result.errors == ()
    assert result.checked_accounts == 1
    assert result.checked_executions == 3
    assert result.checked_lifecycles == 1
    assert result.checked_realizations == 2


def test_evaluator_detects_lifecycle_total_drift() -> None:
    portfolio = valid_portfolio()
    broken_lifecycle = replace(
        portfolio.lifecycles[0],
        realized_pnl=Decimal("999"),
    )
    broken_account = replace(
        portfolio.accounts["main"],
        lifecycles=(broken_lifecycle,),
    )
    broken = replace(
        portfolio,
        accounts=MappingProxyType({"main": broken_account}),
        lifecycles=(broken_lifecycle,),
    )

    result = evaluate_portfolio(broken)

    assert not result.ok
    assert any(issue.code == "lifecycle_pnl_mismatch" for issue in result.errors)


def test_evaluator_detects_broken_realization_reference() -> None:
    portfolio = valid_portfolio()
    broken_realization = replace(
        portfolio.realizations[0],
        execution_uid="missing-execution",
    )
    broken_account = replace(
        portfolio.accounts["main"],
        realizations=(broken_realization, portfolio.realizations[1]),
    )
    broken = replace(
        portfolio,
        accounts=MappingProxyType({"main": broken_account}),
        realizations=(broken_realization, portfolio.realizations[1]),
    )

    result = evaluate_portfolio(broken)

    assert any(
        issue.code == "unknown_realization_execution" for issue in result.errors
    )


def test_evaluator_detects_portfolio_account_aggregate_drift() -> None:
    portfolio = valid_portfolio()
    broken = replace(portfolio, realizations=portfolio.realizations[:1])

    result = evaluate_portfolio(broken)

    assert any(
        issue.code == "portfolio_realization_drift" for issue in result.errors
    )


def test_shadow_comparison_uses_named_accounting_contract_metrics() -> None:
    report = build_period_report(valid_portfolio())

    comparison = compare_shadow_metrics(
        report,
        {
            "realized_pnl": "160",
            "trades_closed": 1,
            "wins": 1,
            "losses": 0,
        },
    )

    assert comparison.matches
    assert comparison.deltas == ()


def test_shadow_comparison_reports_each_difference_without_mutating_state() -> None:
    report = build_period_report(valid_portfolio())

    comparison = compare_shadow_metrics(
        report,
        {
            "realized_pnl": "999",
            "trades_closed": 2,
            "wins": 0,
            "losses": 1,
        },
    )

    assert not comparison.matches
    assert {delta.metric for delta in comparison.deltas} == {
        "realized_pnl",
        "trades_closed",
        "wins",
        "losses",
    }


def test_shadow_comparison_rejects_missing_required_metrics() -> None:
    report = build_period_report(valid_portfolio())

    comparison = compare_shadow_metrics(report, {"realized_pnl": "160"})

    assert not comparison.matches
    assert {delta.metric for delta in comparison.deltas} == {
        "trades_closed",
        "wins",
        "losses",
    }
    assert all(delta.legacy_value is None for delta in comparison.deltas)
