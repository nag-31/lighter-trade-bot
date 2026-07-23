"""Trade analytics engine — channel-agnostic.

Pure functions that compute statistics from closed-trades records (newest-first,
as stored in db.py). All numeric fields in the records arrive as strings or None;
parsed defensively via ``_f()``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional


def _f(x) -> Optional[float]:
    """Parse a value to float, returning None on failure or if x is None."""
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


_MIN_DT = datetime.min.replace(tzinfo=timezone.utc)


def _parse_ts(x) -> Optional[datetime]:
    """Parse an ISO-ish timestamp into an aware UTC datetime, or None.

    Accepts full ISO strings (with optional trailing ``Z``) and date-only
    strings (``2026-05-01``). Naive values are assumed UTC.
    """
    if not x:
        return None
    s = str(x).strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    dt: Optional[datetime] = None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.fromisoformat(s[:10])  # date-only fallback
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _norm_symbol(sym) -> str:
    """Canonicalize a ticker for whitelist matching: uppercase, strip a single
    trailing quote/perp suffix. Mirrors sources._normalize_symbol."""
    s = str(sym or "").upper().strip()
    for suffix in ("USDT", "USDC", "USD", "-PERP", "PERP"):
        if s.endswith(suffix) and len(s) > len(suffix):
            return s[: -len(suffix)]
    return s


def filter_trades(
    trades: list[dict],
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    symbols: Optional[Iterable[str]] = None,
    exclude_symbols: Optional[Iterable[str]] = None,
) -> list[dict]:
    """Return the closed-trade records to DISPLAY, filtered and date-sorted.

    Parameters
    ----------
    trades:
        Closed-trade records (each a dict with ``ts`` and ``market_symbol``).
    start_date:
        ISO date/datetime; only trades on/after this are kept. ``None`` ⇒ no
        lower bound.
    end_date:
        ISO date/datetime; only trades on/before this are kept. ``None`` ⇒ no
        upper bound (i.e. up to the current date/time). A date-only value is
        treated as inclusive end-of-day.
    symbols:
        Whitelist of tickers (normalized). Empty/``None`` ⇒ all coins.
    exclude_symbols:
        Blacklist of tickers (normalized) to always drop (e.g. a source's
        ``exclude_symbols`` like FARTCOIN) — applied even if ``symbols`` is set.

    A record whose ``ts`` can't be parsed is dropped when any date bound is set
    (it can't be placed in the window). Result is sorted newest-first by ``ts``.
    """
    start = _parse_ts(start_date)
    end = _parse_ts(end_date)
    # A date-only end bound means "through the end of that day", inclusive.
    if end is not None and end_date is not None and len(str(end_date).strip()) <= 10:
        end = end.replace(hour=23, minute=59, second=59, microsecond=999_999)

    allow = {_norm_symbol(s) for s in symbols} if symbols else None
    block = {_norm_symbol(s) for s in exclude_symbols} if exclude_symbols else None
    has_bound = start is not None or end is not None

    out: list[dict] = []
    for t in trades:
        sym = _norm_symbol(t.get("market_symbol"))
        if allow is not None and sym not in allow:
            continue
        if block is not None and sym in block:
            continue
        dt = _parse_ts(t.get("ts"))
        if has_bound:
            if dt is None:
                continue
            if start is not None and dt < start:
                continue
            if end is not None and dt > end:
                continue
        out.append(t)

    out.sort(key=lambda r: _parse_ts(r.get("ts")) or _MIN_DT, reverse=True)
    return out


# Legacy migration rows (realization_kind NULL, no fill_ids) that fall within
# this many seconds of a real fill-based row for the same (source, symbol) are
# treated as duplicates of that fill sequence and dropped. The known dupes share
# the fill's exact sub-second timestamp; the window is generous head-room and is
# only ever reachable by legacy rows (current code always writes PARTIAL/FULL).
_LEGACY_DEDUP_WINDOW_SEC = 3600.0


def _source_key(row: dict) -> str:
    """Stable account identity with a legacy display-name fallback."""
    return str(row.get("source_id") or row.get("source") or "")


def _is_legacy_kind(r: dict) -> bool:
    """A legacy migration row: realization_kind is NULL/blank (not PARTIAL/FULL)."""
    return not (r.get("realization_kind") or "").strip()


def _has_fill_ids(r: dict) -> bool:
    """True if the row carries fill identifiers (i.e. it is a real fill-based row)."""
    fids = r.get("fill_ids")
    if fids is None:
        return False
    if isinstance(fids, str):
        return fids.strip() not in ("", "[]", "null")
    try:
        return len(fids) > 0
    except TypeError:
        return False


def _drop_legacy_duplicate_rows(trades: list[dict]) -> list[dict]:
    """Remove legacy NULL-kind rows that double-count a fill-based sequence.

    During an earlier migration, 8 aggregate rows were written with
    ``realization_kind=None`` and no ``fill_ids``/``trade_id``. Six of them
    re-state a round-trip that ALSO exists as a PARTIAL/FULL fill sequence for
    the same coin at the same instant — summing both double-counts the PnL
    (e.g. the TON +$42 seen twice). This drops a legacy row when a real
    fill-based row for the same ``(source, market_symbol)`` exists within
    ``_LEGACY_DEDUP_WINDOW_SEC``. A legacy row with NO fill-based counterpart
    (a genuine standalone legacy round-trip, e.g. ENA/SPX) is kept untouched.
    """
    fill_ts: dict[tuple[str, str], list[datetime]] = {}
    for r in trades:
        if not _is_legacy_kind(r):
            key = (_source_key(r), r.get("market_symbol") or "")
            dt = _parse_ts(r.get("ts"))
            if dt is not None:
                fill_ts.setdefault(key, []).append(dt)

    out: list[dict] = []
    for r in trades:
        if _is_legacy_kind(r) and not _has_fill_ids(r):
            key = (_source_key(r), r.get("market_symbol") or "")
            dt = _parse_ts(r.get("ts"))
            near = fill_ts.get(key)
            if dt is not None and near and any(
                abs((dt - t).total_seconds()) <= _LEGACY_DEDUP_WINDOW_SEC
                for t in near
            ):
                continue  # legacy duplicate of a fill sequence → drop
        out.append(r)
    return out


def aggregate_round_trips(trades: list[dict]) -> list[dict]:
    """Collapse per-fill realization rows into one record per round-trip.

    The recorder writes one row per realization (each scale-out + the final
    close). For display we want ONE entry per completed trade. A *round-trip*
    is the run of realizations for a given ``(source, market_symbol)`` that ends
    with a ``FULL`` close. Trailing ``PARTIAL`` rows with no ``FULL`` yet form an
    in-progress (still-open) round-trip.

    Returns aggregated records (REAL values only — no privacy ``*_disp`` fields;
    the caller re-derives those for HL), newest-first by ``ts``. Each record:

    - ``pnl``      = sum of the group's realized pnl (so a closed trade's total
      already equals every scale-out + the final close — no double counting,
      because each source row keeps its own-fill pnl and we sum once here)
    - ``size`` / ``notional`` = sums
    - ``entry``    = size-weighted cost-basis average of the rows' entries
    - ``exit``     = the final fill price in the group
    - ``pct``      = pnl / cost_basis * 100
    - ``ts``       = the final row's ts ; ``card_path`` = the final row's card
    - ``realization_kind`` = ``"FULL"`` (closed) | ``"OPEN"`` (in progress)
    - ``wins`` / ``total`` = running CLOSED-trade record through this trade
    """
    # Drop legacy NULL-kind rows that duplicate a real fill sequence (otherwise
    # their PnL is counted twice — see _drop_legacy_duplicate_rows).
    trades = _drop_legacy_duplicate_rows(trades)

    # Group by (source, market_symbol), preserving deterministic ordering.
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for t in trades:
        key = (
            _source_key(t),
            t.get("market_symbol") or "",
            t.get("position_side") or "BOTH",
        )
        groups.setdefault(key, []).append(t)

    aggregated: list[dict] = []
    for (_source_id, symbol, _position_side), rows in groups.items():
        # Tiebreaker for fills sharing the SAME timestamp (burst close): the
        # close row goes LAST — the position physically flattens at the end of
        # the burst. Without this, an arbitrarily-ordered same-ms burst split
        # one trade into a fake small "closed" + a phantom "in progress" blob.
        rows_sorted = sorted(
            rows,
            key=lambda r: (
                _parse_ts(r.get("ts")) or _MIN_DT,
                0 if (r.get("realization_kind") or "").upper() == "PARTIAL" else 1,
            ),
        )
        segment: list[dict] = []
        for r in rows_sorted:
            segment.append(r)
            # A round-trip ends on any CLOSE row. Only an explicit PARTIAL
            # (scale-out) keeps it open; FULL, legacy None, or any other kind is
            # a complete close. (Treating None as non-terminating wrongly fused
            # closed trades + reopens into one perpetual "in progress" blob and
            # dropped their PnL from stats.)
            if (r.get("realization_kind") or "").upper() != "PARTIAL":
                aggregated.append(_collapse_segment(segment, closed=True))
                segment = []
        if segment:  # trailing PARTIAL(s) with no close → in-progress
            aggregated.append(_collapse_segment(segment, closed=False))

    # Running CLOSED-trade win/total record, assigned in chronological order.
    aggregated.sort(key=lambda r: _parse_ts(r.get("ts")) or _MIN_DT)
    wins = total = 0
    for rec in aggregated:
        if (rec.get("realization_kind") or "").upper() == "FULL":
            total += 1
            if rec.get("is_win"):
                wins += 1
        rec["wins"] = wins
        rec["total"] = total

    aggregated.sort(key=lambda r: _parse_ts(r.get("ts")) or _MIN_DT, reverse=True)
    return aggregated


def _collapse_segment(rows: list[dict], *, closed: bool) -> dict:
    """Build one aggregated record from a round-trip's realization rows."""
    last = rows[-1]
    total_pnl = sum((_f(r.get("pnl")) or 0.0) for r in rows)
    total_size = sum((_f(r.get("size")) or 0.0) for r in rows)
    total_notional = sum((_f(r.get("notional")) or 0.0) for r in rows)

    cost_basis = 0.0
    for r in rows:
        e, s = _f(r.get("entry")), _f(r.get("size"))
        if e is not None and s is not None:
            cost_basis += e * s
    entry = (cost_basis / total_size) if total_size else _f(last.get("entry"))
    exit_ = _f(last.get("exit"))
    pct = (total_pnl / cost_basis * 100.0) if cost_basis else None

    # Carry only safe identity fields from the representative (final) row; the
    # caller re-derives HL *_disp fields from these REAL values.
    return {
        "ts": last.get("ts"),
        "source": last.get("source"),
        "source_id": last.get("source_id"),
        "exchange": last.get("exchange"),
        "market_symbol": last.get("market_symbol"),
        "position_side": last.get("position_side") or "BOTH",
        "side": last.get("side"),
        "entry": entry,
        "exit": exit_,
        "size": total_size,
        "notional": total_notional,
        "pnl": total_pnl,
        "pct": pct,
        "is_win": 1 if total_pnl > 0 else 0,
        "leverage": last.get("leverage"),
        "card_path": last.get("card_path"),
        "trade_id": last.get("trade_id"),
        "realization_kind": "FULL" if closed else "OPEN",
        "n_fills": len(rows),
    }


def compute_stats(trades: list[dict]) -> dict:
    """Compute full trade analytics from a list of closed-trade records.

    Parameters
    ----------
    trades:
        Newest-first list of dicts with keys from db.py ``closed_trades`` table.
        Numeric fields are strings or None; ``is_win`` is int (0/1).

    Returns
    -------
    dict with the canonical analytics payload shape (JSON-serializable).
    """
    # Filter to records with a usable pnl — these count toward all pnl aggregates.
    # Only realized/closed-trade rows have a non-None pnl; open/unrealized positions
    # are NEVER passed into this function (callers use load_closed_trades from db.py).
    # This explicit guard ensures that any record whose pnl is None (e.g. a partially
    # recorded row or future open-position data) is excluded from ALL numeric aggregates
    # (n_trades, win_rate, total_pnl, etc.).  The equity_curve and pnl_series are
    # therefore strictly closed-trades-only.
    pnl_records = []
    for t in trades:
        v = _f(t.get("pnl"))
        if v is not None:
            pnl_records.append((t, v))

    n_trades = len(pnl_records)

    if n_trades == 0:
        return {
            "n_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "profit_factor": None,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "avg_pnl": 0.0,
            "largest_win": 0.0,
            "largest_loss": 0.0,
            "max_drawdown": 0.0,
            "by_symbol": [],
            "by_source": [],
            "long":  {"n": 0, "pnl": 0.0, "win_rate": 0.0},
            "short": {"n": 0, "pnl": 0.0, "win_rate": 0.0},
            "equity_curve": [],
            "pnl_series": [],
        }

    # Basic aggregates
    wins = sum(1 for _, v in pnl_records if v > 0)
    losses = sum(1 for _, v in pnl_records if v <= 0)
    win_rate = wins / n_trades * 100

    total_pnl = sum(v for _, v in pnl_records)
    gross_profit = sum(v for _, v in pnl_records if v > 0)
    gross_loss = sum(abs(v) for _, v in pnl_records if v < 0)

    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None

    win_pnls = [v for _, v in pnl_records if v > 0]
    loss_pnls = [v for _, v in pnl_records if v < 0]

    avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0.0
    avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0.0
    avg_pnl = total_pnl / n_trades

    largest_win = max(win_pnls) if win_pnls else 0.0
    largest_loss = min(loss_pnls) if loss_pnls else 0.0

    # Equity curve + max drawdown — oldest first
    reversed_records = list(reversed(pnl_records))

    cum_pnl = 0.0
    peak = 0.0
    max_dd = 0.0
    equity_curve: list[dict] = []
    pnl_series: list[dict] = []

    for i, (rec, v) in enumerate(reversed_records):
        cum_pnl += v
        ts = rec.get("ts") or ""
        equity_curve.append({"i": i, "ts": ts, "cum_pnl": round(cum_pnl, 8)})

        # Track peak and drawdown
        if cum_pnl > peak:
            peak = cum_pnl
        dd = cum_pnl - peak
        if dd < max_dd:
            max_dd = dd

        is_win_bool = v > 0
        pnl_series.append({
            "i": i,
            "ts": ts,
            "pnl": round(v, 8),
            "symbol": rec.get("market_symbol") or "",
            "side": rec.get("side") or "",
            "is_win": is_win_bool,
        })

    max_drawdown = round(max_dd, 8)

    # By symbol
    sym_map: dict[str, dict] = {}
    for rec, v in pnl_records:
        sym = rec.get("market_symbol") or "?"
        if sym not in sym_map:
            sym_map[sym] = {"n": 0, "pnl": 0.0, "wins": 0}
        sym_map[sym]["n"] += 1
        sym_map[sym]["pnl"] += v
        if v > 0:
            sym_map[sym]["wins"] += 1

    by_symbol = sorted(
        [
            {
                "symbol": sym,
                "n": d["n"],
                "pnl": round(d["pnl"], 8),
                "win_rate": d["wins"] / d["n"] * 100 if d["n"] > 0 else 0.0,
            }
            for sym, d in sym_map.items()
        ],
        key=lambda x: x["pnl"],
        reverse=True,
    )

    # By source
    src_map: dict[str, dict] = {}
    for rec, v in pnl_records:
        source_id = _source_key(rec) or "?"
        if source_id not in src_map:
            src_map[source_id] = {
                "source": rec.get("source") or source_id,
                "n": 0,
                "pnl": 0.0,
            }
        src_map[source_id]["n"] += 1
        src_map[source_id]["pnl"] += v

    by_source = sorted(
        [
            {
                "source": d["source"],
                "source_id": source_id,
                "n": d["n"],
                "pnl": round(d["pnl"], 8),
            }
            for source_id, d in src_map.items()
        ],
        key=lambda x: x["pnl"],
        reverse=True,
    )

    # Long / short split
    long_records  = [(rec, v) for rec, v in pnl_records if (rec.get("side") or "").lower() == "long"]
    short_records = [(rec, v) for rec, v in pnl_records if (rec.get("side") or "").lower() == "short"]

    def _side_stats(recs: list) -> dict:
        if not recs:
            return {"n": 0, "pnl": 0.0, "win_rate": 0.0}
        n = len(recs)
        pnl = sum(v for _, v in recs)
        w = sum(1 for _, v in recs if v > 0)
        return {"n": n, "pnl": round(pnl, 8), "win_rate": w / n * 100}

    return {
        "n_trades": n_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 4),
        "total_pnl": round(total_pnl, 8),
        "gross_profit": round(gross_profit, 8),
        "gross_loss": round(gross_loss, 8),
        "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
        "avg_win": round(avg_win, 8),
        "avg_loss": round(avg_loss, 8),
        "avg_pnl": round(avg_pnl, 8),
        "largest_win": round(largest_win, 8),
        "largest_loss": round(largest_loss, 8),
        "max_drawdown": max_drawdown,
        "by_symbol": by_symbol,
        "by_source": by_source,
        "long": _side_stats(long_records),
        "short": _side_stats(short_records),
        "equity_curve": equity_curve,
        "pnl_series": pnl_series,
    }


def format_stats_summary(
    stats: dict,
    title: str = "\U0001f4ca Trade stats",
    pool_url: str = "",
) -> str:
    """Format a compact Telegram-ready stats summary string.

    Returns a friendly "No closed trades yet." message when there are no trades.
    """
    if not stats or stats.get("n_trades", 0) == 0:
        lines = [title, "No closed trades yet."]
        if pool_url:
            lines.append(pool_url)
        return "\n".join(lines)

    def _pnl_str(v: float) -> str:
        if v is None:
            return "—"
        sign = "+" if v >= 0 else "−"
        return f"{sign}${abs(v):,.2f}"

    n = stats["n_trades"]
    wr = stats["win_rate"]
    total_pnl = stats["total_pnl"]
    pf = stats.get("profit_factor")
    avg_win = stats.get("avg_win", 0.0)
    avg_loss = stats.get("avg_loss", 0.0)
    md = stats.get("max_drawdown", 0.0)

    pf_str = f"{pf:.2f}" if pf is not None else "N/A"

    by_sym = stats.get("by_symbol", [])
    best_str = ""
    worst_str = ""
    if by_sym:
        best = by_sym[0]
        worst = by_sym[-1]
        best_str = f"{best['symbol']} {_pnl_str(best['pnl'])}"
        worst_str = f"{worst['symbol']} {_pnl_str(worst['pnl'])}"

    lines = [
        title,
        f"Trades: {n}  ·  Win rate: {wr:.1f}%",
        f"Net P&L: {_pnl_str(total_pnl)}",
        f"Profit factor: {pf_str}  |  Avg win: {_pnl_str(avg_win)}  |  Avg loss: {_pnl_str(avg_loss)}",
    ]
    if best_str or worst_str:
        lines.append(f"Best: {best_str}  |  Worst: {worst_str}")
    lines.append(f"Max drawdown: {_pnl_str(md)}")
    if pool_url:
        lines.append(pool_url)

    return "\n".join(lines)
