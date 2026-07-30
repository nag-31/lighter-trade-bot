from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.dashboard import (
    _close_already_observed_by_fill,
    _is_pre_session_backfill,
    _lifecycle_card_position,
    _merge_realized_pnl,
    _reduce_alert_should_send,
    _realization_sequence_key,
    _roundtrip_partial_context,
    _unrecorded_realizing_fills,
)
from tests.conftest import T0, make_position, make_trade


def test_repair_selects_only_missing_historical_fills_before_live_close():
    current = replace(
        make_trade(trade_id=30, market_id=7),
        timestamp=T0 + timedelta(seconds=30),
    )
    already_recorded = replace(
        make_trade(trade_id=10, market_id=7),
        timestamp=T0 + timedelta(seconds=10),
    )
    missing_later = replace(
        make_trade(trade_id=20, market_id=7),
        timestamp=T0 + timedelta(seconds=20),
    )
    future_reopen = replace(
        make_trade(trade_id=40, market_id=7),
        timestamp=T0 + timedelta(seconds=40),
    )

    result = _unrecorded_realizing_fills(
        [future_reopen, current, missing_later, already_recorded, missing_later],
        {already_recorded.event_uid("hl-a")},
        "hl-a",
        current,
    )

    assert [trade.trade_id for trade in result] == [20]


def test_repair_rejects_fills_before_current_lifecycle():
    current = replace(
        make_trade(trade_id=30, market_id=7),
        timestamp=T0 + timedelta(seconds=30),
    )
    stale = replace(
        make_trade(trade_id=10, market_id=7),
        timestamp=T0 - timedelta(days=10),
    )
    current_partial = replace(
        make_trade(trade_id=20, market_id=7),
        timestamp=T0 + timedelta(seconds=20),
    )

    result = _unrecorded_realizing_fills(
        [stale, current_partial, current],
        set(),
        "hl-a",
        current,
        lifecycle_start=T0,
    )

    assert [trade.trade_id for trade in result] == [20]


def test_same_timestamp_close_burst_uses_start_position_to_restore_sequence():
    first = replace(
        make_trade(trade_id=2, market_id=7, size="3"),
        timestamp=T0,
        start_position=None,
    )
    flattening = replace(
        make_trade(trade_id=1, market_id=7, size="2"),
        timestamp=T0,
        start_position=Decimal("2"),
    )
    remaining = replace(
        make_trade(trade_id=3, market_id=7, size="1"),
        timestamp=T0,
        start_position=Decimal("5"),
    )

    ordered = sorted([first, flattening, remaining], key=_realization_sequence_key)

    assert [trade.trade_id for trade in ordered] == [3, 1, 2]


def test_reduce_pnl_merge_preserves_unknown_after_later_known_fill():
    assert _merge_realized_pnl(Decimal("10"), None) == (None, True)
    assert _merge_realized_pnl(None, Decimal("5"), True) == (None, True)
    assert _merge_realized_pnl(Decimal("10"), Decimal("5")) == (Decimal("15"), False)


def test_pending_reduce_before_close_does_not_send_intermediate_alert():
    assert _reduce_alert_should_send(Decimal("1000"), Decimal("900")) is True
    assert _reduce_alert_should_send(Decimal("1000"), Decimal("900"), closing=True) is False
    assert _reduce_alert_should_send(Decimal("899"), Decimal("900")) is False
    assert (
        _reduce_alert_should_send(
            Decimal("22000"),
            Decimal("900"),
            remaining_notional=Decimal("1"),
        )
        is False
    )


def test_startup_backfill_gate_allows_only_session_time_fills():
    started = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)

    assert _is_pre_session_backfill(
        started - timedelta(minutes=10),
        started,
    )
    assert not _is_pre_session_backfill(
        started - timedelta(seconds=90),
        started,
    )
    assert not _is_pre_session_backfill(
        started + timedelta(seconds=1),
        started,
    )


def test_lifecycle_card_reconstructs_full_notional_from_partial_exits():
    rows = [
        {
            "source_id": "hl-a",
            "source": "HL",
            "market_symbol": "BTC",
            "position_side": "BOTH",
            "realization_kind": "PARTIAL",
            "size": "0.10",
            "entry": "100000",
            "exit": "108000",
            "pnl": "800",
            "ts": "2026-07-28T11:50:00+00:00",
        },
        {
            "source_id": "hl-a",
            "source": "HL",
            "market_symbol": "BTC",
            "position_side": "BOTH",
            "realization_kind": "PARTIAL",
            "size": "0.09999",
            "entry": "100000",
            "exit": "107500",
            "pnl": "749.925",
            "ts": "2026-07-28T11:40:00+00:00",
        },
    ]
    context = _roundtrip_partial_context(rows, "hl-a", "HL", "BTC")
    dust = make_position(size="0.00001", avg_entry_price="100000")

    lifecycle = _lifecycle_card_position(dust, context)

    assert context["count"] == 2
    assert context["pnl"] == Decimal("1549.925")
    assert lifecycle is not None
    assert lifecycle.size == Decimal("0.20000")
    assert lifecycle.notional_usd == Decimal("20000")


def test_roundtrip_context_stops_at_previous_full_close_and_preserves_unknown():
    rows = [
        {
            "source_id": "hl-a",
            "market_symbol": "BTC",
            "realization_kind": "PARTIAL",
            "size": "1",
            "entry": "100",
            "exit": "105",
            "pnl": None,
            "ts": "2026-07-28T11:00:00+00:00",
        },
        {
            "source_id": "hl-a",
            "market_symbol": "BTC",
            "realization_kind": "FULL",
            "size": "1",
            "entry": "90",
            "exit": "95",
            "pnl": "5",
            "ts": "2026-07-27T11:00:00+00:00",
        },
        {
            "source_id": "hl-a",
            "market_symbol": "BTC",
            "realization_kind": "PARTIAL",
            "size": "100",
            "entry": "1",
            "exit": "2",
            "pnl": "100",
            "ts": "2026-07-26T11:00:00+00:00",
        },
    ]

    context = _roundtrip_partial_context(rows, "hl-a", "HL", "BTC")

    assert context["count"] == 1
    assert context["pnl"] is None
    assert context["pnl_unknown"] is True
    assert context["size"] == Decimal("1")


def test_roundtrip_context_uses_event_time_not_repair_insertion_order():
    rows = [
        {
            "id": 103,
            "source_id": "hl-a",
            "market_symbol": "BTC",
            "realization_kind": "PARTIAL",
            "size": "100",
            "entry": "1",
            "exit": "2",
            "pnl": "100",
            "ts": "2026-06-01T11:00:00+00:00",
        },
        {
            "id": 102,
            "source_id": "hl-a",
            "market_symbol": "BTC",
            "realization_kind": "PARTIAL",
            "size": "2",
            "entry": "100",
            "exit": "105",
            "pnl": "10",
            "ts": "2026-07-28T11:00:00+00:00",
        },
        {
            "id": 101,
            "source_id": "hl-a",
            "market_symbol": "BTC",
            "realization_kind": "FULL",
            "size": "1",
            "entry": "90",
            "exit": "95",
            "pnl": "5",
            "ts": "2026-07-27T11:00:00+00:00",
        },
    ]

    context = _roundtrip_partial_context(rows, "hl-a", "HL", "BTC")

    assert context["count"] == 1
    assert context["pnl"] == Decimal("10")
    assert context["size"] == Decimal("2")
    assert context["oldest_ts"] == "2026-07-28T11:00:00+00:00"


def test_roundtrip_full_wins_boundary_at_identical_timestamp():
    rows = [
        {
            "source_id": "hl-a",
            "market_symbol": "BTC",
            "realization_kind": "PARTIAL",
            "pnl": "10",
            "ts": "2026-07-28T11:00:00+00:00",
        },
        {
            "source_id": "hl-a",
            "market_symbol": "BTC",
            "realization_kind": "FULL",
            "pnl": "5",
            "ts": "2026-07-28T11:00:00+00:00",
        },
    ]

    context = _roundtrip_partial_context(rows, "hl-a", "HL", "BTC")

    assert context["count"] == 0


def test_reconciler_knows_when_fill_consumer_already_removed_position():
    tracked = {1: make_position(market_id=1)}

    assert _close_already_observed_by_fill(2, tracked)
    assert not _close_already_observed_by_fill(1, tracked)
