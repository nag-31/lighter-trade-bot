"""Tests for LighterClient parsing logic — no network calls."""

from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from src.lighter_client import LighterClient

POOL_ID = 42


def make_client() -> LighterClient:
    client = LighterClient(
        pool_id=POOL_ID,
        rest_base="https://example.com/api/v1",
        ws_url="wss://example.com/stream",
        source="test",
    )
    client._symbols = {0: "BTC", 1: "ETH", 2: "SOL"}
    return client


@pytest.mark.asyncio
async def test_wallet_address_resolves_selected_account_slot():
    requests: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "sub_accounts": [
                    {"index": 7402},
                    {"account_index": 7403},
                ],
                "next_cursor": None,
            },
        )

    client = LighterClient(
        pool_id=None,
        rest_base="https://example.com/api/v1",
        ws_url="wss://example.com/stream",
        source="wallet",
        l1_address="0x" + "1" * 40,
        account_slot=1,
    )
    await client._http.aclose()
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert await client._ensure_account_index() == 7403
        assert await client._ensure_account_index() == 7403
        assert requests == [{"l1_address": "0x" + "1" * 40}]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_wallet_address_rejects_unavailable_account_slot():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"sub_accounts": [{"index": 7402}], "next_cursor": None},
        )

    client = LighterClient(
        pool_id=None,
        rest_base="https://example.com/api/v1",
        ws_url="wss://example.com/stream",
        source="wallet",
        l1_address="0x" + "2" * 40,
        account_slot=1,
    )
    await client._http.aclose()
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(RuntimeError, match="account_slot 1 is unavailable"):
            await client._ensure_account_index()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_wallet_discovery_error_does_not_expose_address():
    address = "0x" + "5" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, request=request, json={"error": "down"})

    client = LighterClient(
        pool_id=None,
        rest_base="https://example.com/api/v1",
        ws_url="wss://example.com/stream",
        source="wallet",
        l1_address=address,
    )
    await client._http.aclose()
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(RuntimeError) as exc_info:
            await client._ensure_account_index()
        assert address not in str(exc_info.value)
        assert "HTTPStatusError" in str(exc_info.value)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_wallet_skips_private_rest_history_and_orders(caplog):
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(500, request=request)

    client = LighterClient(
        pool_id=7402,
        rest_base="https://example.com/api/v1",
        ws_url="wss://example.com/stream",
        source="wallet",
        l1_address="0x" + "3" * 40,
    )
    await client._http.aclose()
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert await client.fetch_trades_since(None) == []
        assert await client.fetch_open_orders() == []
        assert await client.fetch_sl_tp(0) == (None, None)
        assert requests == []
        assert caplog.text.count("skipping unauthenticated Lighter private REST") == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_public_pool_skips_authenticated_orders_endpoint():
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(403, request=request)

    client = make_client()
    await client._http.aclose()
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert await client.fetch_open_orders() == []
        assert await client.fetch_sl_tp(0) == (None, None)
        assert requests == []
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# market_symbol
# ---------------------------------------------------------------------------

class TestMarketSymbol:
    def test_known_id_returns_symbol(self):
        c = make_client()
        assert c.market_symbol(0) == "BTC"

    def test_unknown_id_returns_m_prefix(self):
        c = make_client()
        assert c.market_symbol(99) == "M99"


# ---------------------------------------------------------------------------
# _parse_trade
# ---------------------------------------------------------------------------

class TestParseTrade:
    def _raw(self, **overrides) -> dict:
        base = {
            "trade_id": 1001,
            "market_id": 0,
            "size": "0.5",
            "price": "50000",
            "timestamp": 1704067200000,  # 2024-01-01 00:00:00 UTC in ms
            "ask_account_id": 99,
            "bid_account_id": POOL_ID,
            "tx_hash": "0xabc",
        }
        base.update(overrides)
        return base

    def test_bid_is_pool_returns_long(self):
        c = make_client()
        t = c._parse_trade(self._raw(bid_account_id=POOL_ID, ask_account_id=99))
        assert t is not None
        assert t.side == "long"

    def test_ask_is_pool_returns_short(self):
        c = make_client()
        t = c._parse_trade(self._raw(ask_account_id=POOL_ID, bid_account_id=99))
        assert t is not None
        assert t.side == "short"

    def test_neither_side_returns_none(self):
        c = make_client()
        t = c._parse_trade(self._raw(bid_account_id=11, ask_account_id=22))
        assert t is None

    def test_ms_timestamp_converted(self):
        c = make_client()
        t = c._parse_trade(self._raw(timestamp=1704067200000))
        assert t.timestamp == datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    def test_iso_string_timestamp(self):
        c = make_client()
        t = c._parse_trade(self._raw(timestamp="2024-01-01T00:00:00Z"))
        assert t.timestamp.year == 2024

    def test_small_unix_seconds_timestamp(self):
        # timestamps <= 1e12 are treated as seconds, not ms
        c = make_client()
        t = c._parse_trade(self._raw(timestamp=1704067200))  # seconds
        assert t is not None
        assert t.timestamp.year == 2024

    def test_market_symbol_resolved(self):
        c = make_client()
        t = c._parse_trade(self._raw(market_id=1))
        assert t.market_symbol == "ETH"

    def test_unknown_market_uses_m_prefix(self):
        c = make_client()
        t = c._parse_trade(self._raw(market_id=99))
        assert t.market_symbol == "M99"

    def test_price_and_size_are_decimal(self):
        c = make_client()
        t = c._parse_trade(self._raw(size="0.00123456", price="0.102488"))
        assert isinstance(t.size, Decimal)
        assert isinstance(t.price, Decimal)

    def test_missing_trade_id_returns_none(self):
        c = make_client()
        raw = self._raw()
        del raw["trade_id"]
        assert c._parse_trade(raw) is None

    def test_missing_price_returns_none(self):
        c = make_client()
        raw = self._raw()
        del raw["price"]
        assert c._parse_trade(raw) is None

    def test_tx_hash_extracted(self):
        c = make_client()
        t = c._parse_trade(self._raw(tx_hash="0xdeadbeef"))
        assert t.tx_hash == "0xdeadbeef"

    def test_created_at_fallback_for_timestamp(self):
        c = make_client()
        raw = self._raw()
        del raw["timestamp"]
        raw["created_at"] = 1704067200000
        t = c._parse_trade(raw)
        assert t is not None


# ---------------------------------------------------------------------------
# _extract_trades
# ---------------------------------------------------------------------------

class TestExtractTrades:
    def test_subscribed_snapshot_returns_empty(self):
        msg = {"type": "subscribed/account_all_trades", "trades": [{"trade_id": 1}]}
        assert LighterClient._extract_trades(msg) == []

    def test_pong_returns_empty(self):
        assert LighterClient._extract_trades({"type": "pong"}) == []

    def test_error_returns_empty(self):
        assert LighterClient._extract_trades({"type": "error", "message": "bad"}) == []

    def test_update_with_flat_list(self):
        trades = [{"trade_id": 1}, {"trade_id": 2}]
        msg = {"type": "update/account_all_trades", "trades": trades}
        result = LighterClient._extract_trades(msg)
        assert result == trades

    def test_update_with_dict_per_market(self):
        msg = {
            "type": "update/account_all_trades",
            "trades": {
                "0": [{"trade_id": 1}, {"trade_id": 2}],
                "1": [{"trade_id": 3}],
            },
        }
        result = LighterClient._extract_trades(msg)
        trade_ids = {t["trade_id"] for t in result}
        assert trade_ids == {1, 2, 3}

    def test_root_level_trade(self):
        msg = {"trade_id": 5, "market_id": 0, "size": "1"}
        result = LighterClient._extract_trades(msg)
        assert result == [msg]

    def test_no_trades_key_returns_empty(self):
        msg = {"type": "heartbeat"}
        assert LighterClient._extract_trades(msg) == []

    def test_empty_trades_list(self):
        msg = {"type": "update/account_all_trades", "trades": []}
        assert LighterClient._extract_trades(msg) == []

    def test_empty_dict_trades(self):
        msg = {"type": "update/account_all_trades", "trades": {}}
        assert LighterClient._extract_trades(msg) == []
