from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.candle_provider import CandleProvider, _normalize


def test_normalize_deduplicates_and_sorts_exchange_rows():
    rows = [
        {"t": 1_700_000_120_000, "o": "2", "h": "3", "l": "1", "c": "2.5", "v": "4"},
        {"t": 1_700_000_060_000, "o": "1", "h": "2", "l": "0.5", "c": "2", "v": "3"},
        # The later duplicate wins, matching an exchange's corrected candle.
        {"t": 1_700_000_060_000, "o": "1", "h": "2.1", "l": "0.5", "c": "2", "v": "3"},
    ]

    candles = _normalize(rows)

    assert [item.opened_at for item in candles] == [
        datetime.fromtimestamp(1_700_000_060, tz=timezone.utc),
        datetime.fromtimestamp(1_700_000_120, tz=timezone.utc),
    ]
    assert candles[0].high == Decimal("2.1")


@pytest.mark.asyncio
async def test_hyperliquid_provider_returns_domain_candles_and_provenance():
    class Info:
        def candles_snapshot(self, name, interval, start, end):
            assert (name, interval) == ("LDO", "1m")
            return [
                {"t": start, "o": "0.35", "h": "0.36", "l": "0.34", "c": "0.355", "v": "10"},
                {"t": end - 60_000, "o": "0.355", "h": "0.37", "l": "0.35", "c": "0.36", "v": "12"},
            ]

    source = SimpleNamespace(
        name="HL",
        exchange="hyperliquid",
        client=SimpleNamespace(_info=Info()),
    )
    provider = CandleProvider(timeout_seconds=1)

    candles, provenance = await provider.fetch_for_lifecycle(
        source,
        market_id=46,
        market_symbol="LDO",
        opened_at=datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc),
        closed_at=datetime(2026, 8, 2, 12, 2, tzinfo=timezone.utc),
    )

    assert len(candles) == 2
    assert candles[0].open == Decimal("0.35")
    assert provenance == "hyperliquid:candleSnapshot:1m"


@pytest.mark.asyncio
async def test_provider_failure_is_truthful_execution_only_fallback():
    class BrokenInfo:
        def candles_snapshot(self, *args):
            raise TimeoutError("offline")

    source = SimpleNamespace(
        name="HL",
        exchange="hyperliquid",
        client=SimpleNamespace(_info=BrokenInfo()),
    )
    provider = CandleProvider(timeout_seconds=1)

    candles, provenance = await provider.fetch_for_lifecycle(
        source,
        market_id=46,
        market_symbol="LDO",
        opened_at=datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc),
        closed_at=datetime(2026, 8, 2, 12, 2, tzinfo=timezone.utc),
    )

    assert candles == ()
    assert provenance == "execution-only"


@pytest.mark.asyncio
async def test_lighter_provider_uses_public_candles_endpoint():
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "candles": [
                    {"t": 1_700_000_000_000, "o": "0.35", "h": "0.36", "l": "0.34", "c": "0.355", "v": "5"}
                ]
            }

    class Http:
        async def get(self, url, params):
            assert url.endswith("/candles")
            assert params["market_id"] == 46
            assert params["resolution"] == "1m"
            return Response()

    source = SimpleNamespace(
        name="Lighter",
        exchange="lighter",
        client=SimpleNamespace(_http=Http(), _rest_base="https://lighter.test/api/v1"),
    )
    candles, provenance = await CandleProvider(timeout_seconds=1).fetch_for_lifecycle(
        source,
        market_id=46,
        market_symbol="LDO",
        opened_at=datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc),
        closed_at=datetime(2026, 8, 2, 12, 2, tzinfo=timezone.utc),
    )

    assert len(candles) == 1
    assert provenance == "lighter:candles:1m"
