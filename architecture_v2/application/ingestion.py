"""Ledger-first ingestion and projection recovery orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from architecture_v2.domain.models import Execution
from architecture_v2.domain.policy import ProjectionRunPolicy, ProjectionWindow, RunMode
from architecture_v2.infrastructure.account_ledger_store import AccountLedgerStore
from architecture_v2.infrastructure.catalog_store import CatalogStore
from architecture_v2.infrastructure.sqlite_store import SqliteV2Store


@dataclass(frozen=True, slots=True)
class IngestionResult:
    execution_uid: str
    ledger_appended: bool
    projection_updated: bool
    mode: RunMode


@dataclass(frozen=True, slots=True)
class AccountDrift:
    account_id: str
    ledger_count: int
    projection_count: int
    missing_from_projection: tuple[str, ...]
    extra_in_projection: tuple[str, ...]

    @property
    def matches(self) -> bool:
        return not self.missing_from_projection and not self.extra_in_projection


class ProjectionLagError(RuntimeError):
    """The raw fact is durable, but its rebuildable projection failed."""

    def __init__(self, execution_uid: str, cause: Exception):
        super().__init__(
            f"execution {execution_uid} is durable in its account ledger but "
            f"projection failed: {cause}"
        )
        self.execution_uid = execution_uid
        self.__cause__ = cause


class V2IngestionService:
    """Coordinate catalog policy, immutable facts, and rebuildable projections."""

    def __init__(
        self,
        *,
        catalog: CatalogStore,
        ledgers: AccountLedgerStore,
        projections: SqliteV2Store,
    ):
        if catalog.path.resolve() != projections.catalog.path.resolve():
            raise ValueError("ingestion service and projection store must share one catalog")
        self.catalog = catalog
        self.ledgers = ledgers
        self.projections = projections

    def ingest(
        self,
        execution: Execution,
        *,
        raw_payload: Any | None = None,
        run_policy: ProjectionRunPolicy | None = None,
        window: ProjectionWindow | None = None,
    ) -> IngestionResult:
        policy = run_policy or ProjectionRunPolicy()
        account = self.catalog.get_account(execution.account_id)
        if account is None:
            account = self.catalog.register_account(
                execution.account_id,
                exchange=execution.exchange,
                label=execution.account_id,
            )
        if not account.state.ingestion_enabled:
            raise ValueError(f"ingestion disabled for account: {execution.account_id}")

        ledger_appended = self.ledgers.append(
            execution,
            raw_payload=raw_payload,
            mode=policy.mode,
        )
        try:
            projection_updated = self.projections.ingest_execution(
                execution,
                run_policy=policy,
                window=window,
            )
        except Exception as exc:
            raise ProjectionLagError(execution.execution_uid, exc) from exc
        return IngestionResult(
            execution_uid=execution.execution_uid,
            ledger_appended=ledger_appended,
            projection_updated=projection_updated,
            mode=policy.mode,
        )

    def audit_account(self, account_id: str) -> AccountDrift:
        ledger = self.ledgers.list_executions(account_id)
        projected = self.projections.list_executions(account_ids={account_id})
        ledger_ids = {item.execution_uid for item in ledger}
        projection_ids = {item.execution_uid for item in projected}
        return AccountDrift(
            account_id=account_id,
            ledger_count=len(ledger_ids),
            projection_count=len(projection_ids),
            missing_from_projection=tuple(sorted(ledger_ids - projection_ids)),
            extra_in_projection=tuple(sorted(projection_ids - ledger_ids)),
        )

    def repair_projection(
        self,
        account_id: str,
        *,
        window: ProjectionWindow | None = None,
    ) -> AccountDrift:
        before = self.audit_account(account_id)
        if before.extra_in_projection:
            raise ValueError(
                "projection contains executions absent from the immutable ledger: "
                + ", ".join(before.extra_in_projection)
            )
        missing = set(before.missing_from_projection)
        if not missing:
            return before
        for execution in self.ledgers.list_executions(account_id):
            if execution.execution_uid in missing:
                self.projections.ingest_execution(
                    execution,
                    run_policy=ProjectionRunPolicy(mode=RunMode.REPAIR),
                    window=window,
                )
        return self.audit_account(account_id)
