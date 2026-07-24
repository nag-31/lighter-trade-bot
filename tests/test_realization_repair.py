from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from src.dashboard import _realization_sequence_key, _unrecorded_realizing_fills
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
