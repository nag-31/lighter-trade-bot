from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from architecture_v2 import ACCOUNTING_VERSION
from architecture_v2.application.queries import AccountingQueryService
from architecture_v2.domain.models import (
    Execution,
    ExecutionSide,
    PositionSide,
)
from architecture_v2.infrastructure.sqlite_store import SqliteV2Store


UTC = timezone.utc


def fill(
    native_id: str,
    *,
    account: str = "account-a",
    at: datetime | None = None,
    side: ExecutionSide = ExecutionSide.BUY,
    qty: str = "1",
    price: str = "100",
    fee: str = "0",
) -> Execution:
    return Execution.create(
        account_id=account,
        exchange="hyperliquid",
        market_key="hyperliquid:default:BTC",
        position_side=PositionSide.BOTH,
        native_trade_id=native_id,
        occurred_at=at or datetime(2026, 7, 1, tzinfo=UTC),
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
        fee=Decimal(fee),
    )


def test_init_creates_additive_versioned_schema(tmp_path) -> None:
    path = tmp_path / "v2.db"
    store = SqliteV2Store(path)

    store.init()

    with sqlite3.connect(path) as con:
        tables = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        version = con.execute(
            "SELECT value FROM v2_schema_meta WHERE key='schema_version'"
        ).fetchone()[0]

    assert {
        "v2_accounts",
        "v2_portfolios",
        "v2_portfolio_memberships",
        "v2_executions",
        "v2_realizations",
        "v2_lifecycles",
        "v2_projection_checkpoints",
        "v2_integration_outbox",
    }.issubset(tables)
    assert version == "1"


def test_ingest_is_atomic_idempotent_and_persists_projection(tmp_path) -> None:
    store = SqliteV2Store(tmp_path / "v2.db")
    store.init()
    opened = fill("open", qty="2", price="100", fee="0.2")
    closed = fill(
        "close",
        at=datetime(2026, 7, 2, tzinfo=UTC),
        side=ExecutionSide.SELL,
        qty="2",
        price="110",
        fee="0.2",
    )

    assert store.ingest_execution(opened)
    assert not store.ingest_execution(opened)
    assert store.ingest_execution(closed)
    assert not store.ingest_execution(closed)

    assert store.count("v2_executions") == 2
    assert store.count("v2_realizations") == 1
    assert store.count("v2_lifecycles") == 1
    assert store.count("v2_projection_checkpoints") == 1
    assert store.count("v2_integration_outbox") == 2

    realization = store.list_realizations(account_ids={"account-a"})[0]
    lifecycle = store.list_lifecycles(account_ids={"account-a"})[0]
    checkpoint = store.get_checkpoint("account-a")

    assert realization.net_pnl == Decimal("19.6")
    assert lifecycle.realized_pnl == Decimal("19.6")
    assert checkpoint is not None
    assert checkpoint.accounting_version == ACCOUNTING_VERSION
    assert checkpoint.last_execution_uid == closed.execution_uid


def test_execution_round_trip_preserves_decimal_enum_and_utc_time(tmp_path) -> None:
    store = SqliteV2Store(tmp_path / "v2.db")
    store.init()
    original = fill(
        "precise",
        at=datetime(2026, 7, 1, 12, 34, 56, 789123, tzinfo=UTC),
        qty="0.000000123456789",
        price="65432.123456789",
        fee="0.00000001",
    )

    store.ingest_execution(original)
    loaded = store.list_executions(account_ids={"account-a"})

    assert loaded == [original]


def test_new_accounts_join_default_portfolio_without_reenabling_removed_account(
    tmp_path,
) -> None:
    store = SqliteV2Store(tmp_path / "v2.db")
    store.init()
    store.ingest_execution(fill("a"))

    assert store.list_included_accounts("all") == {"account-a"}

    store.set_membership("all", "account-a", included=False)
    store.ingest_execution(
        fill(
            "a-next",
            at=datetime(2026, 7, 2, tzinfo=UTC),
            side=ExecutionSide.SELL,
        )
    )

    assert store.list_included_accounts("all") == set()
    assert len(store.list_executions(account_ids={"account-a"})) == 2


def test_portfolio_query_recalculates_membership_without_deleting_history(
    tmp_path,
) -> None:
    store = SqliteV2Store(tmp_path / "v2.db")
    store.init()
    query = AccountingQueryService(store)
    for execution in (
        fill("a-open", account="account-a"),
        fill(
            "a-close",
            account="account-a",
            at=datetime(2026, 7, 2, tzinfo=UTC),
            side=ExecutionSide.SELL,
            price="110",
        ),
        fill("b-open", account="account-b"),
        fill(
            "b-close",
            account="account-b",
            at=datetime(2026, 7, 2, tzinfo=UTC),
            side=ExecutionSide.SELL,
            price="130",
        ),
    ):
        store.ingest_execution(execution)

    assert query.period("all").realized_pnl == Decimal("40")

    store.set_membership("all", "account-b", included=False)
    without_b = query.period("all")

    assert without_b.realized_pnl == Decimal("10")
    assert set(without_b.by_account) == {"account-a"}
    assert store.count("v2_executions") == 4

    store.set_membership("all", "account-b", included=True)
    assert query.period("all").realized_pnl == Decimal("40")


def test_same_native_trade_id_is_safe_across_accounts(tmp_path) -> None:
    store = SqliteV2Store(tmp_path / "v2.db")
    store.init()

    assert store.ingest_execution(fill("same", account="account-a"))
    assert store.ingest_execution(fill("same", account="account-b"))

    assert store.count("v2_executions") == 2


def test_conflicting_reuse_of_execution_uid_is_rejected(tmp_path) -> None:
    store = SqliteV2Store(tmp_path / "v2.db")
    store.init()
    original = fill("same-fact", price="100")
    conflicting = Execution(
        execution_uid=original.execution_uid,
        account_id=original.account_id,
        exchange=original.exchange,
        market_key=original.market_key,
        position_side=original.position_side,
        native_trade_id=original.native_trade_id,
        occurred_at=original.occurred_at,
        side=original.side,
        quantity=original.quantity,
        price=Decimal("101"),
        fee=original.fee,
    )

    assert store.ingest_execution(original)
    try:
        store.ingest_execution(conflicting)
    except ValueError as exc:
        assert "collision" in str(exc).lower()
    else:
        raise AssertionError("conflicting execution UID should fail")

    assert store.list_executions(account_ids={"account-a"}) == [original]
    assert store.count("v2_integration_outbox") == 1


def test_late_older_execution_rebuilds_account_in_event_time_order(tmp_path) -> None:
    store = SqliteV2Store(tmp_path / "v2.db")
    store.init()
    opened = fill(
        "open",
        at=datetime(2026, 7, 1, tzinfo=UTC),
        qty="2",
        price="100",
    )
    final = fill(
        "final",
        at=datetime(2026, 7, 3, tzinfo=UTC),
        side=ExecutionSide.SELL,
        qty="1",
        price="120",
    )
    late_partial = fill(
        "late-partial",
        at=datetime(2026, 7, 2, tzinfo=UTC),
        side=ExecutionSide.SELL,
        qty="1",
        price="110",
    )

    store.ingest_execution(opened)
    store.ingest_execution(final)
    store.ingest_execution(late_partial)

    realizations = store.list_realizations(account_ids={"account-a"})
    lifecycle = store.list_lifecycles(account_ids={"account-a"})[0]
    checkpoint = store.get_checkpoint("account-a")

    assert [item.net_pnl for item in realizations] == [
        Decimal("10"),
        Decimal("20"),
    ]
    assert lifecycle.realized_pnl == Decimal("30")
    assert lifecycle.closed_at == datetime(2026, 7, 3, tzinfo=UTC)
    # Projection checkpoint describes the latest event-time execution even
    # though the last ingested fact was older.
    assert checkpoint is not None
    assert checkpoint.last_execution_uid == final.execution_uid


def test_outbox_payload_is_traceable_and_claim_delivery_is_idempotent(tmp_path) -> None:
    store = SqliteV2Store(tmp_path / "v2.db")
    store.init()
    execution = fill("trace")
    store.ingest_execution(execution)

    pending = store.pending_outbox(limit=10)

    assert len(pending) == 1
    payload = json.loads(pending[0].payload_json)
    assert payload["account_id"] == "account-a"
    assert payload["execution_uid"] == execution.execution_uid
    assert payload["accounting_version"] == ACCOUNTING_VERSION

    assert store.mark_outbox_delivered(pending[0].event_uid)
    assert not store.mark_outbox_delivered(pending[0].event_uid)
    assert store.pending_outbox(limit=10) == []


def test_failed_projection_rolls_back_execution_and_outbox(tmp_path) -> None:
    store = SqliteV2Store(tmp_path / "v2.db")
    store.init()
    # An explicit LONG-side SELL cannot open a valid LONG position. The domain
    # projector must reject it and the storage transaction must remain empty.
    invalid = Execution.create(
        account_id="account-a",
        exchange="binance",
        market_key="binance:usdtm:BTCUSDT",
        position_side=PositionSide.LONG,
        native_trade_id="invalid-open",
        occurred_at=datetime(2026, 7, 1, tzinfo=UTC),
        side=ExecutionSide.SELL,
        quantity=Decimal("1"),
        price=Decimal("100"),
    )

    try:
        store.ingest_execution(invalid)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid projection should fail")

    assert store.count("v2_executions") == 0
    assert store.count("v2_integration_outbox") == 0


def test_empty_portfolio_returns_zero_report(tmp_path) -> None:
    store = SqliteV2Store(tmp_path / "v2.db")
    store.init()

    report = AccountingQueryService(store).period("all")

    assert report.realized_pnl == Decimal("0")
    assert report.trades_closed == 0
    assert report.by_account == {}
