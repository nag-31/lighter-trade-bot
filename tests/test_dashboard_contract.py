from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from src.dashboard import INDEX_HTML, _to_jsonable, _trade_dedup_key
from src.types import Event, EventKind, Position


def test_jsonable_serializes_nested_event_values_for_api_payloads():
    position = Position(
        market_id=1,
        market_symbol="BTC",
        side="long",
        size=Decimal("1.25"),
        avg_entry_price=Decimal("100.50"),
        unrealized_pnl=Decimal("2.5"),
        stale_since=datetime(2026, 7, 24, tzinfo=timezone.utc),
    )
    event = Event(
        kind=EventKind.OPEN,
        trade=None,  # type: ignore[arg-type]
        position_before=None,
        position_after=position,
    )

    payload = _to_jsonable(event)

    assert payload["kind"] is EventKind.OPEN
    assert payload["position_after"]["size"] == "1.25"
    assert payload["position_after"]["stale_since"] == "2026-07-24T00:00:00+00:00"


def test_trade_dedup_key_is_scoped_to_source_market_side_and_native_id():
    from tests.conftest import make_trade

    trade = make_trade(trade_id=55, market_id=3)
    same_fill_other_source = trade

    assert _trade_dedup_key(trade, "source-a") != _trade_dedup_key(
        same_fill_other_source, "source-b"
    )
    assert _trade_dedup_key(trade, "source-a") == "source-a|3|BOTH|55"


def test_frontend_contract_keeps_truth_and_alert_views_present():
    required_fragments = (
        'id="positions"',
        'id="alerts"',
        'id="events"',
        'new WebSocket',
        '"positions"',
        "STALE",
        "Telegram alerts",
    )

    for fragment in required_fragments:
        assert fragment in INDEX_HTML
