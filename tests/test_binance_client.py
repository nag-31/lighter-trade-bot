"""Tests for BinanceClient parsing — no network calls."""

import asyncio
import hashlib
import hmac
import time
import urllib.parse
from decimal import Decimal
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.binance_client import BinanceClient, BinanceUnavailable, _to_decimal
from src.proxy_pool import ProxyPool


def make_client() -> BinanceClient:
    c = BinanceClient(
        api_key="testkey",
        api_secret="testsecret",
        source="test",
        rest_base="https://fapi.binance.com",
    )
    # Pre-seed market maps so _market_id / _market_symbol work without network
    c._sym_to_id  = {"BTCUSDT": 0, "ETHUSDT": 1, "SOLUSDT": 2, "DOGEUSDT": 3}
    c._id_to_disp = {0: "BTC", 1: "ETH", 2: "SOL", 3: "DOGE"}
    c._id_to_full = {0: "BTCUSDT", 1: "ETHUSDT", 2: "SOLUSDT", 3: "DOGEUSDT"}
    return c


# ---------------------------------------------------------------------------
# _to_decimal helper
# ---------------------------------------------------------------------------

class TestToDecimal:
    def test_string_number(self):
        assert _to_decimal("50000.5") == Decimal("50000.5")

    def test_int(self):
        assert _to_decimal(100) == Decimal("100")

    def test_none_returns_none(self):
        assert _to_decimal(None) is None

    def test_empty_string_returns_none(self):
        assert _to_decimal("") is None

    def test_inf_returns_none(self):
        assert _to_decimal("Infinity") is None

    def test_nan_returns_none(self):
        assert _to_decimal("NaN") is None

    def test_negative(self):
        assert _to_decimal("-100.5") == Decimal("-100.5")


# ---------------------------------------------------------------------------
# _sign
# ---------------------------------------------------------------------------

class TestSign:
    def test_timestamp_added(self):
        c = make_client()
        params = c._sign({})
        assert "timestamp" in params

    def test_signature_added(self):
        c = make_client()
        params = c._sign({})
        assert "signature" in params

    def test_signature_is_correct_hmac(self):
        c = make_client()
        p = {"symbol": "BTCUSDT", "limit": "100"}
        signed = c._sign(dict(p))
        qs  = urllib.parse.urlencode({k: v for k, v in signed.items() if k != "signature"})
        expected = hmac.new(b"testsecret", qs.encode(), hashlib.sha256).hexdigest()
        assert signed["signature"] == expected

    def test_sign_does_not_mutate_original(self):
        c = make_client()
        original = {"symbol": "BTCUSDT"}
        c._sign(dict(original))
        assert "timestamp" not in original


# ---------------------------------------------------------------------------
# _parse_ws_message
# ---------------------------------------------------------------------------

def _ws_msg(
    event_type="ORDER_TRADE_UPDATE",
    x="TRADE",
    ps="BOTH",
    symbol="BTCUSDT",
    side="BUY",
    last_price="50000",
    last_qty="0.1",
    tx_time=1704067200000,
    rp="0",
    **extra_order_fields,
) -> dict:
    order = {
        "x": x, "ps": ps, "s": symbol, "S": side,
        "L": last_price, "l": last_qty, "T": tx_time, "rp": rp,
    }
    order.update(extra_order_fields)
    return {"e": event_type, "o": order}


class TestParseWsMessage:

    def test_valid_buy_returns_long_trade(self):
        c = make_client()
        t = c._parse_ws_message(_ws_msg(side="BUY"))
        assert t is not None
        assert t.side == "long"

    def test_valid_sell_returns_short_trade(self):
        c = make_client()
        t = c._parse_ws_message(_ws_msg(side="SELL"))
        assert t is not None
        assert t.side == "short"

    def test_wrong_event_type_returns_none(self):
        c = make_client()
        assert c._parse_ws_message(_ws_msg(event_type="ACCOUNT_UPDATE")) is None

    def test_x_not_trade_returns_none(self):
        c = make_client()
        assert c._parse_ws_message(_ws_msg(x="NEW")) is None

    def test_hedge_position_side_is_supported(self):
        c = make_client()
        trade = c._parse_ws_message(_ws_msg(ps="LONG"))
        assert trade is not None
        assert trade.position_side == "LONG"
        assert trade.exchange == "binance"

    def test_zero_price_returns_none(self):
        c = make_client()
        assert c._parse_ws_message(_ws_msg(last_price="0")) is None

    def test_zero_qty_returns_none(self):
        c = make_client()
        assert c._parse_ws_message(_ws_msg(last_qty="0")) is None

    def test_trade_id_is_tx_time(self):
        c = make_client()
        t = c._parse_ws_message(_ws_msg(tx_time=1704067200999))
        assert t.trade_id == 1704067200999

    def test_timestamp_parsed_from_tx_time(self):
        c = make_client()
        t = c._parse_ws_message(_ws_msg(tx_time=1704067200000))
        assert t.timestamp.year == 2024

    def test_realized_pnl_extracted(self):
        c = make_client()
        t = c._parse_ws_message(_ws_msg(rp="250.50"))
        assert t.realized_pnl == Decimal("250.50")

    def test_realized_pnl_zero_is_none(self):
        # rp=0 means an opening fill with no realized PnL — client maps this to None
        # so "P&L: +$0.00" never appears in alerts for position-adding trades.
        c = make_client()
        t = c._parse_ws_message(_ws_msg(rp="0"))
        assert t.realized_pnl is None

    def test_price_and_size_are_decimal(self):
        c = make_client()
        t = c._parse_ws_message(_ws_msg(last_price="0.102488", last_qty="9756.0"))
        assert isinstance(t.price, Decimal)
        assert isinstance(t.size, Decimal)
        assert t.price == Decimal("0.102488")

    def test_symbol_resolved_to_display_name(self):
        c = make_client()
        t = c._parse_ws_message(_ws_msg(symbol="ETHUSDT"))
        assert t.market_symbol == "ETH"

    def test_unknown_symbol_gets_synthetic_id(self):
        c = make_client()
        t = c._parse_ws_message(_ws_msg(symbol="PEPEUSDT"))
        assert t is not None
        assert "PEPE" in t.market_symbol

    def test_non_dict_message_returns_none(self):
        c = make_client()
        assert c._parse_ws_message("not a dict") is None  # type: ignore

    def test_missing_o_field_returns_none(self):
        c = make_client()
        assert c._parse_ws_message({"e": "ORDER_TRADE_UPDATE"}) is None

    def test_hedge_mode_disables_client(self):
        c = make_client()
        c._hedge_mode = True
        # current_positions() returns {} immediately when hedge mode is on
        import asyncio
        result = asyncio.run(c.current_positions())
        assert result == {}


# ---------------------------------------------------------------------------
# market id helpers
# ---------------------------------------------------------------------------

class TestMarketId:
    def test_known_symbol_returns_id(self):
        c = make_client()
        assert c._market_id("BTCUSDT") == 0

    def test_unknown_symbol_gets_new_synthetic_id(self):
        c = make_client()
        mid = c._market_id("XRPUSDT")
        assert isinstance(mid, int)
        assert mid >= 4  # next after existing 0..3

    def test_same_unknown_symbol_stable_id(self):
        c = make_client()
        mid1 = c._market_id("XRPUSDT")
        mid2 = c._market_id("XRPUSDT")
        assert mid1 == mid2

    def test_display_symbol_strips_usdt(self):
        c = make_client()
        c._market_id("LINKUSDT")
        assert "LINK" in c._market_symbol(c._market_id("LINKUSDT"))


# ---------------------------------------------------------------------------
# Helpers for proxy-pool tests
# ---------------------------------------------------------------------------

def _make_mock_pool(
    *,
    working_url: Optional[str] = "socks5://proxy:1080",
    n_total: int = 3,
    n_alive: int = 1,
) -> MagicMock:
    """Build a MagicMock that satisfies the ProxyPool interface."""
    pool = MagicMock(spec=ProxyPool)
    pool.n_total = n_total
    pool.n_alive = n_alive

    # get_working returns working_url on first call; subsequent calls exhausted
    if working_url is not None:
        pool.get_working = AsyncMock(return_value=working_url)
    else:
        pool.get_working = AsyncMock(return_value=None)

    pool.mark_ok = MagicMock()
    pool.mark_dead = MagicMock()
    return pool


def _make_client_with_pool(pool: MagicMock) -> BinanceClient:
    c = BinanceClient(
        api_key="k",
        api_secret="s",
        source="test",
        rest_base="https://fapi.binance.com",
        proxy_pool=pool,
    )
    c._sym_to_id  = {"BTCUSDT": 0}
    c._id_to_disp = {0: "BTC"}
    c._id_to_full = {0: "BTCUSDT"}
    return c


def _mock_response(status_code: int = 200, json_data: dict | None = None) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json = MagicMock(return_value=json_data or {})
    resp.text = ""
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status_code}",
            request=MagicMock(),
            response=resp,
        )
    return resp


# ---------------------------------------------------------------------------
# BinanceUnavailable exception
# ---------------------------------------------------------------------------

class TestBinanceUnavailable:
    def test_is_exception(self):
        exc = BinanceUnavailable("test")
        assert isinstance(exc, Exception)

    def test_message_preserved(self):
        exc = BinanceUnavailable("proxy pool exhausted")
        assert "proxy pool exhausted" in str(exc)


# ---------------------------------------------------------------------------
# _request — proxy pool path
# ---------------------------------------------------------------------------

class TestRequestWithPool:
    """Tests for BinanceClient._request when a ProxyPool is configured.

    We patch httpx.AsyncClient to avoid any real network activity.
    """

    def test_success_marks_proxy_ok(self):
        pool = _make_mock_pool(working_url="socks5://proxy:1080")
        c = _make_client_with_pool(pool)

        ok_resp = _mock_response(200, {})

        async def _run():
            with patch("httpx.AsyncClient") as MockClient:
                ctx = MockClient.return_value.__aenter__.return_value
                ctx.request = AsyncMock(return_value=ok_resp)
                result = await c._request("GET", "/fapi/v1/ping")
                return result

        result = asyncio.run(_run())
        assert result.status_code == 200
        pool.mark_ok.assert_called_once()

    def test_451_marks_proxy_dead_and_raises_unavailable(self):
        """When every proxy returns 451, BinanceUnavailable is raised."""
        pool = _make_mock_pool(working_url="socks5://proxy:1080", n_total=1)
        # make get_working always return the same proxy (pool never exhausts in get_working)
        # but we cap attempts at min(n_total, 5) = 1
        c = _make_client_with_pool(pool)

        geo_resp = _mock_response(451)
        geo_resp.raise_for_status = MagicMock()  # don't raise on 451 — client checks status manually

        async def _run():
            with patch("httpx.AsyncClient") as MockClient:
                ctx = MockClient.return_value.__aenter__.return_value
                ctx.request = AsyncMock(return_value=geo_resp)
                await c._request("GET", "/fapi/v1/ping")

        with pytest.raises(BinanceUnavailable):
            asyncio.run(_run())
        pool.mark_dead.assert_called()

    def test_timeout_rotates_proxy_and_raises_unavailable(self):
        pool = _make_mock_pool(working_url="socks5://proxy:1080", n_total=1)
        c = _make_client_with_pool(pool)

        async def _run():
            with patch("httpx.AsyncClient") as MockClient:
                ctx = MockClient.return_value.__aenter__.return_value
                ctx.request = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
                await c._request("GET", "/fapi/v1/exchangeInfo")

        with pytest.raises(BinanceUnavailable):
            asyncio.run(_run())
        pool.mark_dead.assert_called()

    def test_connect_error_marks_dead_raises_unavailable(self):
        pool = _make_mock_pool(working_url="socks5://proxy:1080", n_total=1)
        c = _make_client_with_pool(pool)

        async def _run():
            with patch("httpx.AsyncClient") as MockClient:
                ctx = MockClient.return_value.__aenter__.return_value
                ctx.request = AsyncMock(side_effect=httpx.ConnectError("refused"))
                await c._request("GET", "/fapi/v1/exchangeInfo")

        with pytest.raises(BinanceUnavailable):
            asyncio.run(_run())
        pool.mark_dead.assert_called()

    def test_no_working_proxy_raises_unavailable_immediately(self):
        pool = _make_mock_pool(working_url=None)  # pool always returns None
        c = _make_client_with_pool(pool)

        async def _run():
            with patch("httpx.AsyncClient"):
                await c._request("GET", "/fapi/v1/ping")

        with pytest.raises(BinanceUnavailable):
            asyncio.run(_run())

    def test_second_proxy_succeeds_after_first_fails(self):
        """Rotation: first proxy times out, second proxy works."""
        pool = MagicMock(spec=ProxyPool)
        pool.n_total = 2
        pool.n_alive = 2
        # get_working returns different proxies on successive calls
        pool.get_working = AsyncMock(side_effect=[
            "socks5://bad:1080",
            "socks5://good:1080",
            None,  # safety sentinel
        ])
        pool.mark_ok = MagicMock()
        pool.mark_dead = MagicMock()

        c = _make_client_with_pool(pool)

        ok_resp = _mock_response(200, {})

        async def _run():
            call_count = 0

            async def _side_effect(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise httpx.TimeoutException("slow")
                return ok_resp

            with patch("httpx.AsyncClient") as MockClient:
                ctx = MockClient.return_value.__aenter__.return_value
                ctx.request = AsyncMock(side_effect=_side_effect)
                result = await c._request("GET", "/fapi/v1/ping")
                return result

        result = asyncio.run(_run())
        assert result.status_code == 200
        # First proxy was marked dead, second was marked ok
        pool.mark_dead.assert_called_once()
        pool.mark_ok.assert_called_once()


# ---------------------------------------------------------------------------
# _request — no-pool (legacy) path
# ---------------------------------------------------------------------------

class TestRequestNoPool:
    """When no proxy_pool is set, _request delegates to self._http directly."""

    def test_no_pool_uses_shared_client(self):
        c = make_client()  # no pool

        ok_resp = _mock_response(200, {})

        async def _run():
            c._http.request = AsyncMock(return_value=ok_resp)
            return await c._request("GET", "/fapi/v1/ping")

        result = asyncio.run(_run())
        assert result.status_code == 200


# ---------------------------------------------------------------------------
# ping()
# ---------------------------------------------------------------------------

class TestPing:
    def test_ping_returns_true_on_200(self):
        pool = _make_mock_pool(working_url="socks5://p:1080")
        c = _make_client_with_pool(pool)

        ok_resp = _mock_response(200, {})

        async def _run():
            with patch("httpx.AsyncClient") as MockClient:
                ctx = MockClient.return_value.__aenter__.return_value
                ctx.request = AsyncMock(return_value=ok_resp)
                return await c.ping()

        assert asyncio.run(_run()) is True

    def test_ping_returns_false_when_unavailable(self):
        pool = _make_mock_pool(working_url=None)
        c = _make_client_with_pool(pool)

        async def _run():
            with patch("httpx.AsyncClient"):
                return await c.ping()

        assert asyncio.run(_run()) is False

    def test_ping_returns_false_on_non_200(self):
        pool = _make_mock_pool(working_url="socks5://p:1080", n_total=1)
        c = _make_client_with_pool(pool)

        geo_resp = _mock_response(451)
        geo_resp.raise_for_status = MagicMock()

        async def _run():
            with patch("httpx.AsyncClient") as MockClient:
                ctx = MockClient.return_value.__aenter__.return_value
                ctx.request = AsyncMock(return_value=geo_resp)
                return await c.ping()

        assert asyncio.run(_run()) is False
