from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import yaml

from src.binance_client import BinanceClient
from src.db import (
    enqueue_notification,
    init_db,
    load_closed_trades,
    mark_notification,
    notification_status,
    save_closed_trade,
)
from src.result import FetchResult, ResultState
from src.source_runtime import SourceRuntime
from src.sources import Source, load_source_report
from src.position_tracker import PositionTracker
from src.stats import aggregate_round_trips
from src.types import Position


def _run(coro):
    return asyncio.run(coro)


def _write_config(path: Path, sources) -> None:
    path.write_text(yaml.safe_dump({"sources": sources}), encoding="utf-8")


def test_empty_source_list_is_valid(tmp_path):
    config = tmp_path / "config.yaml"
    _write_config(config, [])
    report = load_source_report(config)
    assert report.sources == []
    assert report.ok


def test_missing_binance_credentials_disable_only_binance(tmp_path, monkeypatch):
    monkeypatch.delenv("BINANCE_A_KEY", raising=False)
    monkeypatch.delenv("BINANCE_A_SECRET", raising=False)
    config = tmp_path / "config.yaml"
    _write_config(
        config,
        [
            {
                "type": "lighter",
                "id": "lighter-main",
                "name": "Lighter",
                "pool_id": 123,
            },
            {
                "type": "binance",
                "id": "binance-main",
                "name": "Binance",
                "api_key_env": "BINANCE_A_KEY",
                "api_secret_env": "BINANCE_A_SECRET",
            },
        ],
    )
    report = load_source_report(config)
    assert [source.id for source in report.sources] == ["lighter-main"]
    assert any(
        issue.exchange == "binance" and issue.status == "disabled"
        for issue in report.issues
    )


def test_multiple_hyperliquid_wallet_envs(tmp_path, monkeypatch):
    monkeypatch.setenv("HL_WALLET_A", "0x" + "1" * 40)
    monkeypatch.setenv("HL_WALLET_B", "0x" + "2" * 40)
    config = tmp_path / "config.yaml"
    _write_config(
        config,
        [
            {
                "type": "hyperliquid",
                "id": "hl-a",
                "name": "HL A",
                "address_env": "HL_WALLET_A",
            },
            {
                "type": "hyperliquid",
                "id": "hl-b",
                "name": "HL B",
                "address_env": "HL_WALLET_B",
            },
        ],
    )
    report = load_source_report(config)
    assert {source.id for source in report.sources} == {"hl-a", "hl-b"}
    assert all(source.exchange == "hyperliquid" for source in report.sources)


def test_multiple_lighter_wallet_envs_and_subaccount_slots(tmp_path, monkeypatch):
    wallet_a = "0x" + "3" * 40
    wallet_b = "0x" + "4" * 40
    monkeypatch.setenv("LIGHTER_WALLET_A", wallet_a)
    monkeypatch.setenv("LIGHTER_WALLET_B", wallet_b)
    config = tmp_path / "config.yaml"
    _write_config(
        config,
        [
            {
                "type": "lighter",
                "id": "lighter-a-main",
                "name": "Lighter A Main",
                "address_env": "LIGHTER_WALLET_A",
            },
            {
                "type": "lighter",
                "id": "lighter-a-sub",
                "name": "Lighter A Sub",
                "address_env": "LIGHTER_WALLET_A",
                "account_slot": 1,
            },
            {
                "type": "lighter",
                "id": "lighter-b-main",
                "name": "Lighter B Main",
                "address_env": "LIGHTER_WALLET_B",
            },
        ],
    )
    report = load_source_report(config)
    assert {source.id for source in report.sources} == {
        "lighter-a-main",
        "lighter-a-sub",
        "lighter-b-main",
    }
    assert all(source.exchange == "lighter" for source in report.sources)
    assert all(source.url == "" for source in report.sources)
    assert len({source.account_fingerprint for source in report.sources}) == 3


def test_missing_lighter_wallet_env_disables_only_that_source(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("LIGHTER_MISSING_WALLET", raising=False)
    config = tmp_path / "config.yaml"
    _write_config(
        config,
        [
            {
                "type": "lighter",
                "id": "lighter-pool",
                "name": "Lighter Pool",
                "pool_id": 123,
            },
            {
                "type": "lighter",
                "id": "lighter-wallet",
                "name": "Lighter Wallet",
                "address_env": "LIGHTER_MISSING_WALLET",
            },
        ],
    )
    report = load_source_report(config)
    assert [source.id for source in report.sources] == ["lighter-pool"]
    assert any(
        issue.source_id == "lighter-wallet"
        and issue.status == "disabled"
        and issue.detail
        == "missing environment variable LIGHTER_MISSING_WALLET"
        for issue in report.issues
    )


def test_duplicate_explicit_source_id_is_rejected(tmp_path):
    config = tmp_path / "config.yaml"
    _write_config(
        config,
        [
            {"type": "lighter", "id": "same-id", "name": "A", "pool_id": 1},
            {"type": "lighter", "id": "same-id", "name": "B", "pool_id": 2},
        ],
    )
    report = load_source_report(config)
    assert len(report.sources) == 1
    assert any(issue.detail == "duplicate source id" for issue in report.issues)


def test_failed_snapshot_retains_last_good_as_stale():
    client = AsyncMock()
    client.bootstrap_markets.return_value = {}
    position = Position(
        market_id=1,
        market_symbol="BTC",
        side="long",
        size=Decimal("1"),
        avg_entry_price=Decimal("100"),
    )
    client.current_positions_result.side_effect = [
        FetchResult.success({1: position}),
        FetchResult.failure({}, error="timeout"),
    ]
    source = Source(
        id="hl-a",
        name="HL A",
        client=client,
        tracker=PositionTracker("HL A"),
        url="",
        min_notional=Decimal("0"),
        exchange="hyperliquid",
    )
    runtime = SourceRuntime(source)
    first = _run(runtime.fetch_positions())
    second = _run(runtime.fetch_positions())
    assert first.authoritative
    assert not second.authoritative
    assert second.state == ResultState.STALE
    assert second.value[1].stale
    assert second.value[1].source_id == "hl-a"


def test_authoritative_empty_snapshot_clears_last_good():
    client = AsyncMock()
    position = Position(
        market_id=1,
        market_symbol="BTC",
        side="long",
        size=Decimal("1"),
        avg_entry_price=Decimal("100"),
    )
    client.current_positions_result.side_effect = [
        FetchResult.success({1: position}),
        FetchResult.success({}),
    ]
    source = Source(
        id="hl-a",
        name="HL A",
        client=client,
        tracker=PositionTracker("HL A"),
        url="",
        min_notional=Decimal("0"),
        exchange="hyperliquid",
    )
    runtime = SourceRuntime(source)
    _run(runtime.fetch_positions())
    empty = _run(runtime.fetch_positions())
    assert empty.authoritative
    assert empty.value == {}
    assert runtime.last_good_positions == {}


def test_closed_trade_uid_is_source_scoped(tmp_path):
    db = tmp_path / "events.db"
    _run(init_db(db))
    base = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "source": "Account",
        "market_symbol": "BTC",
        "side": "long",
        "trade_id": 42,
        "native_trade_id": "42",
        "market_key": "1:BOTH",
        "position_side": "BOTH",
        "realization_kind": "FULL",
        "fill_ids": json.dumps([42]),
    }
    _run(
        save_closed_trade(
            db, {**base, "source_id": "account-a", "event_uid": "account-a|1|BOTH|42"}
        )
    )
    _run(
        save_closed_trade(
            db, {**base, "source_id": "account-b", "event_uid": "account-b|1|BOTH|42"}
        )
    )
    _run(
        save_closed_trade(
            db, {**base, "source_id": "account-a", "event_uid": "account-a|1|BOTH|42"}
        )
    )
    rows = _run(load_closed_trades(db))
    assert len(rows) == 2
    assert {row["source_id"] for row in rows} == {"account-a", "account-b"}


def test_same_display_name_accounts_do_not_merge_round_trips():
    rows = [
        {
            "ts": "2026-01-01T00:00:00+00:00",
            "source": "Main",
            "source_id": "account-a",
            "market_symbol": "BTC",
            "position_side": "BOTH",
            "pnl": "10",
            "size": "1",
            "entry": "100",
            "notional": "100",
            "realization_kind": "FULL",
        },
        {
            "ts": "2026-01-01T00:00:01+00:00",
            "source": "Main",
            "source_id": "account-b",
            "market_symbol": "BTC",
            "position_side": "BOTH",
            "pnl": "20",
            "size": "1",
            "entry": "100",
            "notional": "100",
            "realization_kind": "FULL",
        },
    ]
    aggregated = aggregate_round_trips(rows)
    assert len(aggregated) == 2
    assert {row["source_id"] for row in aggregated} == {"account-a", "account-b"}


def test_notification_outbox_is_restart_persistent(tmp_path):
    db = tmp_path / "events.db"
    _run(init_db(db))
    now = datetime.now(timezone.utc).isoformat()
    _run(enqueue_notification(db, "event-1", "telegram:channel", "hello", now))
    assert _run(notification_status(db, "event-1")) == "pending"
    _run(mark_notification(db, "event-1", "sent", now))
    assert _run(notification_status(db, "event-1")) == "sent"


def test_binance_hedge_legs_have_distinct_position_ids():
    client = BinanceClient("key", "secret", source="Binance")
    client._sym_to_id = {"BTCUSDT": 0}
    client._id_to_disp = {0: "BTC"}
    client._id_to_full = {0: "BTCUSDT"}
    client._fetch_position_risk = AsyncMock(
        return_value=[
            {
                "symbol": "BTCUSDT",
                "positionSide": "LONG",
                "positionAmt": "1",
                "entryPrice": "100",
            },
            {
                "symbol": "BTCUSDT",
                "positionSide": "SHORT",
                "positionAmt": "-2",
                "entryPrice": "110",
            },
        ]
    )
    positions = _run(client.current_positions())
    assert len(positions) == 2
    assert {p.position_side for p in positions.values()} == {"LONG", "SHORT"}
    assert {p.side for p in positions.values()} == {"long", "short"}
    _run(client.close())


def test_binance_native_trade_ids_do_not_collide_in_same_millisecond():
    client = BinanceClient("key", "secret", source="Binance")
    client._sym_to_id = {"BTCUSDT": 0}
    client._id_to_disp = {0: "BTC"}
    client._id_to_full = {0: "BTCUSDT"}
    common = {
        "e": "ORDER_TRADE_UPDATE",
        "o": {
            "x": "TRADE",
            "ps": "BOTH",
            "s": "BTCUSDT",
            "S": "BUY",
            "L": "100",
            "l": "1",
            "T": 1700000000000,
            "rp": "0",
        },
    }
    first = json.loads(json.dumps(common))
    second = json.loads(json.dumps(common))
    first["o"]["t"] = 10
    second["o"]["t"] = 11
    t1 = client._parse_ws_message(first)
    t2 = client._parse_ws_message(second)
    assert t1 is not None and t2 is not None
    assert t1.trade_id == 10
    assert t2.trade_id == 11
    assert t1.timestamp == t2.timestamp
    _run(client.close())
