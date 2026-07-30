from __future__ import annotations

import asyncio
import sqlite3
from decimal import Decimal

from src.canonical_pnl import (
    backfill_canonical_ledger,
    load_canonical_realizations,
    project_portfolio,
    sync_portfolio_membership,
)
from src.db import (
    delete_closed_trades_by_identity_since,
    init_db,
    save_closed_trade,
)


def run(coro):
    return asyncio.run(coro)


def record(
    *,
    source_id: str,
    source: str,
    pnl: str | None,
    ts: str = "2026-01-01T00:00:00+00:00",
    symbol: str = "BTC",
    event_uid: str | None = None,
) -> dict:
    return {
        "ts": ts,
        "source": source,
        "source_id": source_id,
        "exchange": "hyperliquid",
        "market_symbol": symbol,
        "market_key": symbol,
        "position_side": "LONG",
        "side": "long",
        "entry": "100",
        "exit": "110",
        "size": "1",
        "notional": "100",
        "pnl": pnl,
        "pct": "10" if pnl is not None else None,
        "is_win": None if pnl is None else int(Decimal(pnl) > 0),
        "realization_kind": "FULL",
        "event_uid": event_uid,
    }


def test_save_dual_writes_account_partitioned_ledger(tmp_path):
    path = tmp_path / "events.db"
    run(init_db(path))
    run(save_closed_trade(path, record(source_id="acct:a", source="A", pnl="12.50")))
    run(save_closed_trade(path, record(source_id="acct:b", source="B", pnl="-2")))

    connection = sqlite3.connect(path)
    try:
        accounts = connection.execute(
            "SELECT account_id FROM canonical_accounts ORDER BY account_id"
        ).fetchall()
        ledger = connection.execute(
            """
            SELECT account_id, entry_kind
            FROM canonical_ledger_entries
            ORDER BY account_id
            """
        ).fetchall()
    finally:
        connection.close()

    assert accounts == [("acct:a",), ("acct:b",)]
    assert ledger == [
        ("acct:a", "trade_realization"),
        ("acct:b", "trade_realization"),
    ]


def test_backfill_is_idempotent_and_resolves_legacy_alias(tmp_path):
    path = tmp_path / "events.db"
    run(init_db(path))
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            INSERT INTO closed_trades(
                ts, source, exchange, market_symbol, side, pnl,
                realization_kind
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-01-01T00:00:00+00:00",
                "Old label",
                "hyperliquid",
                "ETH",
                "long",
                "5",
                "FULL",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    aliases = {("Old label", "hyperliquid"): "acct:permanent"}
    first = run(backfill_canonical_ledger(path, aliases))
    second = run(backfill_canonical_ledger(path, aliases))
    rows = run(load_canonical_realizations(path))

    assert first == {"scanned": 1, "inserted": 1}
    assert second == {"scanned": 1, "inserted": 0}
    assert rows[0]["account_id"] == "acct:permanent"


def test_membership_remove_and_add_recalculates_without_deleting_history(tmp_path):
    path = tmp_path / "events.db"
    run(init_db(path))
    run(save_closed_trade(path, record(source_id="acct:a", source="A", pnl="10")))
    run(save_closed_trade(path, record(source_id="acct:b", source="B", pnl="20")))

    run(
        sync_portfolio_membership(
            path,
            [{"account_id": "acct:a", "name": "A", "exchange": "hyperliquid"}],
        )
    )
    only_a = run(load_canonical_realizations(path))
    assert [row["account_id"] for row in only_a] == ["acct:a"]

    connection = sqlite3.connect(path)
    try:
        b_ledger_count = connection.execute(
            """
            SELECT COUNT(*) FROM canonical_ledger_entries
            WHERE account_id='acct:b' AND entry_kind='trade_realization'
            """
        ).fetchone()[0]
    finally:
        connection.close()
    assert b_ledger_count == 1

    run(
        sync_portfolio_membership(
            path,
            [
                {"account_id": "acct:a", "name": "A", "exchange": "hyperliquid"},
                {"account_id": "acct:b", "name": "B", "exchange": "hyperliquid"},
            ],
        )
    )
    restored = run(load_canonical_realizations(path))
    assert {row["account_id"] for row in restored} == {"acct:a", "acct:b"}


def test_portfolio_is_composed_from_independent_account_projections():
    rows = [
        record(source_id="acct:a", source="A", pnl="10", event_uid="a"),
        record(source_id="acct:b", source="B", pnl="25", event_uid="b"),
    ]
    projection = project_portfolio(rows)

    assert len(projection.accounts) == 2
    assert projection.known_net_pnl == Decimal("35")
    assert {account.account_id for account in projection.accounts} == {
        "acct:a",
        "acct:b",
    }
    # Identical symbols in different accounts remain separate trades.
    assert len(projection.trades) == 2


def test_unknown_pnl_is_reported_not_fabricated_as_zero():
    projection = project_portfolio(
        [record(source_id="acct:a", source="A", pnl=None)]
    )
    assert projection.known_net_pnl == Decimal("0")
    assert projection.unknown_pnl_trades == 1
    assert projection.accounts[0].unknown_pnl_trades == 1


def test_reconciliation_appends_retraction_then_accepts_rebuilt_row(tmp_path):
    path = tmp_path / "events.db"
    run(init_db(path))
    run(
        save_closed_trade(
            path,
            record(
                source_id="acct:a",
                source="A",
                pnl="10",
                event_uid="old-fill",
            ),
        )
    )
    deleted = run(
        delete_closed_trades_by_identity_since(
            path,
            "acct:a",
            "A",
            "2026-01-01T00:00:00+00:00",
            include_legacy=False,
        )
    )
    assert deleted == 1
    assert run(load_canonical_realizations(path)) == []

    run(
        save_closed_trade(
            path,
            record(
                source_id="acct:a",
                source="A",
                pnl="11",
                event_uid="rebuilt-fill",
            ),
        )
    )
    active = run(load_canonical_realizations(path))
    assert len(active) == 1
    assert active[0]["pnl"] == "11"

    connection = sqlite3.connect(path)
    try:
        kinds = connection.execute(
            """
            SELECT entry_kind, COUNT(*)
            FROM canonical_ledger_entries
            GROUP BY entry_kind
            ORDER BY entry_kind
            """
        ).fetchall()
    finally:
        connection.close()
    assert kinds == [("retraction", 1), ("trade_realization", 2)]


def test_repair_before_backfill_still_preserves_auditable_retraction(tmp_path):
    path = tmp_path / "events.db"
    run(init_db(path))
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            INSERT INTO closed_trades(
                ts, source, source_id, exchange, market_symbol, side, pnl,
                realization_kind
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-01-01T00:00:00+00:00",
                "A",
                "acct:a",
                "hyperliquid",
                "BTC",
                "long",
                "10",
                "FULL",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    deleted = run(
        delete_closed_trades_by_identity_since(
            path,
            "acct:a",
            "A",
            "2026-01-01T00:00:00+00:00",
            include_legacy=False,
        )
    )
    assert deleted == 1
    assert run(load_canonical_realizations(path)) == []

    connection = sqlite3.connect(path)
    try:
        kinds = connection.execute(
            "SELECT entry_kind FROM canonical_ledger_entries ORDER BY sequence_id"
        ).fetchall()
    finally:
        connection.close()
    assert kinds == [("trade_realization",), ("retraction",)]
