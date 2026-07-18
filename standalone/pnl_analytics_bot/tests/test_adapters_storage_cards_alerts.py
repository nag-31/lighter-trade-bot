from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from standalone.pnl_analytics_bot.adapters.hyperliquid import parse_user_fill
from standalone.pnl_analytics_bot.adapters.lighter import parse_trade
from standalone.pnl_analytics_bot.cards.renderer import render_round_trip_card
from standalone.pnl_analytics_bot.core.engine import PnlReconstructor
from standalone.pnl_analytics_bot.notifier.telegram import AlertDeduper, RoundTripTelegramAlerter, format_round_trip_alert
from standalone.pnl_analytics_bot.notifier.service import process_fills_and_alert_closed_round_trips
from standalone.pnl_analytics_bot.reports.fixtures import acceptance_fills
from standalone.pnl_analytics_bot.storage.sqlite_store import AnalyticsStore


def test_hyperliquid_adapter_parses_closed_pnl_and_fee():
    fill = parse_user_fill(
        {
            "coin": "BTC",
            "tid": 123,
            "oid": 456,
            "time": 1780272000000,
            "side": "B",
            "sz": "0.5",
            "px": "100",
            "closedPnl": "12.5",
            "fee": "0.1",
            "feeToken": "USDC",
        },
        account="acct",
    )
    assert fill.side == "buy"
    assert fill.exchange_realized_pnl == Decimal("12.5")
    assert fill.fee == Decimal("0.1")

def test_hyperliquid_adapter_preserves_hip3_stock_perp_symbol():
    fill = parse_user_fill(
        {
            "coin": "rwa:SKHYNIX",
            "tid": 124,
            "oid": 457,
            "time": 1780272000000,
            "side": "A",
            "sz": "2",
            "px": "150",
            "closedPnl": "-5",
            "fee": "0.2",
        },
        account="acct",
    )
    assert fill.symbol == "RWA:SKHYNIX"
    assert fill.side == "sell"
    assert fill.exchange_realized_pnl == Decimal("-5")


def test_lighter_adapter_standard_account_zeroes_malformed_fee():
    fill = parse_trade(
        {
            "trade_id": 1,
            "timestamp": "2026-06-01T00:00:00+00:00",
            "side": "sell",
            "size": "2",
            "price": "90",
            "fee": "bad",
            "market_symbol": "BTC",
        },
        standard_account=True,
    )
    assert fill.fee == Decimal("0")
    assert fill.side == "sell"


def test_lighter_adapter_premium_uses_parseable_fee():
    fill = parse_trade(
        {
            "trade_id": 1,
            "timestamp": "2026-06-01T00:00:00+00:00",
            "side": "buy",
            "size": "2",
            "price": "90",
            "fee": "0.03",
            "market_symbol": "BTC",
        },
        standard_account=False,
    )
    assert fill.fee == Decimal("0.03")


def test_storage_persists_only_standalone_tables(tmp_path):
    result = PnlReconstructor().reconstruct(acceptance_fills())
    db = tmp_path / "pnl_analytics.db"
    store = AnalyticsStore(db)
    store.init()
    store.save_result(result)
    with sqlite3.connect(db) as con:
        assert con.execute("select count(*) from raw_fills").fetchone()[0] == len(result.raw_fills)
        assert con.execute("select count(*) from round_trips").fetchone()[0] == len(result.round_trips)
        assert con.execute("select count(*) from realizations").fetchone()[0] >= len(result.round_trips)


def test_card_renderer_outputs_png_bytes(tmp_path):
    rt = PnlReconstructor().reconstruct(acceptance_fills()).round_trips[0]
    path = tmp_path / "card.png"
    data = render_round_trip_card(rt, output_path=path)
    assert data.startswith(b"\x89PNG")
    assert path.read_bytes().startswith(b"\x89PNG")


def test_alert_format_mentions_closed_round_trip_return_on_cost():
    rt = PnlReconstructor().reconstruct(acceptance_fills()).round_trips[0]
    text = format_round_trip_alert(rt)
    assert "CLOSE" in text
    assert "Return on cost" in text
    assert rt.symbol in text


def test_alert_deduper_blocks_duplicates_and_rate_limit():
    deduper = AlertDeduper(ttl_seconds=60, max_per_minute=2)
    assert deduper.should_send("a", now=0)
    assert not deduper.should_send("a", now=1)
    assert deduper.should_send("b", now=2)
    assert not deduper.should_send("c", now=3)
    assert deduper.should_send("a", now=61)


class FakeTransport:
    def __init__(self):
        self.sent = []

    async def send_photo(self, *, caption: str, png_bytes: bytes) -> None:
        self.sent.append((caption, png_bytes))


@pytest.mark.asyncio
async def test_telegram_alerter_sends_once_per_closed_round_trip():
    rt = PnlReconstructor().reconstruct(acceptance_fills()).round_trips[0]
    transport = FakeTransport()
    alerter = RoundTripTelegramAlerter(transport, AlertDeduper(ttl_seconds=60, max_per_minute=10))
    assert await alerter.alert_closed_round_trip(rt) is True
    assert await alerter.alert_closed_round_trip(rt) is False
    assert len(transport.sent) == 1
    assert transport.sent[0][1].startswith(b"\x89PNG")


@pytest.mark.asyncio
async def test_alert_service_bootstraps_historical_without_sending(tmp_path):
    store = AnalyticsStore(tmp_path / "alerts.db")
    transport = FakeTransport()
    alerter = RoundTripTelegramAlerter(transport, AlertDeduper(ttl_seconds=60, max_per_minute=20))
    first = await process_fills_and_alert_closed_round_trips(
        acceptance_fills(),
        store=store,
        alerter=alerter,
        max_alerts=10,
    )
    assert first["telegram_alerts_sent"] == 0
    assert first["telegram_alerts_skipped_historical"] == 6
    assert transport.sent == []
    second = await process_fills_and_alert_closed_round_trips(
        acceptance_fills(),
        store=store,
        alerter=RoundTripTelegramAlerter(FakeTransport(), AlertDeduper(ttl_seconds=60, max_per_minute=20)),
        max_alerts=10,
    )
    assert second["telegram_alerts_sent"] == 0
    assert second["telegram_alerts_skipped_existing"] == 6


@pytest.mark.asyncio
async def test_alert_service_sends_newly_discovered_closes_after_bootstrap(tmp_path):
    store = AnalyticsStore(tmp_path / "post_bootstrap_alerts.db")
    first_transport = FakeTransport()
    first = await process_fills_and_alert_closed_round_trips(
        acceptance_fills()[:2],
        store=store,
        alerter=RoundTripTelegramAlerter(first_transport, AlertDeduper(ttl_seconds=60, max_per_minute=20)),
        max_alerts=10,
    )
    assert first["telegram_alerts_sent"] == 0
    assert first["telegram_alerts_skipped_historical"] == 1
    assert first_transport.sent == []

    second_transport = FakeTransport()
    second = await process_fills_and_alert_closed_round_trips(
        acceptance_fills(),
        store=store,
        alerter=RoundTripTelegramAlerter(second_transport, AlertDeduper(ttl_seconds=60, max_per_minute=20)),
        max_alerts=10,
    )
    assert second["telegram_alerts_sent"] == 5
    assert second["telegram_alerts_skipped_existing"] == 1
    assert second["telegram_alerts_skipped_historical"] == 0
    assert len(second_transport.sent) == 5


@pytest.mark.asyncio
async def test_alert_service_backfill_can_send_historical_when_explicit(tmp_path):
    store = AnalyticsStore(tmp_path / "backfill_alerts.db")
    transport = FakeTransport()
    summary = await process_fills_and_alert_closed_round_trips(
        acceptance_fills(),
        store=store,
        alerter=RoundTripTelegramAlerter(transport, AlertDeduper(ttl_seconds=60, max_per_minute=20)),
        max_alerts=10,
        send_historical=True,
    )
    assert summary["telegram_alerts_sent"] == 6
    assert summary["telegram_alerts_skipped_historical"] == 0
    assert len(transport.sent) == 6


@pytest.mark.asyncio
async def test_alert_service_sends_only_round_trips_after_alert_cutoff(tmp_path):
    store = AnalyticsStore(tmp_path / "cutoff_alerts.db")
    transport = FakeTransport()
    summary = await process_fills_and_alert_closed_round_trips(
        acceptance_fills(),
        store=store,
        alerter=RoundTripTelegramAlerter(transport, AlertDeduper(ttl_seconds=60, max_per_minute=20)),
        max_alerts=10,
        alert_after=datetime(2026, 6, 1, 0, 9, tzinfo=timezone.utc),
    )
    assert summary["telegram_alerts_sent"] == 3
    assert summary["telegram_alerts_skipped_historical"] == 3
    assert len(transport.sent) == 3
