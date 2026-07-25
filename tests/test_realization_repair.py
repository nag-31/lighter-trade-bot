from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from src.dashboard import (
    _merge_realized_pnl,
    _reduce_alert_should_send,
    _realization_sequence_key,
    _unrecorded_realizing_fills,
)
from tests.conftest import T0, make_trade


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
