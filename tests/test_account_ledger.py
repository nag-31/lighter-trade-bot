"""Tests for immutable per-account exchange ledgers."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from src.account_ledger import (
    account_db_path,
    append_realization,
    append_trade,
    init_account_ledger,
    load_realizations,
    replace_realizations,
)
from src.types import Trade


def run(awaitable):
    return asyncio.run(awaitable)


def sample_trade(**kwargs) -> Trade:
    values = dict(
        trade_id=17,
        timestamp=datetime(2026, 6, 1, tzinfo=timezone.utc),
        market_id=1,
        market_symbol="BTC",
        side="long",
        size=Decimal("0.1"),
        price=Decimal("100"),
        source_id="hl-main",
        exchange="hyperliquid",
        native_trade_id="fill-17",
    )
    values.update(kwargs)
    return Trade(**values)


def test_account_path_is_stable_and_partitioned(tmp_path):
    assert account_db_path(tmp_path, "hl-main") == tmp_path / "accounts" / "hl-main.db"
    assert account_db_path(tmp_path, "hl/second") == tmp_path / "accounts" / "hl_second.db"


def test_fill_is_immutable_and_changed_payload_is_observed(tmp_path):
    path = account_db_path(tmp_path, "hl-main")
    run(init_account_ledger(path, account_id="hl-main", exchange="hyperliquid", display_name="HL"))
    trade = sample_trade()
    run(append_trade(path, account_id="hl-main", exchange="hyperliquid", trade=trade, raw_payload={"px": "100"}))
    run(append_trade(path, account_id="hl-main", exchange="hyperliquid", trade=replace(trade, price=Decimal("101")), raw_payload={"px": "101"}))

    con = sqlite3.connect(path)
    try:
        fill = con.execute("SELECT price, raw_json FROM exchange_fills").fetchone()
        observations = con.execute("SELECT COUNT(*) FROM fill_observations").fetchone()[0]
    finally:
        con.close()
    assert fill[0] == "100"
    assert json.loads(fill[1]) == {"px": "100"}
    assert observations == 2


def test_realization_projection_is_idempotent_and_separate(tmp_path):
    path = account_db_path(tmp_path, "lighter-wallet")
    run(init_account_ledger(path, account_id="lighter-wallet", exchange="lighter", display_name="Lighter Wallet"))
    record = {"event_uid": "lighter-wallet|1|BOTH|42", "ts": "2026-06-02T00:00:00+00:00", "pnl": "12.5", "source_id": "lighter-wallet"}
    run(append_realization(path, record=record))
    run(append_realization(path, record=record))
    rows = run(load_realizations(path))
    assert len(rows) == 1
    assert rows[0]["pnl"] == "12.5"


def test_projection_rebuild_replaces_only_rows_at_cutoff(tmp_path):
    path = account_db_path(tmp_path, "hl-main")
    run(init_account_ledger(path, account_id="hl-main", exchange="hyperliquid", display_name="HL"))
    old = {"event_uid": "old", "ts": "2026-05-31T23:59:00+00:00", "pnl": "4"}
    stale = {"event_uid": "stale", "ts": "2026-06-02T00:00:00+00:00", "pnl": "5"}
    fresh = {"event_uid": "fresh", "ts": "2026-06-02T00:00:00+00:00", "pnl": "9"}
    for record in (old, stale):
        run(append_realization(path, record=record))
    result = run(replace_realizations(path, records=[fresh], cutoff_utc="2026-06-01T00:00:00+00:00"))
    assert result == {"deleted": 1, "inserted": 1}
    assert {row["event_uid"] for row in run(load_realizations(path))} == {"old", "fresh"}
