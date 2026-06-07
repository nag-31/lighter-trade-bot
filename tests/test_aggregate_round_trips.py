"""Tests for src.stats.aggregate_round_trips — collapsing per-fill realization
rows into one record per round-trip (a coin's scale-outs + its final close)."""

from __future__ import annotations

from src.stats import aggregate_round_trips, compute_stats, filter_trades


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


def test_filter_first_disregards_precutoff_fills():
    # A NEAR position scaled out for a big loss in May, then closed in June.
    rows = [
        _row("2026-05-20T10:00:00Z", "NEAR", -2000, "PARTIAL"),
        _row("2026-06-03T10:00:00Z", "NEAR", 500, "FULL"),
    ]
    # CORRECT: filter fills first, THEN aggregate — May loss is gone.
    agg = aggregate_round_trips(filter_trades(rows, start_date="2026-06-01"))
    assert len(agg) == 1
    assert agg[0]["pnl"] == 500.0

    # WRONG (the old order): aggregate first, filter after — May bleeds into June.
    bled = filter_trades(aggregate_round_trips(rows), start_date="2026-06-01")
    assert bled[0]["pnl"] == -1500.0  # -2000 + 500, mis-attributed to June


def test_reopened_ticker_never_summed_with_prior_close():
    # NEAR closed +600, reopened and closed -130, reopened and closed -120.
    rows = [
        _row("2026-06-01T10:00:00Z", "NEAR", 600, "FULL"),
        _row("2026-06-02T10:00:00Z", "NEAR", -130, "FULL"),
        _row("2026-06-03T10:00:00Z", "NEAR", -120, "FULL"),
    ]
    agg = sorted(aggregate_round_trips(rows), key=lambda a: a["ts"])
    assert [a["pnl"] for a in agg] == [600.0, -130.0, -120.0]  # 3 distinct trades
    assert len(agg) == 3


def test_legacy_null_row_duplicating_fill_sequence_is_dropped():
    # The TON +$42 case: a legacy NULL-kind aggregate row restates a round-trip
    # that ALSO exists as a real PARTIAL/FULL fill sequence at the same instant.
    # The legacy row must be dropped so +$42 is counted once, not twice.
    rows = [
        _row("2026-06-03T19:36:39Z", "TON", 42, None),        # legacy duplicate
        _row("2026-06-03T19:36:39Z", "TON", 20, "PARTIAL"),   # real fills...
        _row("2026-06-03T19:36:39Z", "TON", 22, "FULL"),
    ]
    agg = aggregate_round_trips(rows)
    assert len(agg) == 1
    assert agg[0]["pnl"] == 42.0          # 20 + 22, NOT 84
    assert agg[0]["realization_kind"] == "FULL"


def test_legacy_null_row_without_fill_counterpart_is_kept():
    # ENA/SPX case: a genuine standalone legacy round-trip with no fill-based
    # counterpart must survive — it is real PnL, not a duplicate.
    rows = [_row("2026-06-03T19:36:45Z", "ENA", 165.01, None)]
    agg = aggregate_round_trips(rows)
    assert len(agg) == 1
    assert abs(agg[0]["pnl"] - 165.01) < 1e-9
    assert agg[0]["realization_kind"] == "FULL"


def test_legacy_null_row_outside_window_is_kept():
    # A legacy row hours away from an unrelated later fill-based trade on the
    # same coin is NOT a duplicate — both are distinct round-trips.
    rows = [
        _row("2026-06-01T10:00:00Z", "WLD", 100, None),    # legacy, hours earlier
        _row("2026-06-03T19:36:00Z", "WLD", 50, "FULL"),   # unrelated later trade
    ]
    agg = sorted(aggregate_round_trips(rows), key=lambda a: a["ts"])
    assert len(agg) == 2
    assert agg[0]["pnl"] == 100.0
    assert agg[1]["pnl"] == 50.0


def test_running_win_total_counts_closed_only():
    rows = [
        _row("2026-06-01T10:00:00Z", "BTC", 100, "FULL"),   # win  -> 1/1
        _row("2026-06-02T10:00:00Z", "ETH", -50, "FULL"),   # loss -> 1/2
        _row("2026-06-03T10:00:00Z", "SOL", 20, "FULL"),    # win  -> 2/3
    ]
    agg = sorted(aggregate_round_trips(rows), key=lambda a: a["ts"])
    assert [(a["wins"], a["total"]) for a in agg] == [(1, 1), (1, 2), (2, 3)]
