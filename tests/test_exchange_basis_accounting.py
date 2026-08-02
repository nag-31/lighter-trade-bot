from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from command_center.lifecycles import reconstruct_lifecycles
from scripts.reconcile_hl_pnl import resolve_start_time_ms


def _event(
    event_id: int,
    trade_id: int,
    timestamp: str,
    *,
    kind: str,
    direction: str,
    size: str,
    price: str,
    before: str | None,
    after: str | None,
    before_entry: str | None,
    after_entry: str | None,
) -> dict:
    position = None
    if before is not None:
        position = {
            "source": "Lighter Wallet",
            "market_symbol": "LIT",
            "side": "long",
            "size": before,
            "avg_entry_price": before_entry,
        }
    after_position = None
    if after is not None:
        after_position = {
            "source": "Lighter Wallet",
            "market_symbol": "LIT",
            "side": "long",
            "size": after,
            "avg_entry_price": after_entry,
        }
    return {
        "id": event_id,
        "ts": timestamp,
        "payload": json.dumps(
            {
                "kind": kind,
                "trade": {
                    "trade_id": trade_id,
                    "timestamp": timestamp,
                    "source": "Lighter Wallet",
                    "market_symbol": "LIT",
                    "side": "long",
                    "size": size,
                    "price": price,
                    "dir": direction,
                },
                "position_before": position,
                "position_after": after_position,
            }
        ),
    }


def test_lighter_exits_use_each_exchange_position_basis_after_scale_in() -> None:
    rows = [
        _event(1, 1, "2026-07-01T00:00:00+00:00", kind="OPEN",
               direction="Open Long", size="10", price="100",
               before=None, after="10", before_entry=None, after_entry="100"),
        _event(2, 2, "2026-07-01T00:01:00+00:00", kind="REDUCE",
               direction="Close Long", size="2", price="110",
               before="10", after="8", before_entry="100", after_entry="100"),
        _event(3, 3, "2026-07-01T00:02:00+00:00", kind="SIZE_CHANGE",
               direction="Open Long", size="10", price="120",
               before="8", after="18", before_entry="100", after_entry="111.111111111111"),
        _event(4, 4, "2026-07-01T00:03:00+00:00", kind="CLOSE",
               direction="Close Long", size="18", price="100",
               before="18", after=None, before_entry="111.111111111111", after_entry=None),
    ]

    lifecycle = reconstruct_lifecycles(rows)[0]
    exits = [item for item in lifecycle["executions"] if item["action"] == "exit"]

    assert [item["pnl_basis"] for item in exits] == [
        "exchange_position_before",
        "exchange_position_before",
    ]
    assert [item["pnl"] for item in exits] == pytest.approx([20.0, -200.0])
    assert lifecycle["pnl"] == pytest.approx(-180.0)


def test_reconciliation_cutoff_is_explicit_and_does_not_default_to_epoch() -> None:
    now = int(datetime(2026, 8, 2, tzinfo=timezone.utc).timestamp() * 1000)

    assert resolve_start_time_ms(
        now_ms=now, days=180, from_date="2026-07-01"
    ) == int(datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp() * 1000)
    assert resolve_start_time_ms(now_ms=now, days=30) == now - 30 * 86_400_000

    with pytest.raises(ValueError, match="future"):
        resolve_start_time_ms(now_ms=now, days=30, from_date="2026-09-01")
