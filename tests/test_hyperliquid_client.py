"""Tests for HyperliquidClient.fetch_realizing_fills — no network calls.

Covers:
- Only realizing fills returned (non-zero closedPnl or dir starts with 'Close')
- market_id filtering works correctly
- SDK error (user_fills raises) → returns []
- Dedup by trade_id within a single call
- start_time_ms=None uses user_fills; paging via user_fills_by_time
- Oldest-first sort order
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from src.hyperliquid_client import HyperliquidClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_client() -> HyperliquidClient:
    """Create an HyperliquidClient without triggering any network calls.

    The HL SDK's Info.__init__ makes a live spot_meta() POST during construction.
    We patch it at the class level for the duration of the factory call.
    """
    with patch("hyperliquid.info.Info.__init__", return_value=None):
        c = HyperliquidClient(
            address="0xdeadbeefdeadbeefdeadbeefdeadbeef00001234",
            source="test",
        )
    # Re-create a fresh MagicMock as _info (the patch left it uninitialised)
    c._info = MagicMock()
    # Pre-populate coin map so BTC=0, ETH=1
    c._coin_to_id = {"BTC": 0, "ETH": 1}
    c._id_to_coin = {0: "BTC", 1: "ETH"}
    return c


def _raw_fill(
    *,
    tid: int,
    coin: str = "BTC",
    side: str = "B",
    sz: str = "0.5",
    px: str = "50000",
    closed_pnl: str = "0",
    dir_: str = "Open Long",
    time_ms: int = 1700000000000,
) -> dict:
    return {
        "tid": tid,
        "coin": coin,
        "side": side,
        "sz": sz,
        "px": px,
        "closedPnl": closed_pnl,
        "dir": dir_,
        "time": time_ms,
        "hash": f"0x{tid:064x}",
    }


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# fetch_realizing_fills — basic filtering
# ---------------------------------------------------------------------------

class TestFetchRealizingFillsFiltering:
    def _mock_fills(self) -> list[dict]:
        return [
            # Realizing: non-zero closedPnl
            _raw_fill(tid=10, closed_pnl="250.50", dir_="Close Long",  time_ms=1700000001000),
            # Realizing: dir starts with Close even if closedPnl is 0
            _raw_fill(tid=11, closed_pnl="0",      dir_="Close Short", time_ms=1700000002000),
            # NOT realizing: Open Long, zero closedPnl
            _raw_fill(tid=12, closed_pnl="0",      dir_="Open Long",   time_ms=1700000003000),
            # NOT realizing: Size Change, zero pnl
            _raw_fill(tid=13, closed_pnl="0",      dir_="Size Change", time_ms=1700000004000),
            # Realizing: negative closedPnl (a loss close)
            _raw_fill(tid=14, closed_pnl="-100.0", dir_="Close Long",  time_ms=1700000005000),
        ]

    def test_returns_only_realizing(self):
        c = make_client()
        fills = self._mock_fills()
        with patch.object(c._info, "user_fills", return_value=fills):
            result = _run(c.fetch_realizing_fills())
        tids = [t.trade_id for t in result]
        assert 10 in tids
        assert 11 in tids
        assert 14 in tids
        assert 12 not in tids
        assert 13 not in tids

    def test_open_long_excluded(self):
        c = make_client()
        fills = [_raw_fill(tid=99, closed_pnl="0", dir_="Open Long")]
        with patch.object(c._info, "user_fills", return_value=fills):
            result = _run(c.fetch_realizing_fills())
        assert result == []

    def test_realized_pnl_populated(self):
        c = make_client()
        fills = [_raw_fill(tid=20, closed_pnl="300.00", dir_="Close Long")]
        with patch.object(c._info, "user_fills", return_value=fills):
            result = _run(c.fetch_realizing_fills())
        assert len(result) == 1
        assert result[0].realized_pnl == Decimal("300.00")
        assert result[0].closed_pnl == Decimal("300.00")

    def test_oldest_first_order(self):
        c = make_client()
        fills = [
            _raw_fill(tid=30, closed_pnl="100", dir_="Close Long", time_ms=1700000003000),
            _raw_fill(tid=20, closed_pnl="200", dir_="Close Long", time_ms=1700000002000),
            _raw_fill(tid=10, closed_pnl="300", dir_="Close Long", time_ms=1700000001000),
        ]
        with patch.object(c._info, "user_fills", return_value=fills):
            result = _run(c.fetch_realizing_fills())
        assert [t.trade_id for t in result] == [10, 20, 30]


class TestFetchTradesSinceHip3Cursors:
    def test_prime_trade_anchors_warms_each_dex_without_returning_fills(self):
        c = make_client()
        fills = [
            _raw_fill(tid=100, coin="BTC"),
            _raw_fill(tid=7, coin="xyz:AMD"),
        ]
        with patch.object(c._info, "user_fills", return_value=fills):
            _run(c.prime_trade_anchors())

        assert c._last_tid_by_dex == {"": 100, "xyz": 7}

    def test_since_filter_uses_independent_dex_tid_anchors(self):
        c = make_client()
        c._last_tid_by_dex = {"": 100, "xyz": 1000}
        fills = [
            _raw_fill(tid=99, coin="BTC", time_ms=1700000001000),
            _raw_fill(tid=101, coin="BTC", time_ms=1700000002000),
            _raw_fill(tid=900, coin="xyz:AMD", time_ms=1700000003000),
            _raw_fill(tid=1001, coin="xyz:AMD", time_ms=1700000004000),
        ]
        with patch.object(c._info, "user_fills", return_value=fills):
            result = _run(c.fetch_trades_since(100))

        assert {(t.market_symbol, t.trade_id) for t in result} == {
            ("BTC", 101),
            ("XYZ:AMD", 1001),
        }

    def test_limit_keeps_recent_hip3_fills_when_default_tid_is_larger(self):
        c = make_client()
        fills = [
            _raw_fill(tid=900000 + i, coin="BTC", time_ms=1700000000000 + i * 1000)
            for i in range(3)
        ]
        fills.append(_raw_fill(tid=7, coin="xyz:AMD", time_ms=1700000004000))
        with patch.object(c._info, "user_fills", return_value=fills):
            result = _run(c.fetch_trades_since(None, limit=1))

        assert [(t.market_symbol, t.trade_id) for t in result] == [("XYZ:AMD", 7)]


class TestHip3PositionState:
    @staticmethod
    def _state(*positions: tuple[str, str, str, str]) -> dict:
        return {
            "assetPositions": [
                {
                    "position": {
                        "coin": coin,
                        "szi": size,
                        "entryPx": entry,
                        "unrealizedPnl": pnl,
                    }
                }
                for coin, size, entry, pnl in positions
            ]
        }

    def test_current_positions_queries_default_and_hip3_dexes(self):
        c = make_client()
        c._perp_dexes = ["", "xyz"]
        c._coin_to_id["XYZ:TSM"] = 110000
        c._id_to_coin[110000] = "XYZ:TSM"
        states = {
            "": self._state(("BTC", "0.5", "60000", "10")),
            "xyz": self._state(("xyz:TSM", "-3.1", "450", "-2")),
        }

        def fake_user_state(address, dex=""):
            return states[dex]

        c._info.user_state.side_effect = fake_user_state
        result = _run(c.current_positions())
        by_symbol = {position.market_symbol: position for position in result.values()}

        assert by_symbol["BTC"].side == "long"
        assert by_symbol["XYZ:TSM"].side == "short"
        assert by_symbol["XYZ:TSM"].size == Decimal("3.1")
        assert {call.args[1] for call in c._info.user_state.call_args_list} == {"", "xyz"}

    def test_fetch_leverage_uses_market_dex_state(self):
        c = make_client()
        c._coin_to_id["XYZ:TSM"] = 110000
        c._id_to_coin[110000] = "XYZ:TSM"

        def fake_user_state(address, dex=""):
            if dex == "xyz":
                return {
                    "assetPositions": [
                        {"position": {"coin": "xyz:TSM", "leverage": {"value": 10}}}
                    ]
                }
            return {"assetPositions": []}

        c._info.user_state.side_effect = fake_user_state

        assert _run(c.fetch_leverage(110000)) == 10.0
        assert c._info.user_state.call_args.args[1] == "xyz"


# ---------------------------------------------------------------------------
# fetch_realizing_fills — market_id filtering
# ---------------------------------------------------------------------------

class TestFetchRealizingFillsMarketId:
    def test_market_id_filter_btc(self):
        c = make_client()
        fills = [
            _raw_fill(tid=1, coin="BTC", closed_pnl="100", dir_="Close Long"),
            _raw_fill(tid=2, coin="ETH", closed_pnl="200", dir_="Close Long"),
        ]
        with patch.object(c._info, "user_fills", return_value=fills):
            result = _run(c.fetch_realizing_fills(market_id=0))  # BTC=0
        assert len(result) == 1
        assert result[0].market_id == 0
        assert result[0].market_symbol == "BTC"

    def test_market_id_filter_eth(self):
        c = make_client()
        fills = [
            _raw_fill(tid=1, coin="BTC", closed_pnl="100", dir_="Close Long"),
            _raw_fill(tid=2, coin="ETH", closed_pnl="200", dir_="Close Long"),
        ]
        with patch.object(c._info, "user_fills", return_value=fills):
            result = _run(c.fetch_realizing_fills(market_id=1))  # ETH=1
        assert len(result) == 1
        assert result[0].market_id == 1

    def test_no_market_id_returns_all(self):
        c = make_client()
        fills = [
            _raw_fill(tid=1, coin="BTC", closed_pnl="100", dir_="Close Long"),
            _raw_fill(tid=2, coin="ETH", closed_pnl="200", dir_="Close Short"),
        ]
        with patch.object(c._info, "user_fills", return_value=fills):
            result = _run(c.fetch_realizing_fills())
        assert len(result) == 2


# ---------------------------------------------------------------------------
# fetch_realizing_fills — SDK error handling
# ---------------------------------------------------------------------------

class TestFetchRealizingFillsErrorHandling:
    def test_sdk_error_returns_empty(self):
        c = make_client()
        with patch.object(c._info, "user_fills", side_effect=RuntimeError("network down")):
            result = _run(c.fetch_realizing_fills())
        assert result == []
        assert c.last_realizing_fetch_error == "RuntimeError"

    def test_non_list_response_returns_empty(self):
        c = make_client()
        with patch.object(c._info, "user_fills", return_value=None):
            result = _run(c.fetch_realizing_fills())
        assert result == []
        assert c.last_realizing_fetch_error == "non-list response"

    def test_non_list_string_response_returns_empty(self):
        c = make_client()
        with patch.object(c._info, "user_fills", return_value="error string"):
            result = _run(c.fetch_realizing_fills())
        assert result == []
        assert c.last_realizing_fetch_error == "non-list response"

    def test_successful_empty_response_is_authoritative(self):
        c = make_client()
        with patch.object(c._info, "user_fills", return_value=[]):
            result = _run(c.fetch_realizing_fills())
        assert result == []
        assert c.last_realizing_fetch_error is None

    def test_partial_malformed_fills_skipped(self):
        """Malformed fills (missing tid) are skipped; valid ones still returned."""
        c = make_client()
        fills = [
            {"coin": "BTC", "sz": "1", "px": "50000", "closedPnl": "100", "dir": "Close Long",
             "side": "B", "time": 1700000001000, "hash": "0xbad"},   # missing tid -> skipped
            _raw_fill(tid=5, closed_pnl="50", dir_="Close Long"),
        ]
        with patch.object(c._info, "user_fills", return_value=fills):
            result = _run(c.fetch_realizing_fills())
        assert len(result) == 1
        assert result[0].trade_id == 5

    def test_dedup_same_trade_id(self):
        """Duplicate trade_ids in the response are de-duped."""
        c = make_client()
        fills = [
            _raw_fill(tid=7, closed_pnl="100", dir_="Close Long"),
            _raw_fill(tid=7, closed_pnl="100", dir_="Close Long"),
        ]
        with patch.object(c._info, "user_fills", return_value=fills):
            result = _run(c.fetch_realizing_fills())
        assert len(result) == 1
        assert result[0].trade_id == 7

    def test_same_tid_in_different_hip3_dex_namespaces_is_kept(self):
        c = make_client()
        fills = [
            _raw_fill(tid=7, coin="xyz:AMD", closed_pnl="100", dir_="Close Long"),
            _raw_fill(tid=7, coin="flx:BTC", closed_pnl="200", dir_="Close Long"),
        ]
        with patch.object(c._info, "user_fills", return_value=fills):
            result = _run(c.fetch_realizing_fills())

        assert {(t.market_symbol, t.trade_id) for t in result} == {
            ("XYZ:AMD", 7),
            ("FLX:BTC", 7),
        }


# ---------------------------------------------------------------------------
# fetch_realizing_fills — with start_time_ms (uses user_fills_by_time)
# ---------------------------------------------------------------------------

class TestFetchRealizingFillsByTime:
    def test_uses_user_fills_by_time_when_start_given(self):
        c = make_client()
        fills = [_raw_fill(tid=100, closed_pnl="500", dir_="Close Long")]

        called_with = {}
        def fake_by_time(address, start_time, end_time=None, aggregate_by_time=False):
            called_with["start"] = start_time
            called_with["end"] = end_time
            return fills

        with patch.object(c._info, "user_fills_by_time", side_effect=fake_by_time):
            result = _run(c.fetch_realizing_fills(start_time_ms=1700000000000))

        assert called_with["start"] == 1700000000000
        assert len(result) == 1
        assert result[0].trade_id == 100

    def test_by_time_error_returns_empty(self):
        c = make_client()
        with patch.object(c._info, "user_fills_by_time", side_effect=Exception("timeout")):
            result = _run(c.fetch_realizing_fills(start_time_ms=1700000000000))
        assert result == []

    def test_by_time_dedup_across_pages(self):
        """Fills returned in overlapping pages are de-duped."""
        c = make_client()
        page1 = [_raw_fill(tid=1, closed_pnl="100", dir_="Close Long", time_ms=1700000001000)]
        page2 = [_raw_fill(tid=1, closed_pnl="100", dir_="Close Long", time_ms=1700000001000)]

        call_count = 0
        def fake_by_time(address, start_time, end_time=None, aggregate_by_time=False):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return page1
            return []  # stop paging

        with patch.object(c._info, "user_fills_by_time", side_effect=fake_by_time):
            result = _run(c.fetch_realizing_fills(start_time_ms=1699999999000))
        assert len(result) == 1


# ---------------------------------------------------------------------------
# fetch_realizing_fills — limit cap
# ---------------------------------------------------------------------------

class TestFetchRealizingFillsLimit:
    def test_limit_caps_results(self):
        c = make_client()
        fills = [
            _raw_fill(tid=i, closed_pnl="10", dir_="Close Long", time_ms=1700000000000 + i * 1000)
            for i in range(10)
        ]
        with patch.object(c._info, "user_fills", return_value=fills):
            result = _run(c.fetch_realizing_fills(limit=3))
        # Returns the 3 NEWEST (tail) after oldest-first sort
        assert len(result) == 3
        assert result[-1].trade_id == 9

    def test_cross_dex_order_and_limit_use_timestamp_not_global_tid(self):
        c = make_client()
        fills = [
            _raw_fill(tid=900, coin="BTC", closed_pnl="9", dir_="Close Long", time_ms=1700000003000),
            _raw_fill(tid=2, coin="xyz:XYZ100", closed_pnl="2", dir_="Close Long", time_ms=1700000001000),
            _raw_fill(tid=3, coin="xyz:XYZ100", closed_pnl="3", dir_="Close Long", time_ms=1700000002000),
        ]
        with patch.object(c._info, "user_fills", return_value=fills):
            result = _run(c.fetch_realizing_fills(limit=2))

        assert [(t.market_symbol, t.trade_id) for t in result] == [
            ("XYZ:XYZ100", 3),
            ("BTC", 900),
        ]


# ---------------------------------------------------------------------------
# Regression test: multi-coin cascade bug
#
# Before the fix, _parse_fill used _coin_to_id for the perp filter.
# _market_id() adds synthetic entries to _coin_to_id as a side-effect, so
# after parsing the first fill the map became non-empty; every subsequent
# fill for a *different* coin then failed "coin.upper() not in _coin_to_id"
# and was silently dropped — only 1 coin survived.
#
# The fix: _parse_fill now uses the stable _perp_universe set (populated once
# by bootstrap_markets()) instead of the mutating _coin_to_id map.
# ---------------------------------------------------------------------------

class TestMultiCoinCascadeBug:
    """Regression test: all perp coins kept, spot coin dropped."""

    def _make_bootstrapped_client(self) -> HyperliquidClient:
        """Client with _perp_universe pre-populated for BTC, ETH, RENDER."""
        with patch("hyperliquid.info.Info.__init__", return_value=None):
            c = HyperliquidClient(
                address="0xdeadbeefdeadbeefdeadbeefdeadbeef00001234",
                source="test",
            )
        c._info = MagicMock()
        # Simulate what bootstrap_markets() does: populate all three maps.
        c._coin_to_id = {"BTC": 0, "ETH": 1, "RENDER": 2}
        c._id_to_coin = {0: "BTC", 1: "ETH", 2: "RENDER"}
        c._perp_universe = {"BTC", "ETH", "RENDER"}
        return c

    def test_all_perp_coins_kept_not_just_first(self):
        """Parsing BTC, ETH, RENDER fills all return non-None trades."""
        c = self._make_bootstrapped_client()
        raw_fills = [
            _raw_fill(tid=1, coin="BTC",    closed_pnl="100", dir_="Close Long"),
            _raw_fill(tid=2, coin="ETH",    closed_pnl="200", dir_="Close Long"),
            _raw_fill(tid=3, coin="RENDER", closed_pnl="50",  dir_="Close Short"),
        ]
        with patch.object(c._info, "user_fills", return_value=raw_fills):
            result = _run(c.fetch_realizing_fills())

        symbols = {t.market_symbol for t in result}
        assert "BTC" in symbols,    "BTC fills must not be dropped"
        assert "ETH" in symbols,    "ETH fills must not be dropped"
        assert "RENDER" in symbols, "RENDER fills must not be dropped"
        assert len(result) == 3

    def test_spot_fill_dropped_hip3_and_default_perps_kept(self):
        """Spot '@index' fills are dropped; default and HIP-3 perp coins survive."""
        c = self._make_bootstrapped_client()
        raw_fills = [
            _raw_fill(tid=10, coin="BTC",         closed_pnl="100", dir_="Close Long"),
            _raw_fill(tid=11, coin="ETH",         closed_pnl="200", dir_="Close Long"),
            _raw_fill(tid=12, coin="@107",        closed_pnl="5",   dir_="Sell"),
            _raw_fill(tid=13, coin="RENDER",      closed_pnl="30",  dir_="Close Short"),
            _raw_fill(tid=14, coin="rwa:SKHYNIX", closed_pnl="25",  dir_="Close Long"),
        ]
        with patch.object(c._info, "user_fills", return_value=raw_fills):
            result = _run(c.fetch_realizing_fills())

        tids = {t.trade_id for t in result}
        symbols = {t.market_symbol for t in result}
        assert 10 in tids, "BTC fill must be kept"
        assert 11 in tids, "ETH fill must be kept"
        assert 12 not in tids, "spot fill '@107' must be dropped by perp filter"
        assert 13 in tids, "RENDER fill must be kept"
        assert 14 in tids, "HIP-3 stock perp fill must be kept"
        assert "RWA:SKHYNIX" in symbols
        assert len(result) == 4

    def test_empty_perp_universe_skips_filter_parses_all(self):
        """When _perp_universe is empty (no bootstrap), the filter is skipped."""
        with patch("hyperliquid.info.Info.__init__", return_value=None):
            c = HyperliquidClient(
                address="0xdeadbeefdeadbeefdeadbeefdeadbeef00001234",
                source="test",
            )
        c._info = MagicMock()
        # Intentionally leave _perp_universe empty (no bootstrap)
        assert c._perp_universe == set()
        raw_fills = [
            _raw_fill(tid=1, coin="BTC", closed_pnl="100", dir_="Close Long"),
            _raw_fill(tid=2, coin="SOL", closed_pnl="50",  dir_="Close Long"),
        ]
        with patch.object(c._info, "user_fills", return_value=raw_fills):
            result = _run(c.fetch_realizing_fills())
        # Without a universe, all fills pass the filter
        assert len(result) == 2
