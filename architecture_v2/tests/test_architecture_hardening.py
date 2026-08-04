from datetime import datetime, timezone
from decimal import Decimal
import sqlite3

import pytest

from architecture_v2.application.ingestion import (
    ProjectionLagError,
    V2IngestionService,
)
from architecture_v2.domain.models import Execution, ExecutionSide, PositionSide
from architecture_v2.domain.policy import AccountState, ProjectionRunPolicy, RunMode
from architecture_v2.infrastructure.account_ledger_store import AccountLedgerStore
from architecture_v2.infrastructure.catalog_store import CatalogStore
from architecture_v2.infrastructure.sqlite_store import SqliteV2Store


UTC = timezone.utc


def fill(
    native_id: str,
    *,
    at: datetime | None = None,
    side: ExecutionSide = ExecutionSide.BUY,
    position_side: PositionSide = PositionSide.BOTH,
) -> Execution:
    return Execution.create(
        account_id="hl-main",
        exchange="hyperliquid",
        market_key="hyperliquid:default:BTC",
        position_side=position_side,
        native_trade_id=native_id,
        occurred_at=at or datetime(2026, 7, 1, tzinfo=UTC),
        side=side,
        quantity=Decimal("1"),
        price=Decimal("100"),
    )


def service(tmp_path):
    catalog = CatalogStore(tmp_path / "catalog.db")
    projections = SqliteV2Store(
        tmp_path / "projections.db", catalog_path=catalog.path
    )
    projections.init()
    return V2IngestionService(
        catalog=catalog,
        ledgers=AccountLedgerStore(tmp_path / "ledgers"),
        projections=projections,
    )


def test_account_state_history_records_only_real_transitions(tmp_path):
    catalog = CatalogStore(tmp_path / "catalog.db")
    catalog.init()
    catalog.register_account(
        "hl-main",
        exchange="hyperliquid",
        label="HL",
        state=AccountState(),
        at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    assert len(catalog.state_history("hl-main")) == 4

    catalog.set_state(
        "hl-main",
        alerts_enabled=False,
        at=datetime(2026, 7, 2, tzinfo=UTC),
        changed_by="owner",
        change_reason="mute alerts",
    )
    catalog.set_state(
        "hl-main",
        alerts_enabled=False,
        at=datetime(2026, 7, 3, tzinfo=UTC),
        changed_by="owner",
        change_reason="idempotent retry",
    )
    history = catalog.state_history("hl-main")
    assert len(history) == 5
    assert history[-1].field_name == "alerts_enabled"
    assert history[-1].old_value is True
    assert history[-1].new_value is False
    assert history[-1].changed_by == "owner"


def test_ledger_first_ingestion_audits_and_repairs_projection_lag(tmp_path):
    app = service(tmp_path)
    first = fill("first")
    result = app.ingest(
        first,
        run_policy=ProjectionRunPolicy(mode=RunMode.BACKFILL),
    )
    assert result.ledger_appended and result.projection_updated
    assert app.audit_account("hl-main").matches
    assert app.projections.pending_outbox() == []

    second = fill("second", at=datetime(2026, 7, 2, tzinfo=UTC))
    app.ledgers.append(second, mode=RunMode.BACKFILL)
    drift = app.audit_account("hl-main")
    assert drift.missing_from_projection == (second.execution_uid,)
    repaired = app.repair_projection("hl-main")
    assert repaired.matches
    assert app.projections.pending_outbox() == []


def test_projection_failure_keeps_raw_fact_for_repair(tmp_path):
    app = service(tmp_path)
    invalid = fill(
        "invalid-long-open",
        side=ExecutionSide.SELL,
        position_side=PositionSide.LONG,
    )
    with pytest.raises(ProjectionLagError):
        app.ingest(invalid)
    assert [item.execution_uid for item in app.ledgers.list_executions("hl-main")] == [
        invalid.execution_uid
    ]
    assert app.projections.list_executions(account_ids={"hl-main"}) == []


def test_ledger_observation_records_run_mode(tmp_path):
    ledgers = AccountLedgerStore(tmp_path / "ledgers")
    execution = fill("backfilled")
    ledgers.append(execution, mode=RunMode.BACKFILL)
    with sqlite3.connect(ledgers.path("hl-main")) as con:
        assert con.execute(
            "SELECT run_mode FROM fill_observations"
        ).fetchone()[0] == "BACKFILL"


def test_projection_run_and_shadow_evidence_cannot_be_overwritten(tmp_path):
    store = SqliteV2Store(tmp_path / "projections.db")
    store.init()
    policy = ProjectionRunPolicy(mode=RunMode.SHADOW)
    store.ingest_execution(fill("one"), run_policy=policy, run_id="fixed-run")

    with pytest.raises(ValueError, match="projection run ID collision"):
        store.ingest_execution(
            fill("two", at=datetime(2026, 7, 2, tzinfo=UTC)),
            run_policy=policy,
            run_id="fixed-run",
        )
    assert store.count("v2_executions") == 1

    comparison = {
        "dimension": "realized_pnl",
        "subject_uid": "2026-07-01",
        "classification": "MATCH",
        "legacy_value": "0",
        "v2_value": "0",
    }
    assert store.record_shadow_comparisons(
        "fixed-run", [comparison], account_id="hl-main"
    ) == 1
    assert store.record_shadow_comparisons(
        "fixed-run", [comparison], account_id="hl-main"
    ) == 0
    conflicting = dict(comparison, classification="UNEXPLAINED")
    with pytest.raises(ValueError, match="shadow comparison ID collision"):
        store.record_shadow_comparisons(
            "fixed-run", [conflicting], account_id="hl-main"
        )
    assert store.shadow_summary("fixed-run") == {"MATCH": 1}
