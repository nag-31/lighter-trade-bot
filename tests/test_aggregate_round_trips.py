"""Tests for src.stats.aggregate_round_trips — collapsing per-fill realization
rows into one record per round-trip (a coin's scale-outs + its final close)."""

from __future__ import annotations

from src.stats import aggregate_round_trips, compute_stats


def _row(ts, sym, pnl, kind, *, source="HL", side="long",
         entry="100", exit="110", size="1", notional="110", card=None):
    return {
        "ts": ts, "source": source, "market_symbol": sym, "side": side,
        "entry": entry, "exit": exit, "size": size, "notional": notional,
        "pnl": str(pnl), "realization_kind": kind,
        "card_path": card or f"/cards/{sym}_{kind}_{ts[-8:]}.png",
    }


def _by_sym(agg):
    return {a["market_symbol"]: a for a in agg}


def test_single_full_close_is_unchanged():
    agg = aggregate_round_trips([_row("2026-06-01T10:00:00Z", "BTC", 100, "FULL")])
    assert len(agg) == 1
    assert agg[0]["pnl"] == 100.0
    assert agg[0]["realization_kind"] == "FULL"
    assert agg[0]["n_fills"] == 1


def test_partials_plus_close_sum_into_one():
    rows = [
        _row("2026-06-01T09:00:00Z", "ETH", 50, "PARTIAL", entry="2000", exit="2050", size="1"),
        _row("2026-06-01T09:30:00Z", "ETH", 100, "PARTIAL", entry="2000", exit="2100", size="1"),
        _row("2026-06-01T10:00:00Z", "ETH", 200, "FULL", entry="2000", exit="2200", size="1", card="/cards/eth_close.png"),
    ]
    agg = aggregate_round_trips(rows)
    assert len(agg) == 1
    a = agg[0]
    assert a["pnl"] == 350.0            # 50 + 100 + 200
    assert a["size"] == 3.0             # summed
    assert a["realization_kind"] == "FULL"
    assert a["card_path"] == "/cards/eth_close.png"  # the FINAL close's card
    assert a["n_fills"] == 3
    assert abs(a["entry"] - 2000.0) < 1e-9
    assert a["exit"] == 2200.0


def test_trailing_partials_are_in_progress():
    rows = [
        _row("2026-06-01T09:00:00Z", "ARB", 10, "PARTIAL"),
        _row("2026-06-01T09:30:00Z", "ARB", 5, "PARTIAL"),
    ]
    agg = aggregate_round_trips(rows)
    assert len(agg) == 1
    assert agg[0]["realization_kind"] == "OPEN"
    assert agg[0]["pnl"] == 15.0


def test_open_excluded_from_closed_only_stats():
    rows = [
        _row("2026-06-01T10:00:00Z", "BTC", 100, "FULL"),
        _row("2026-06-02T10:00:00Z", "ARB", 25, "PARTIAL"),  # still open
    ]
    agg = aggregate_round_trips(rows)
    closed = [a for a in agg if a["realization_kind"] == "FULL"]
    s = compute_stats(closed)
    assert s["n_trades"] == 1
    assert s["total_pnl"] == 100.0     # the open ARB +25 is NOT counted


def test_flip_same_coin_two_round_trips():
    # Long round-trip closes, then a short round-trip on the same coin.
    rows = [
        _row("2026-06-01T08:00:00Z", "SOL", 30, "PARTIAL", side="long"),
        _row("2026-06-01T09:00:00Z", "SOL", 70, "FULL", side="long"),
        _row("2026-06-01T11:00:00Z", "SOL", -20, "PARTIAL", side="short"),
        _row("2026-06-01T12:00:00Z", "SOL", 50, "FULL", side="short"),
    ]
    agg = aggregate_round_trips(rows)
    assert len(agg) == 2
    # newest-first
    assert agg[0]["side"] == "short" and agg[0]["pnl"] == 30.0   # -20 + 50
    assert agg[1]["side"] == "long" and agg[1]["pnl"] == 100.0   # 30 + 70


def test_multi_coin_independent_grouping():
    rows = [
        _row("2026-06-01T08:00:00Z", "ETH", 10, "PARTIAL"),
        _row("2026-06-01T08:05:00Z", "BTC", 5, "FULL"),
        _row("2026-06-01T08:10:00Z", "ETH", 40, "FULL"),
    ]
    agg = _by_sym(aggregate_round_trips(rows))
    assert agg["ETH"]["pnl"] == 50.0 and agg["ETH"]["n_fills"] == 2
    assert agg["BTC"]["pnl"] == 5.0 and agg["BTC"]["n_fills"] == 1


def test_unsorted_input_is_segmented_by_time():
    # Same round-trip delivered out of order; must still group correctly.
    rows = [
        _row("2026-06-01T10:00:00Z", "ETH", 200, "FULL", entry="2000", exit="2200"),
        _row("2026-06-01T09:00:00Z", "ETH", 50, "PARTIAL", entry="2000", exit="2050"),
    ]
    agg = aggregate_round_trips(rows)
    assert len(agg) == 1
    assert agg[0]["pnl"] == 250.0


def test_none_kind_rows_are_closed_not_in_progress():
    # Legacy rows with realization_kind=None must count as complete closes,
    # not get fused into a perpetual "in progress" blob.
    rows = [
        _row("2026-06-01T10:00:00Z", "DOGE", 40, None),
        _row("2026-06-02T10:00:00Z", "DOGE", 60, None),
    ]
    agg = aggregate_round_trips(rows)
    assert len(agg) == 2
    assert all(a["realization_kind"] == "FULL" for a in agg)
    s = compute_stats([a for a in agg if a["realization_kind"] == "FULL"])
    assert s["n_trades"] == 2 and s["total_pnl"] == 100.0


def test_close_then_reopen_separates_closed_from_in_progress():
    # LIT closed for profit, then reopened and scaled out (still open).
    rows = [
        _row("2026-06-01T10:00:00Z", "LIT", 200, "FULL", card="/cards/lit_close.png"),
        _row("2026-06-03T09:00:00Z", "LIT", 30, "PARTIAL", card="/cards/lit_reopen.png"),
    ]
    agg = sorted(aggregate_round_trips(rows), key=lambda a: a["ts"])
    assert len(agg) == 2
    assert agg[0]["realization_kind"] == "FULL" and agg[0]["pnl"] == 200.0
    assert agg[1]["realization_kind"] == "OPEN" and agg[1]["pnl"] == 30.0
    # The previous close is NOT marked in-progress; only the reopen is.


def test_running_win_total_counts_closed_only():
    rows = [
        _row("2026-06-01T10:00:00Z", "BTC", 100, "FULL"),   # win  -> 1/1
        _row("2026-06-02T10:00:00Z", "ETH", -50, "FULL"),   # loss -> 1/2
        _row("2026-06-03T10:00:00Z", "SOL", 20, "FULL"),    # win  -> 2/3
    ]
    agg = sorted(aggregate_round_trips(rows), key=lambda a: a["ts"])
    assert [(a["wins"], a["total"]) for a in agg] == [(1, 1), (1, 2), (2, 3)]
