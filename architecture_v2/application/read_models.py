"""Read-only adapters for Dashboard and Trade Journal consumers.

These adapters deliberately return immutable snapshots.  They do not import
the legacy web applications and cannot mutate ledger, projection, or Journal
annotation state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from architecture_v2.application.queries import AccountingQueryService
from architecture_v2.domain.models import Lifecycle, Realization
from architecture_v2.domain.policy import ProjectionWindow
from architecture_v2.domain.reports import AccountingPeriodReport
from architecture_v2.infrastructure.catalog_store import CatalogStore
from architecture_v2.infrastructure.sqlite_store import SqliteV2Store


@dataclass(frozen=True, slots=True)
class AccountReadModel:
    account_id: str
    label: str
    exchange: str
    portfolio_included: bool
    historical_visible: bool


@dataclass(frozen=True, slots=True)
class DashboardReadModel:
    report: AccountingPeriodReport
    accounts: tuple[AccountReadModel, ...]
    window: ProjectionWindow


@dataclass(frozen=True, slots=True)
class JournalLifecycleReadModel:
    lifecycle_uid: str
    account_id: str
    market_key: str
    direction: str
    opened_at: datetime
    closed_at: datetime | None
    realized_pnl: Decimal
    holding_duration_ms: int | None
    holding_duration_basis: str
    execution_uids: tuple[str, ...]
    realization_uids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class JournalReadModel:
    lifecycles: tuple[JournalLifecycleReadModel, ...]
    realizations: tuple[Realization, ...]


def read_dashboard(
    store: SqliteV2Store,
    catalog: CatalogStore | None = None,
    *,
    portfolio_id: str = "all",
    window: ProjectionWindow | None = None,
) -> DashboardReadModel:
    """Build a Dashboard snapshot without writing any state."""
    selected_window = window or ProjectionWindow.default()
    report = AccountingQueryService(store).period(
        portfolio_id,
        start_at=selected_window.report_start,
        end_at=selected_window.report_end,
        timezone=selected_window.timezone,
    )
    source = catalog or store.catalog
    accounts = tuple(
        AccountReadModel(
            account_id=item.account_id,
            label=item.label,
            exchange=item.exchange,
            portfolio_included=item.state.portfolio_included,
            historical_visible=item.state.historical_visible,
        )
        for item in source.list_accounts(historical_visible=True)
    )
    return DashboardReadModel(report=report, accounts=accounts, window=selected_window)


def read_journal(
    store: SqliteV2Store,
    *,
    account_ids: set[str] | frozenset[str] | None = None,
    catalog: CatalogStore | None = None,
) -> JournalReadModel:
    """Build Journal lifecycle links using immutable Tracker UIDs."""
    source = catalog or store.catalog
    visible = {item.account_id for item in source.list_accounts(historical_visible=True)}
    selected = visible if account_ids is None else visible.intersection(account_ids)
    lifecycles = tuple(
        JournalLifecycleReadModel(
            lifecycle_uid=item.lifecycle_uid,
            account_id=item.account_id,
            market_key=item.market_key,
            direction=item.direction.value,
            opened_at=item.opened_at,
            closed_at=item.closed_at,
            realized_pnl=item.realized_pnl,
            holding_duration_ms=item.holding_duration_ms,
            holding_duration_basis=item.holding_duration_basis,
            execution_uids=item.execution_uids,
            realization_uids=item.realization_uids,
        )
        for item in store.list_lifecycles(account_ids=selected)
    )
    return JournalReadModel(
        lifecycles=lifecycles,
        realizations=tuple(store.list_realizations(account_ids=selected)),
    )
