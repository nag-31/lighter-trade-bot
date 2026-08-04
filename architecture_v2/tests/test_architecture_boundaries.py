from datetime import datetime, timezone
from decimal import Decimal
import sqlite3

from architecture_v2.application.read_models import read_dashboard, read_journal
from architecture_v2.domain.models import Execution, ExecutionSide, PositionSide
from architecture_v2.domain.policy import (
    AccountState,
    ProjectionWindow,
    ProjectionRunPolicy,
    RunMode,
)
from architecture_v2.domain.projections import projection_hash
from architecture_v2.infrastructure.account_ledger_store import AccountLedgerStore
from architecture_v2.infrastructure.catalog_store import CatalogStore
from architecture_v2.infrastructure.sqlite_store import SqliteV2Store
from architecture_v2.infrastructure.verification import backup_sqlite, restore_sqlite
from architecture_v2.infrastructure.rollout import evaluate_rollout_gate


UTC = timezone.utc


def execution(native: str, *, account: str = "hl-main", at=None, side=ExecutionSide.BUY):
    return Execution.create(
        account_id=account,
        exchange="hyperliquid",
        market_key="hyperliquid:default:BTC",
        position_side=PositionSide.BOTH,
        native_trade_id=native,
        occurred_at=at or datetime(2026, 7, 1, tzinfo=UTC),
        side=side,
        quantity=Decimal("1"),
        price=Decimal("100" if side is ExecutionSide.BUY else "110"),
    )


def test_catalog_preserves_identity_and_label_history(tmp_path):
    catalog = CatalogStore(tmp_path / "catalog.db")
    catalog.init()
    catalog.register_account("hl-main", exchange="hyperliquid", label="HL", at=datetime(2026, 7, 1, tzinfo=UTC))
    catalog.rename_account(
        "hl-main", "HL Swing Wallet",
        at=datetime(2026, 7, 2, tzinfo=UTC),
        changed_by="owner", change_reason="clarify wallet role",
    )
    catalog.set_state(
        "hl-main", ingestion_enabled=True, alerts_enabled=False,
        portfolio_included=False, historical_visible=True,
    )

    account = catalog.get_account("hl-main")
    assert account is not None
    assert account.label == "HL Swing Wallet"
    assert account.state == AccountState(
        ingestion_enabled=True, alerts_enabled=False,
        portfolio_included=False, historical_visible=True,
    )
    assert catalog.label_at("hl-main", datetime(2026, 7, 1, tzinfo=UTC)) == "HL"
    assert catalog.label_at("hl-main", datetime(2026, 7, 3, tzinfo=UTC)) == "HL Swing Wallet"


def test_account_ledger_is_append_only_and_observes_repeated_payloads(tmp_path):
    ledger = AccountLedgerStore(tmp_path / "data")
    first = execution("1")
    assert ledger.append(first, raw_payload={"version": 1})
    assert not ledger.append(first, raw_payload={"version": 1})
    assert not ledger.append(first, raw_payload={"version": 2})
    with sqlite3.connect(ledger.path("hl-main")) as con:
        assert con.execute("SELECT COUNT(*) FROM exchange_fills").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM fill_observations").fetchone()[0] == 2


def test_non_live_runs_rebuild_without_outbox_and_record_cutoff(tmp_path):
    store = SqliteV2Store(tmp_path / "v2.db")
    store.init()
    assert store.ingest_execution(
        execution("1"),
        run_policy=ProjectionRunPolicy(mode=RunMode.BACKFILL),
    )
    assert store.pending_outbox() == []
    run = store.latest_projection_run("hl-main")
    assert run is not None
    assert run.mode is RunMode.BACKFILL
    assert run.alerts_created == 0
    assert run.window.report_start == datetime(2026, 6, 1, tzinfo=UTC)


def test_dashboard_and_journal_are_read_only_and_use_catalog_state(tmp_path):
    store = SqliteV2Store(tmp_path / "v2.db")
    store.init()
    store.ingest_execution(execution("open"))
    store.ingest_execution(
        execution("close", at=datetime(2026, 7, 2, tzinfo=UTC), side=ExecutionSide.SELL)
    )
    store.catalog.rename_account("hl-main", "HL Swing Wallet")
    dashboard = read_dashboard(store)
    journal = read_journal(store)
    assert dashboard.accounts[0].label == "HL Swing Wallet"
    assert dashboard.report.realized_pnl == Decimal("10")
    assert journal.lifecycles[0].holding_duration_ms == 86_400_000
    assert journal.lifecycles[0].lifecycle_uid


def test_projection_hash_and_sqlite_backup_restore_are_deterministic(tmp_path):
    store = SqliteV2Store(tmp_path / "v2.db")
    store.init()
    store.ingest_execution(execution("1"))
    projection = __import__("architecture_v2.domain.accounting", fromlist=["project_account"]).project_account(
        "hl-main", store.list_executions(account_ids={"hl-main"})
    )
    assert projection_hash(projection) == projection_hash(projection)
    evidence = backup_sqlite(tmp_path / "v2.db", tmp_path / "backup" / "v2.db")
    restored = restore_sqlite(tmp_path / "backup" / "v2.db", tmp_path / "restore" / "v2.db")
    assert evidence.integrity == restored.integrity == "ok"
    assert evidence.sha256 == restored.sha256



def test_rollout_gate_requires_shadow_parity_and_restore_evidence(tmp_path):
    store = SqliteV2Store(tmp_path / "v2.db")
    store.init()
    store.ingest_execution(
        execution("1"), run_policy=ProjectionRunPolicy(mode=RunMode.SHADOW)
    )
    manifest = store.latest_projection_run("hl-main")
    assert manifest is not None
    blocked = evaluate_rollout_gate(manifest)
    assert not blocked.passed
    assert "replay hash is not stable" in blocked.failures
    passed = evaluate_rollout_gate(
        manifest,
        shadow_summary={"MATCH": 4, "UNEXPLAINED": 0},
        backup_integrity="ok",
        restored_integrity="ok",
        replay_hash_stable=True,
        consumer_approved=True,
    )
    assert passed.passed

def test_catalog_alert_and_ingestion_controls_are_independent(tmp_path):
    store = SqliteV2Store(tmp_path / "v2.db")
    store.init()
    store.catalog.register_account(
        "hl-main", exchange="hyperliquid", label="HL",
        state=AccountState(alerts_enabled=False),
    )
    assert store.ingest_execution(execution("silent"))
    assert store.pending_outbox() == []
    store.catalog.set_state("hl-main", ingestion_enabled=False)
    try:
        store.ingest_execution(execution("blocked"))
    except ValueError as exc:
        assert "ingestion disabled" in str(exc)
    else:
        raise AssertionError("disabled ingestion must reject new facts")