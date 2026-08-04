from __future__ import annotations

from datetime import datetime

from architecture_v2.application.portfolio import project_portfolio
from architecture_v2.domain.policy import ProjectionWindow
from architecture_v2.domain.reports import (
    AccountingPeriodReport,
    build_period_report,
)
from architecture_v2.infrastructure.sqlite_store import SqliteV2Store


class AccountingQueryService:
    """The single period-query handler for all future V2 consumers."""

    def __init__(self, store: SqliteV2Store):
        self.store = store

    def period(
        self,
        portfolio_id: str,
        *,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        timezone: str = "UTC",
        window: ProjectionWindow | None = None,
    ) -> AccountingPeriodReport:
        selected_window = window or ProjectionWindow(
            report_start=start_at or ProjectionWindow.default().report_start,
            report_end=end_at,
            timezone=timezone,
        )
        account_ids = self.store.list_included_accounts(portfolio_id)
        executions = self.store.list_executions(account_ids=account_ids)
        portfolio = project_portfolio(
            executions,
            included_accounts=account_ids,
        )
        return build_period_report(
            portfolio,
            start_at=selected_window.report_start,
            end_at=selected_window.report_end,
            timezone=selected_window.timezone,
        )
