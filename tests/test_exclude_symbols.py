"""Tests for per-source exclude_symbols: normalization, is_excluded, and config parsing."""

from __future__ import annotations

import pytest

from src.sources import _normalize_symbol, Source, _build_source
from src.position_tracker import PositionTracker


# ---------------------------------------------------------------------------
# _normalize_symbol
# ---------------------------------------------------------------------------

class TestNormalizeSymbol:
    def test_fartcoinusd_normalizes_to_fartcoin(self):
        assert _normalize_symbol("fartcoinusd") == "FARTCOIN"

    def test_fartcoin_already_clean(self):
        assert _normalize_symbol("FARTCOIN") == "FARTCOIN"

    def test_fartcoin_perp(self):
        assert _normalize_symbol("FARTCOIN-PERP") == "FARTCOIN"

    def test_fartcoin_perp_no_dash(self):
        assert _normalize_symbol("FARTCOINPERP") == "FARTCOIN"

    def test_btcusdt(self):
        assert _normalize_symbol("btcusdt") == "BTC"

    def test_btcusdc(self):
        assert _normalize_symbol("BTCUSDC") == "BTC"

    def test_plain_doge(self):
        assert _normalize_symbol("DOGE") == "DOGE"

    def test_plain_doge_lowercase(self):
        assert _normalize_symbol("doge") == "DOGE"

    def test_empty_string_safe(self):
        # Should not raise; returns empty string
        result = _normalize_symbol("")
        assert isinstance(result, str)

    def test_garbage_safe(self):
        # Arbitrary garbage should not raise
        result = _normalize_symbol("!!!@@@")
        assert isinstance(result, str)

    def test_strips_usd_suffix(self):
        assert _normalize_symbol("ETHUSD") == "ETH"

    def test_does_not_strip_suffix_if_that_is_the_whole_symbol(self):
        # "USD" alone — after stripping "USD" nothing remains, so it stays "USD"
        result = _normalize_symbol("USD")
        # len("USD") == len(suffix "USD") so the guard `len(s) > len(suffix)` prevents stripping
        assert result == "USD"


# ---------------------------------------------------------------------------
# Source.is_excluded
# ---------------------------------------------------------------------------

class _DummyClient:
    """Minimal stand-in so Source() can be constructed without a real client."""
    source = "test"

    async def bootstrap_markets(self): return {}
    async def current_positions(self): return {}
    async def fetch_trades_since(self, since_trade_id, limit=100): return []
    def stream_trades(self): pass
    async def fetch_leverage(self, market_id): return None
    async def fetch_sl_tp(self, market_id): return None, None
    async def close(self): pass


def _make_source(exclude_symbols: frozenset[str]) -> Source:
    return Source(
        id="test:0",
        name="Test",
        client=_DummyClient(),
        tracker=PositionTracker(source="test"),
        url="",
        min_notional=__import__("decimal").Decimal("0"),
        exclude_symbols=exclude_symbols,
    )


class TestIsExcluded:
    def test_exact_match(self):
        src = _make_source(frozenset({"FARTCOIN"}))
        assert src.is_excluded("FARTCOIN") is True

    def test_lowercase_input_normalized(self):
        src = _make_source(frozenset({"FARTCOIN"}))
        assert src.is_excluded("fartcoinusd") is True

    def test_perp_suffix_normalized(self):
        src = _make_source(frozenset({"FARTCOIN"}))
        assert src.is_excluded("FARTCOIN-PERP") is True

    def test_unrelated_symbol_not_excluded(self):
        src = _make_source(frozenset({"FARTCOIN"}))
        assert src.is_excluded("BTC") is False

    def test_empty_exclude_set_always_false(self):
        src = _make_source(frozenset())
        assert src.is_excluded("FARTCOIN") is False
        assert src.is_excluded("BTC") is False

    def test_multiple_excludes(self):
        src = _make_source(frozenset({"FARTCOIN", "DOGE"}))
        assert src.is_excluded("DOGE") is True
        assert src.is_excluded("BTC") is False


# ---------------------------------------------------------------------------
# Config parsing: exclude_symbols flows through _build_source correctly
# ---------------------------------------------------------------------------

class TestConfigParsing:
    def test_lighter_source_parses_exclude_symbols(self):
        """_build_source with a lighter source normalizes and stores exclude_symbols."""
        raw = {
            "type": "lighter",
            "name": "Test Pool",
            "pool_id": 12345,
            "exclude_symbols": ["fartcoinusd"],
        }
        src = _build_source(raw)
        assert src is not None
        assert src.exclude_symbols == frozenset({"FARTCOIN"})

    def test_lighter_source_multiple_symbols(self):
        raw = {
            "type": "lighter",
            "name": "Test Pool",
            "pool_id": 12345,
            "exclude_symbols": ["fartcoinusd", "DOGE", "btcusdt"],
        }
        src = _build_source(raw)
        assert src is not None
        assert src.exclude_symbols == frozenset({"FARTCOIN", "DOGE", "BTC"})

    def test_no_exclude_symbols_gives_empty_frozenset(self):
        raw = {
            "type": "lighter",
            "name": "Test Pool",
            "pool_id": 12345,
        }
        src = _build_source(raw)
        assert src is not None
        assert src.exclude_symbols == frozenset()

    def test_source_parses_exclude_symbols_case_insensitive(self):
        """exclude_symbols from config are normalized regardless of input case."""
        raw = {
            "type": "lighter",
            "name": "Test Pool",
            "pool_id": 12345,
            "exclude_symbols": ["FaRtCoIn", "BTCUSDT"],
        }
        src = _build_source(raw)
        assert src is not None
        assert src.exclude_symbols == frozenset({"FARTCOIN", "BTC"})

    def test_is_excluded_uses_parsed_config(self):
        """End-to-end: config value 'fartcoinusd' → is_excluded('FARTCOIN-PERP') is True."""
        raw = {
            "type": "lighter",
            "name": "Test Pool",
            "pool_id": 12345,
            "exclude_symbols": ["fartcoinusd"],
        }
        src = _build_source(raw)
        assert src is not None
        assert src.is_excluded("FARTCOIN-PERP") is True
        assert src.is_excluded("BTC") is False
