"""Tests for src.stats.filter_trades — the stats display-window filter.

Covers the date range (start/end, end None ⇒ now, date-only end inclusive),
the symbol whitelist (quote-suffix tolerant), unparseable-ts handling, and the
newest-first date sort.
"""

from __future__ import annotations

from src.stats import filter_trades


def _rec(ts: str, sym: str) -> dict:
    return {"ts": ts, "market_symbol": sym, "pnl": "1.0", "side": "long", "is_win": 1}


SAMPLE = [
    _rec("2026-06-03T10:00:00Z", "ETH"),
    _rec("2026-05-01T09:00:00Z", "BTC"),
    _rec("2026-04-10T09:00:00Z", "ARB"),
    _rec("2026-06-01T12:00:00Z", "PUMP"),
]


def _syms(rows: list[dict]) -> list[str]:
    return [r["market_symbol"] for r in rows]


def test_no_filter_sorts_newest_first():
    assert _syms(filter_trades(SAMPLE)) == ["ETH", "PUMP", "BTC", "ARB"]


def test_start_date_lower_bound_inclusive():
    out = filter_trades(SAMPLE, start_date="2026-05-01")
    assert _syms(out) == ["ETH", "PUMP", "BTC"]  # ARB (Apr 10) dropped, BTC kept


def test_end_date_none_means_now():
    # No upper bound ⇒ everything on/after start is kept.
    out = filter_trades(SAMPLE, start_date="2026-04-01", end_date=None)
    assert _syms(out) == ["ETH", "PUMP", "BTC", "ARB"]


def test_date_only_end_is_inclusive_end_of_day():
    # end=2026-06-01 must include the 12:00 trade that day, exclude Jun 3.
    out = filter_trades(SAMPLE, start_date="2026-05-01", end_date="2026-06-01")
    assert _syms(out) == ["PUMP", "BTC"]


def test_datetime_end_is_exact():
    out = filter_trades(SAMPLE, start_date="2026-05-01", end_date="2026-06-01T00:00:00Z")
    assert _syms(out) == ["BTC"]  # PUMP at 12:00 is after the exact midnight bound


def test_symbol_whitelist_quote_suffix_tolerant():
    out = filter_trades(SAMPLE, symbols=["ETHUSDT", "btc"])
    assert _syms(out) == ["ETH", "BTC"]


def test_empty_symbol_whitelist_keeps_all():
    assert len(filter_trades(SAMPLE, symbols=[])) == 4
    assert len(filter_trades(SAMPLE, symbols=None)) == 4


def test_unparseable_ts_dropped_when_date_bound_set():
    rows = SAMPLE + [_rec("not-a-date", "DOGE")]
    out = filter_trades(rows, start_date="2026-01-01")
    assert "DOGE" not in _syms(out)


def test_unparseable_ts_kept_when_no_date_bound():
    rows = SAMPLE + [_rec("not-a-date", "DOGE")]
    out = filter_trades(rows, symbols=["DOGE", "ETH"])
    assert set(_syms(out)) == {"DOGE", "ETH"}
    # unparseable ts sorts to the very end
    assert _syms(out)[-1] == "DOGE"


def test_combined_date_and_symbol_filter():
    out = filter_trades(
        SAMPLE, start_date="2026-05-01", end_date=None, symbols=["BTC", "PUMP"]
    )
    assert _syms(out) == ["PUMP", "BTC"]
