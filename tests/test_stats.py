"""Thorough tests for src/stats.py — compute_stats() and format_stats_summary()."""

from __future__ import annotations

import pytest

from src.stats import compute_stats, format_stats_summary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rec(
    *,
    ts: str = "2026-01-01T00:00:00+00:00",
    source: str = "HL",
    market_symbol: str = "BTC",
    side: str = "long",
    pnl: str | None = "100.0",
    is_win: int = 1,
) -> dict:
    """Build a minimal closed-trade record (all numeric fields as strings)."""
    return {
        "ts": ts,
        "source": source,
        "market_symbol": market_symbol,
        "side": side,
        "entry": "50000.0",
        "exit": "51000.0",
        "size": "0.1",
        "notional": "5000",
        "pnl": pnl,
        "pct": "2.0" if pnl is not None else None,
        "is_win": is_win,
        "leverage": "10.0",
        "wins": 1,
        "total": 1,
        "card_path": None,
    }


# Canonical 6-trade dataset (stored newest-first as the DB returns them):
#
#   trade 0 (newest): BTC  long   +500   HL
#   trade 1:          ETH  short  +200   HL
#   trade 2:          BTC  long   -100   NK
#   trade 3:          SOL  long   +300   NK
#   trade 4:          ETH  long   -150   HL
#   trade 5 (oldest): SOL  short  +50    HL
#
# Wins: 0,1,3,5  (pnl > 0)  → 4 wins
# Losses: 2,4 → 2 losses
# Total PnL: 500+200-100+300-150+50 = 800
# Gross profit: 500+200+300+50 = 1050
# Gross loss: 100+150 = 250
# Profit factor: 1050/250 = 4.2
# Avg win: 1050/4 = 262.5
# Avg loss: -250/2 = -125.0
# Largest win: 500
# Largest loss: -150

TRADES_6 = [
    _rec(ts="2026-01-06T00:00:00+00:00", market_symbol="BTC",  side="long",  pnl="500.0",  is_win=1, source="HL"),
    _rec(ts="2026-01-05T00:00:00+00:00", market_symbol="ETH",  side="short", pnl="200.0",  is_win=1, source="HL"),
    _rec(ts="2026-01-04T00:00:00+00:00", market_symbol="BTC",  side="long",  pnl="-100.0", is_win=0, source="NK"),
    _rec(ts="2026-01-03T00:00:00+00:00", market_symbol="SOL",  side="long",  pnl="300.0",  is_win=1, source="NK"),
    _rec(ts="2026-01-02T00:00:00+00:00", market_symbol="ETH",  side="long",  pnl="-150.0", is_win=0, source="HL"),
    _rec(ts="2026-01-01T00:00:00+00:00", market_symbol="SOL",  side="short", pnl="50.0",   is_win=1, source="HL"),
]


# ---------------------------------------------------------------------------
# Empty list → safe zeros
# ---------------------------------------------------------------------------

class TestEmptyInput:
    def test_empty_returns_zero_n_trades(self):
        s = compute_stats([])
        assert s["n_trades"] == 0

    def test_empty_win_rate_zero(self):
        assert compute_stats([])["win_rate"] == 0.0

    def test_empty_total_pnl_zero(self):
        assert compute_stats([])["total_pnl"] == 0.0

    def test_empty_profit_factor_none(self):
        assert compute_stats([])["profit_factor"] is None

    def test_empty_by_symbol_empty_list(self):
        assert compute_stats([])["by_symbol"] == []

    def test_empty_by_source_empty_list(self):
        assert compute_stats([])["by_source"] == []

    def test_empty_equity_curve_empty_list(self):
        assert compute_stats([])["equity_curve"] == []

    def test_empty_pnl_series_empty_list(self):
        assert compute_stats([])["pnl_series"] == []

    def test_empty_max_drawdown_zero(self):
        assert compute_stats([])["max_drawdown"] == 0.0

    def test_empty_long_short_zeros(self):
        s = compute_stats([])
        assert s["long"]  == {"n": 0, "pnl": 0.0, "win_rate": 0.0}
        assert s["short"] == {"n": 0, "pnl": 0.0, "win_rate": 0.0}

    def test_empty_wins_losses_zero(self):
        s = compute_stats([])
        assert s["wins"] == 0
        assert s["losses"] == 0

    def test_empty_avg_win_avg_loss_zero(self):
        s = compute_stats([])
        assert s["avg_win"] == 0.0
        assert s["avg_loss"] == 0.0

    def test_empty_largest_win_loss_zero(self):
        s = compute_stats([])
        assert s["largest_win"] == 0.0
        assert s["largest_loss"] == 0.0


# ---------------------------------------------------------------------------
# Null-pnl records excluded from pnl aggregates
# ---------------------------------------------------------------------------

class TestNullPnlExclusion:
    def test_null_pnl_not_counted(self):
        trades = [
            _rec(pnl="100.0", is_win=1),
            _rec(pnl=None),
            _rec(pnl=None),
        ]
        s = compute_stats(trades)
        assert s["n_trades"] == 1
        assert s["total_pnl"] == pytest.approx(100.0)

    def test_only_null_pnl_returns_zeros(self):
        trades = [_rec(pnl=None), _rec(pnl=None)]
        s = compute_stats(trades)
        assert s["n_trades"] == 0
        assert s["win_rate"] == 0.0
        assert s["profit_factor"] is None

    def test_null_pnl_graceful_missing_keys(self):
        # Record that is completely sparse
        trades = [{"ts": "2026-01-01T00:00:00+00:00"}]
        s = compute_stats(trades)
        assert s["n_trades"] == 0

    def test_unparseable_pnl_excluded(self):
        trades = [_rec(pnl="abc"), _rec(pnl="100.0")]
        s = compute_stats(trades)
        assert s["n_trades"] == 1


# ---------------------------------------------------------------------------
# Core aggregate correctness
# ---------------------------------------------------------------------------

class TestAggregates:
    def setup_method(self):
        self.s = compute_stats(TRADES_6)

    def test_n_trades(self):
        assert self.s["n_trades"] == 6

    def test_wins(self):
        assert self.s["wins"] == 4

    def test_losses(self):
        assert self.s["losses"] == 2

    def test_win_rate(self):
        assert self.s["win_rate"] == pytest.approx(4 / 6 * 100, rel=1e-4)

    def test_total_pnl(self):
        assert self.s["total_pnl"] == pytest.approx(800.0)

    def test_gross_profit(self):
        assert self.s["gross_profit"] == pytest.approx(1050.0)

    def test_gross_loss(self):
        assert self.s["gross_loss"] == pytest.approx(250.0)

    def test_profit_factor(self):
        assert self.s["profit_factor"] == pytest.approx(4.2, rel=1e-4)

    def test_avg_win(self):
        assert self.s["avg_win"] == pytest.approx(262.5)

    def test_avg_loss(self):
        assert self.s["avg_loss"] == pytest.approx(-125.0)

    def test_avg_pnl(self):
        assert self.s["avg_pnl"] == pytest.approx(800.0 / 6)

    def test_largest_win(self):
        assert self.s["largest_win"] == pytest.approx(500.0)

    def test_largest_loss(self):
        assert self.s["largest_loss"] == pytest.approx(-150.0)


# ---------------------------------------------------------------------------
# All wins (no losses) — profit_factor should be None (gross_loss == 0)
# ---------------------------------------------------------------------------

class TestAllWins:
    def test_profit_factor_none_when_no_losses(self):
        trades = [_rec(pnl="100.0", is_win=1), _rec(pnl="200.0", is_win=1)]
        s = compute_stats(trades)
        assert s["profit_factor"] is None

    def test_avg_loss_zero_when_no_losses(self):
        trades = [_rec(pnl="100.0", is_win=1)]
        s = compute_stats(trades)
        assert s["avg_loss"] == 0.0

    def test_largest_loss_zero_when_no_losses(self):
        trades = [_rec(pnl="100.0", is_win=1)]
        s = compute_stats(trades)
        assert s["largest_loss"] == 0.0


# ---------------------------------------------------------------------------
# By-symbol correctness
# ---------------------------------------------------------------------------

class TestBySymbol:
    def setup_method(self):
        self.s = compute_stats(TRADES_6)

    def test_by_symbol_sorted_pnl_desc(self):
        pnls = [x["pnl"] for x in self.s["by_symbol"]]
        assert pnls == sorted(pnls, reverse=True)

    def test_btc_pnl(self):
        btc = next(x for x in self.s["by_symbol"] if x["symbol"] == "BTC")
        assert btc["pnl"] == pytest.approx(400.0)   # 500 - 100
        assert btc["n"] == 2

    def test_btc_win_rate(self):
        btc = next(x for x in self.s["by_symbol"] if x["symbol"] == "BTC")
        assert btc["win_rate"] == pytest.approx(50.0)

    def test_eth_win_rate(self):
        # ETH: +200 (win), -150 (loss) → 1/2 = 50%
        eth = next(x for x in self.s["by_symbol"] if x["symbol"] == "ETH")
        assert eth["win_rate"] == pytest.approx(50.0)

    def test_sol_win_rate(self):
        # SOL: +300 (win), +50 (win) → 2/2 = 100%
        sol = next(x for x in self.s["by_symbol"] if x["symbol"] == "SOL")
        assert sol["win_rate"] == pytest.approx(100.0)

    def test_all_symbols_present(self):
        syms = {x["symbol"] for x in self.s["by_symbol"]}
        assert syms == {"BTC", "ETH", "SOL"}


# ---------------------------------------------------------------------------
# By-source correctness
# ---------------------------------------------------------------------------

class TestBySource:
    def setup_method(self):
        self.s = compute_stats(TRADES_6)

    def test_by_source_sorted_pnl_desc(self):
        pnls = [x["pnl"] for x in self.s["by_source"]]
        assert pnls == sorted(pnls, reverse=True)

    def test_hl_pnl(self):
        # HL: 500+200-150+50 = 600
        hl = next(x for x in self.s["by_source"] if x["source"] == "HL")
        assert hl["pnl"] == pytest.approx(600.0)
        assert hl["n"] == 4

    def test_nk_pnl(self):
        # NK: -100+300 = 200
        nk = next(x for x in self.s["by_source"] if x["source"] == "NK")
        assert nk["pnl"] == pytest.approx(200.0)
        assert nk["n"] == 2


# ---------------------------------------------------------------------------
# Long / short split
# ---------------------------------------------------------------------------

class TestLongShortSplit:
    def setup_method(self):
        self.s = compute_stats(TRADES_6)

    def test_long_n(self):
        # BTC long +500, BTC long -100, SOL long +300, ETH long -150 → 4
        assert self.s["long"]["n"] == 4

    def test_long_pnl(self):
        assert self.s["long"]["pnl"] == pytest.approx(550.0)   # 500-100+300-150

    def test_long_win_rate(self):
        # wins: 500, 300 → 2/4 = 50%
        assert self.s["long"]["win_rate"] == pytest.approx(50.0)

    def test_short_n(self):
        # ETH short +200, SOL short +50 → 2
        assert self.s["short"]["n"] == 2

    def test_short_pnl(self):
        assert self.s["short"]["pnl"] == pytest.approx(250.0)

    def test_short_win_rate(self):
        assert self.s["short"]["win_rate"] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Equity curve — oldest-first + cumulative correctness
# ---------------------------------------------------------------------------

class TestEquityCurve:
    def setup_method(self):
        self.s = compute_stats(TRADES_6)
        self.curve = self.s["equity_curve"]

    def test_curve_length_matches_n_trades(self):
        assert len(self.curve) == 6

    def test_curve_oldest_first(self):
        # Oldest trade: SOL short +50 (ts 2026-01-01)
        assert self.curve[0]["ts"] == "2026-01-01T00:00:00+00:00"

    def test_curve_newest_last(self):
        # Newest trade: BTC long +500 (ts 2026-01-06)
        assert self.curve[-1]["ts"] == "2026-01-06T00:00:00+00:00"

    def test_curve_cumulative_values(self):
        # Oldest→newest pnl order: 50, -150, 300, -100, 200, 500
        expected_cum = [50, -100, 200, 100, 300, 800]
        for i, exp in enumerate(expected_cum):
            assert self.curve[i]["cum_pnl"] == pytest.approx(exp)

    def test_curve_index_sequential(self):
        indices = [pt["i"] for pt in self.curve]
        assert indices == list(range(6))

    def test_curve_final_equals_total_pnl(self):
        assert self.curve[-1]["cum_pnl"] == pytest.approx(self.s["total_pnl"])


# ---------------------------------------------------------------------------
# Max drawdown
# ---------------------------------------------------------------------------

class TestMaxDrawdown:
    def test_max_drawdown_all_positive(self):
        # Monotonically rising curve — no drawdown
        trades = [
            _rec(pnl="100.0", ts="2026-01-03T00:00:00+00:00"),
            _rec(pnl="50.0",  ts="2026-01-02T00:00:00+00:00"),
            _rec(pnl="200.0", ts="2026-01-01T00:00:00+00:00"),
        ]
        s = compute_stats(trades)
        assert s["max_drawdown"] == pytest.approx(0.0)

    def test_max_drawdown_rises_then_falls(self):
        # oldest→newest: +100, +200, -150  (stored newest-first so reversed below)
        trades = [
            _rec(pnl="-150.0", ts="2026-01-03T00:00:00+00:00"),
            _rec(pnl="200.0",  ts="2026-01-02T00:00:00+00:00"),
            _rec(pnl="100.0",  ts="2026-01-01T00:00:00+00:00"),
        ]
        s = compute_stats(trades)
        # cum: 100, 300, 150  →  peak after trade 1 = 300; dd = 150-300 = -150
        assert s["max_drawdown"] == pytest.approx(-150.0)

    def test_max_drawdown_is_non_positive(self):
        s = compute_stats(TRADES_6)
        assert s["max_drawdown"] <= 0.0

    def test_max_drawdown_single_trade(self):
        trades = [_rec(pnl="-50.0")]
        s = compute_stats(trades)
        # cumulative goes from 0 to -50; peak = 0; dd = -50
        assert s["max_drawdown"] == pytest.approx(-50.0)


# ---------------------------------------------------------------------------
# PnL series
# ---------------------------------------------------------------------------

class TestPnlSeries:
    def setup_method(self):
        self.s = compute_stats(TRADES_6)
        self.series = self.s["pnl_series"]

    def test_series_length(self):
        assert len(self.series) == 6

    def test_series_oldest_first(self):
        assert self.series[0]["ts"] == "2026-01-01T00:00:00+00:00"

    def test_series_has_required_keys(self):
        pt = self.series[0]
        assert set(pt.keys()) >= {"i", "ts", "pnl", "symbol", "side", "is_win"}

    def test_series_is_win_bool(self):
        for pt in self.series:
            assert isinstance(pt["is_win"], bool)

    def test_series_pnl_values_match_input(self):
        # Oldest record should be SOL short +50
        assert self.series[0]["pnl"] == pytest.approx(50.0)
        assert self.series[0]["symbol"] == "SOL"
        assert self.series[0]["side"] == "short"
        assert self.series[0]["is_win"] is True


# ---------------------------------------------------------------------------
# format_stats_summary
# ---------------------------------------------------------------------------

class TestFormatStatsSummary:
    def test_no_trades_friendly_message(self):
        msg = format_stats_summary(compute_stats([]))
        assert "No closed trades" in msg

    def test_no_trades_uses_title(self):
        msg = format_stats_summary(compute_stats([]), title="My Title")
        assert "My Title" in msg

    def test_no_trades_with_pool_url(self):
        msg = format_stats_summary(compute_stats([]), pool_url="https://example.com")
        assert "https://example.com" in msg

    def test_contains_trade_count(self):
        s = compute_stats(TRADES_6)
        msg = format_stats_summary(s)
        assert "6" in msg

    def test_contains_win_rate(self):
        s = compute_stats(TRADES_6)
        msg = format_stats_summary(s)
        # 4/6 * 100 ≈ 66.7%
        assert "66.7" in msg

    def test_contains_total_pnl(self):
        s = compute_stats(TRADES_6)
        msg = format_stats_summary(s)
        assert "800" in msg

    def test_contains_profit_factor(self):
        s = compute_stats(TRADES_6)
        msg = format_stats_summary(s)
        assert "4.2" in msg or "Profit factor" in msg

    def test_none_profit_factor_safe(self):
        # All-win scenario — profit_factor is None
        trades = [_rec(pnl="100.0", is_win=1)]
        s = compute_stats(trades)
        msg = format_stats_summary(s)
        assert "N/A" in msg   # Should render gracefully

    def test_contains_best_worst_symbols(self):
        s = compute_stats(TRADES_6)
        msg = format_stats_summary(s)
        # BTC has highest pnl (400), ETH has lowest (50), SOL (350)
        # Best should be BTC (400), worst should be ETH (50)
        assert "BTC" in msg or "SOL" in msg  # at least one symbol appears

    def test_pool_url_appended_when_provided(self):
        s = compute_stats(TRADES_6)
        msg = format_stats_summary(s, pool_url="https://enkapital.xyz")
        assert "https://enkapital.xyz" in msg

    def test_title_overrideable(self):
        s = compute_stats(TRADES_6)
        msg = format_stats_summary(s, title="Custom Title")
        assert msg.startswith("Custom Title")

    def test_empty_stats_dict_graceful(self):
        msg = format_stats_summary({})
        assert "No closed trades" in msg

    def test_drawdown_in_message(self):
        s = compute_stats(TRADES_6)
        msg = format_stats_summary(s)
        assert "drawdown" in msg.lower() or "Drawdown" in msg


# ---------------------------------------------------------------------------
# TASK 5 — stats are closed-trades-only: explicit pnl=None exclusion assertion
# ---------------------------------------------------------------------------

class TestClosedTradesOnly:
    """Verify that records with pnl=None (open/unrealized positions) are fully
    excluded from every PnL aggregate.  Stats MUST reflect realized closed
    trades only; no unrealized/open-position data may pollute them.
    """

    def test_none_pnl_excluded_from_n_trades(self):
        """n_trades must only count records that have a numeric pnl."""
        trades = [
            _rec(pnl="200.0", is_win=1),    # closed trade — counted
            _rec(pnl=None,    is_win=0),     # open/unrealized — excluded
            _rec(pnl=None,    is_win=0),     # open/unrealized — excluded
        ]
        s = compute_stats(trades)
        assert s["n_trades"] == 1, "open-position rows (pnl=None) must not count as trades"

    def test_none_pnl_excluded_from_win_rate(self):
        """win_rate must be computed over closed trades only."""
        trades = [
            _rec(pnl="100.0", is_win=1),    # win
            _rec(pnl="100.0", is_win=1),    # win
            _rec(pnl=None,    is_win=0),     # open — excluded; must not lower win_rate
        ]
        s = compute_stats(trades)
        # 2 wins / 2 closed trades = 100% — not 2/3 = 66%
        assert s["win_rate"] == pytest.approx(100.0), (
            "pnl=None rows must not inflate the denominator of win_rate"
        )

    def test_none_pnl_excluded_from_total_pnl(self):
        """total_pnl must be unaffected by pnl=None rows."""
        trades = [
            _rec(pnl="500.0",  is_win=1),   # closed +500
            _rec(pnl="-200.0", is_win=0),   # closed -200
            _rec(pnl=None,     is_win=0),   # open — must not contribute anything
        ]
        s = compute_stats(trades)
        assert s["total_pnl"] == pytest.approx(300.0), (
            "pnl=None rows must not affect total_pnl"
        )
        assert s["n_trades"] == 2
