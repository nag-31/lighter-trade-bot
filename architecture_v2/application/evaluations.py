from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping

from architecture_v2.domain.models import (
    LifecycleStatus,
    PortfolioProjection,
)
from architecture_v2.domain.reports import AccountingPeriodReport


@dataclass(frozen=True, slots=True)
class EvaluationIssue:
    code: str
    subject_uid: str
    message: str


@dataclass(frozen=True, slots=True)
class ProjectionEvaluation:
    errors: tuple[EvaluationIssue, ...]
    checked_accounts: int
    checked_executions: int
    checked_lifecycles: int
    checked_realizations: int

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True, slots=True)
class MetricDelta:
    metric: str
    legacy_value: Decimal | int | None
    v2_value: Decimal | int


@dataclass(frozen=True, slots=True)
class ShadowComparison:
    deltas: tuple[MetricDelta, ...]

    @property
    def matches(self) -> bool:
        return not self.deltas


def _duplicates(values: list[str]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


def evaluate_portfolio(portfolio: PortfolioProjection) -> ProjectionEvaluation:
    """Validate references and composition without recalculating trading PnL."""
    issues: list[EvaluationIssue] = []
    execution_ids = {item.execution_uid for item in portfolio.executions}
    lifecycle_by_id = {
        item.lifecycle_uid: item for item in portfolio.lifecycles
    }
    realization_by_id = {
        item.realization_uid: item for item in portfolio.realizations
    }

    for kind, identifiers in (
        (
            "execution",
            [item.execution_uid for item in portfolio.executions],
        ),
        (
            "lifecycle",
            [item.lifecycle_uid for item in portfolio.lifecycles],
        ),
        (
            "realization",
            [item.realization_uid for item in portfolio.realizations],
        ),
    ):
        for duplicate in sorted(_duplicates(identifiers)):
            issues.append(
                EvaluationIssue(
                    code=f"duplicate_{kind}_uid",
                    subject_uid=duplicate,
                    message=f"{kind} UID occurs more than once",
                )
            )

    for realization in portfolio.realizations:
        if realization.execution_uid not in execution_ids:
            issues.append(
                EvaluationIssue(
                    code="unknown_realization_execution",
                    subject_uid=realization.realization_uid,
                    message=(
                        f"execution {realization.execution_uid} is not present"
                    ),
                )
            )
        lifecycle = lifecycle_by_id.get(realization.lifecycle_uid)
        if lifecycle is None:
            issues.append(
                EvaluationIssue(
                    code="unknown_realization_lifecycle",
                    subject_uid=realization.realization_uid,
                    message=(
                        f"lifecycle {realization.lifecycle_uid} is not present"
                    ),
                )
            )
        elif lifecycle.account_id != realization.account_id:
            issues.append(
                EvaluationIssue(
                    code="realization_account_mismatch",
                    subject_uid=realization.realization_uid,
                    message="realization and lifecycle belong to different accounts",
                )
            )

    open_position_lifecycles = {
        item.lifecycle_uid for item in portfolio.open_positions
    }
    for lifecycle in portfolio.lifecycles:
        linked = [
            realization_by_id[uid]
            for uid in lifecycle.realization_uids
            if uid in realization_by_id
        ]
        missing = set(lifecycle.realization_uids) - realization_by_id.keys()
        for missing_uid in sorted(missing):
            issues.append(
                EvaluationIssue(
                    code="unknown_lifecycle_realization",
                    subject_uid=lifecycle.lifecycle_uid,
                    message=f"realization {missing_uid} is not present",
                )
            )
        linked_total = sum(
            (item.net_pnl for item in linked),
            Decimal("0"),
        )
        if linked_total != lifecycle.realized_pnl:
            issues.append(
                EvaluationIssue(
                    code="lifecycle_pnl_mismatch",
                    subject_uid=lifecycle.lifecycle_uid,
                    message=(
                        f"realizations total {linked_total}, "
                        f"lifecycle stores {lifecycle.realized_pnl}"
                    ),
                )
            )
        is_open_position = lifecycle.lifecycle_uid in open_position_lifecycles
        if lifecycle.status is LifecycleStatus.OPEN and not is_open_position:
            issues.append(
                EvaluationIssue(
                    code="open_lifecycle_without_position",
                    subject_uid=lifecycle.lifecycle_uid,
                    message="open lifecycle has no open position",
                )
            )
        if lifecycle.status is LifecycleStatus.CLOSED and is_open_position:
            issues.append(
                EvaluationIssue(
                    code="closed_lifecycle_has_position",
                    subject_uid=lifecycle.lifecycle_uid,
                    message="closed lifecycle is still represented as open",
                )
            )

    account_executions = tuple(
        item
        for account in portfolio.accounts.values()
        for item in account.executions
    )
    account_lifecycles = tuple(
        item
        for account in portfolio.accounts.values()
        for item in account.lifecycles
    )
    account_realizations = tuple(
        item
        for account in portfolio.accounts.values()
        for item in account.realizations
    )
    account_positions = tuple(
        item
        for account in portfolio.accounts.values()
        for item in account.open_positions
    )
    aggregate_checks = (
        (
            "portfolio_execution_drift",
            portfolio.executions,
            account_executions,
        ),
        (
            "portfolio_lifecycle_drift",
            portfolio.lifecycles,
            account_lifecycles,
        ),
        (
            "portfolio_realization_drift",
            portfolio.realizations,
            account_realizations,
        ),
        (
            "portfolio_open_position_drift",
            portfolio.open_positions,
            account_positions,
        ),
    )
    for code, aggregate, composed in aggregate_checks:
        if aggregate != composed:
            issues.append(
                EvaluationIssue(
                    code=code,
                    subject_uid="portfolio",
                    message="portfolio aggregate differs from account projections",
                )
            )

    return ProjectionEvaluation(
        errors=tuple(issues),
        checked_accounts=len(portfolio.accounts),
        checked_executions=len(portfolio.executions),
        checked_lifecycles=len(portfolio.lifecycles),
        checked_realizations=len(portfolio.realizations),
    )


def _decimal(value: object) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        converted = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return converted if str(converted) == str(value) else None


def compare_shadow_metrics(
    v2_report: AccountingPeriodReport,
    legacy_metrics: Mapping[str, object],
) -> ShadowComparison:
    """Compare published metrics during shadow mode without writing either side."""
    expected: tuple[tuple[str, Decimal | int], ...] = (
        ("realized_pnl", v2_report.realized_pnl),
        ("trades_closed", v2_report.trades_closed),
        ("wins", v2_report.wins),
        ("losses", v2_report.losses),
    )
    deltas: list[MetricDelta] = []
    for metric, v2_value in expected:
        raw = legacy_metrics.get(metric)
        legacy_value = (
            _decimal(raw) if isinstance(v2_value, Decimal) else _integer(raw)
        )
        if legacy_value != v2_value:
            deltas.append(
                MetricDelta(
                    metric=metric,
                    legacy_value=legacy_value,
                    v2_value=v2_value,
                )
            )
    return ShadowComparison(deltas=tuple(deltas))
