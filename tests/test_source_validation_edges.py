from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
import yaml

from src.sources import Source, _preview_source_identity, load_source_report
from src.position_tracker import PositionTracker
from src.types import Trade
from datetime import datetime, timezone


def _run(coro):
    return asyncio.run(coro)


def _write(path, sources):
    path.write_text(yaml.safe_dump({"sources": sources}), encoding="utf-8")


def test_missing_file_and_non_list_are_reported_without_crashing(tmp_path):
    missing = load_source_report(tmp_path / "missing.yaml")
    assert not missing.ok
    assert missing.issues[0].status == "config_error"

    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("sources: not-a-list\n", encoding="utf-8")
    report = load_source_report(invalid)
    assert not report.ok
    assert report.issues[0].detail == "sources must be a list"


@pytest.mark.parametrize(
    ("source", "detail"),
    [
        ({"type": "unknown", "name": "X"}, "unsupported source type"),
        ({"type": "lighter", "name": "X"}, "configure exactly one of pool_id or address_env"),
        ({"type": "lighter", "name": "X", "pool_id": -1}, "pool_id must be a non-negative integer"),
        ({"type": "hyperliquid", "name": "X", "address_env": "lowercase"}, "address_env must name an uppercase environment variable"),
    ],
)
def test_invalid_source_shapes_are_redacted_and_isolated(tmp_path, source, detail):
    config = tmp_path / "config.yaml"
    _write(config, [source, {"type": "lighter", "name": "valid pool", "pool_id": 1}])

    report = load_source_report(config)

    assert [item.name for item in report.sources] == ["valid pool"]
    assert any(issue.detail == detail for issue in report.issues)
    assert all("0x" not in issue.detail for issue in report.issues)


def test_duplicate_account_configuration_is_rejected_even_with_different_ids(
    tmp_path, monkeypatch
):
    address = "0x" + "a" * 40
    monkeypatch.setenv("HL_A", address)
    config = tmp_path / "config.yaml"
    _write(
        config,
        [
            {"type": "hyperliquid", "id": "hl-a", "name": "A", "address_env": "HL_A"},
            {"type": "hyperliquid", "id": "hl-b", "name": "B", "address_env": "HL_A"},
        ],
    )

    report = load_source_report(config)

    assert len(report.sources) == 1
    assert any(issue.detail == "duplicate account/pool configuration" for issue in report.issues)


def test_disabled_entry_is_reported_and_not_constructed(tmp_path):
    config = tmp_path / "config.yaml"
    _write(config, [{"type": "lighter", "id": "off", "name": "Off", "enabled": False}])

    report = load_source_report(config)

    assert report.sources == []
    assert report.ok
    assert report.issues[0].status == "disabled"


def test_preview_identity_is_stable_and_does_not_include_wallet_address(
    monkeypatch,
):
    address = "0x" + "b" * 40
    monkeypatch.setenv("WALLET_A", address)

    source_id, account = _preview_source_identity(
        {"type": "lighter", "id": "wallet-a", "address_env": "WALLET_A", "account_slot": 2}
    )

    assert source_id == "wallet-a"
    assert address not in source_id
    assert account.startswith("lighter-wallet:")
    assert address not in account


def test_source_cursor_uses_protocol_specific_identity():
    source = Source(
        id="binance-a",
        name="Binance",
        client=object(),
        tracker=PositionTracker("Binance"),
        url="",
        min_notional=Decimal("0"),
        exchange="binance",
    )
    trade = Trade(
        trade_id=55,
        timestamp=datetime.fromtimestamp(1700000000.123, tz=timezone.utc),
        market_id=1,
        market_symbol="BTC",
        side="long",
        size=Decimal("1"),
        price=Decimal("100"),
    )

    assert source.cursor_for_trade(trade) == 1700000000123
