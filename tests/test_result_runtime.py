from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from src.position_tracker import PositionTracker
from src.result import FetchResult, ResultState, coerce_fetch_result
from src.source_runtime import SourceRuntime
from src.sources import Source
from src.types import Position


def _run(coro):
    return asyncio.run(coro)


def _source(client) -> Source:
    return Source(
        id="hl-account-a",
        name="HL A",
        client=client,
        tracker=PositionTracker("HL A"),
        url="",
        min_notional=Decimal("0"),
        exchange="hyperliquid",
    )


def _position(**overrides) -> Position:
    values = dict(
        market_id=7,
        market_symbol="BTC",
        side="long",
        size=Decimal("2"),
        avg_entry_price=Decimal("100"),
    )
    values.update(overrides)
    return Position(**values)


def test_fetch_result_factories_preserve_authority_contract():
    success = FetchResult.success({"BTC": 1}, detail="authoritative")
    stale = FetchResult.stale({"BTC": 1}, error="timeout")
    failure = FetchResult.failure({}, error="HTTP 503")
    disabled = FetchResult.disabled({}, detail="not configured")

    assert (success.ok, success.authoritative, success.state) == (
        True,
        True,
        ResultState.OK,
    )
    assert (stale.ok, stale.authoritative, stale.error) == (False, False, "timeout")
    assert (failure.state, failure.authoritative, failure.value) == (
        ResultState.ERROR,
        False,
        {},
    )
    assert (disabled.state, disabled.authoritative, disabled.detail) == (
        ResultState.DISABLED,
        False,
        "not configured",
    )


def test_coerce_fetch_result_wraps_legacy_values_as_success():
    value = {1: _position()}
    result = coerce_fetch_result(value)

    assert result.value is value
    assert result.ok
    assert result.authoritative


@pytest.mark.asyncio
async def test_runtime_normalizes_source_identity_and_clears_stale_after_recovery():
    client = AsyncMock()
    client.current_positions_result.side_effect = [
        FetchResult.success({7: _position(source="", source_id="", exchange="")}),
        FetchResult.failure({}, error="temporary timeout"),
        FetchResult.success({7: _position(source="new label")}),
    ]
    runtime = SourceRuntime(_source(client))

    first = await runtime.fetch_positions()
    stale = await runtime.fetch_positions()
    recovered = await runtime.fetch_positions()

    assert first.authoritative
    assert first.value[7].source == ""
    assert first.value[7].source_id == "hl-account-a"
    assert first.value[7].exchange == "hyperliquid"
    assert stale.state is ResultState.STALE
    assert stale.value[7].stale
    assert recovered.authoritative
    assert not recovered.value[7].stale
    assert runtime.consecutive_failures == 0
    assert runtime.last_good_positions[7].source == "new label"


@pytest.mark.asyncio
async def test_runtime_converts_legacy_exception_to_last_good_stale_snapshot():
    client = AsyncMock()
    client.current_positions_result.side_effect = [
        {7: _position()},
        RuntimeError("provider unavailable"),
    ]
    runtime = SourceRuntime(_source(client))

    assert (await runtime.fetch_positions()).authoritative
    result = await runtime.fetch_positions()

    assert result.state is ResultState.STALE
    assert not result.authoritative
    assert "RuntimeError" in result.error
    assert "provider unavailable" in result.error
    assert result.value[7].stale


@pytest.mark.asyncio
async def test_runtime_does_not_bootstrap_until_authoritative_snapshot_exists():
    client = AsyncMock()
    client.bootstrap_markets.return_value = {}
    client.current_positions_result.side_effect = [
        FetchResult.failure({}, error="rate limited"),
        FetchResult.success({}),
    ]
    runtime = SourceRuntime(_source(client))

    with pytest.raises(RuntimeError, match="rate limited"):
        await runtime.bootstrap()
    assert not runtime.bootstrapped

    assert await runtime.bootstrap() == {}
    assert runtime.bootstrapped
    assert client.bootstrap_markets.await_count == 2


@pytest.mark.asyncio
async def test_runtime_bootstrap_is_idempotent_after_success():
    client = AsyncMock()
    client.bootstrap_markets.return_value = {}
    client.current_positions_result.return_value = FetchResult.success({7: _position()})
    runtime = SourceRuntime(_source(client))

    first = await runtime.bootstrap()
    second = await runtime.bootstrap()

    assert first == second
    assert client.bootstrap_markets.await_count == 1
    assert client.current_positions_result.await_count == 1
