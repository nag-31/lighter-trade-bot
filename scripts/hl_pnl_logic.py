"""Pure, testable reconstruction logic for the HL PnL reconciler.

This module contains NO I/O, NO database calls, NO network calls — only
deterministic transformations from Trade objects (HL fills) to DB record
dicts.  This makes every function here straightforward to unit-test without
any mocking.

Entry back-out formula
----------------------
HL's ``closedPnl`` equals the per-fill realized profit/loss:

    long:   closedPnl = (exit - entry) * size
    short:  closedPnl = (entry - exit) * size

Solving for entry:

    long:   entry = exit - realized / size
    short:  entry = exit + realized / size

Where:
    exit     = fill.price      (HL fill px = exit price for a realizing fill)
    size     = fill.size       (absolute, always > 0 after the size==0 guard)
    realized = fill.realized_pnl

Realization-kind heuristic
---------------------------
We walk fills oldest-first and maintain a running per-market cumulative
closed-size counter.  When the counter drops to (near) zero after a fill
we tag it "FULL" (this fill closed the position); otherwise "PARTIAL".

Limitation: we only have REALIZING fills here — we can't observe how large
the original open was.  So "FULL" just means "cumulative closed size reset to
~0 after this fill", which is a good proxy but not guaranteed.  Regardless,
the recovered PnL figures are exact because they come directly from HL's
``closedPnl`` field; the kind tag is purely cosmetic (card label).
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Optional

from src.types import Trade

# Tolerance for "position effectively zero" when deciding FULL vs PARTIAL.
# Sizes below this threshold (in coin units) are treated as "closed to zero".
_ZERO_THRESHOLD = Decimal("1e-8")


def _safe_decimal(value) -> Optional[Decimal]:
    """Coerce value to Decimal, returning None on failure."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Realization-kind tracker (stateful; one instance per reconciliation run)
# ---------------------------------------------------------------------------

class RealizationKindTracker:
    """Tags each realizing fill FULL (position flat after it) or PARTIAL.

    Primary rule (exact): HL fills carry ``startPosition`` — the signed
    position size BEFORE the fill. A fill is a FULL close iff it closes the
    whole remainder: ``fill.size >= |startPosition| - ε``. This is exchange
    fact, not a heuristic, and correctly tags EVERY round-trip of a coin
    (the old one-FULL-per-window approximation collapsed a coin's multiple
    trades into one closed + one perpetual in-progress blob).

    Fallback (fills without start_position, e.g. synthetic test fills):
    the legacy first-fill-FULL heuristic, corrected afterwards by
    ``finalize()`` — which the caller must then apply to those markets.
    """

    def __init__(self) -> None:
        # market_symbol -> cumulative closed size seen so far
        self._cumulative: dict[str, Decimal] = {}

    def classify(self, fill: Trade) -> str:
        """Return "FULL" or "PARTIAL" for this fill, updating internal state.

        Exact rule when the fill carries HL's ``startPosition`` (the signed
        position size before the fill): FULL iff this fill closes the whole
        remainder, i.e. ``size >= |startPosition| - ε``. This tags every
        round-trip of a coin correctly (close → reopen → close = two FULLs).

        Fallback for fills WITHOUT start_position: first-fill-FULL heuristic;
        the caller must then run ``finalize()`` over those markets so their
        LAST fill ends up FULL instead.
        """
        sym = fill.market_symbol
        sp = getattr(fill, "start_position", None)
        if sp is not None:
            return (
                "FULL"
                if fill.size >= abs(sp) - _ZERO_THRESHOLD
                else "PARTIAL"
            )

        self._cumulative[sym] = self._cumulative.get(sym, Decimal("0")) + fill.size
        count = self._fill_count.get(sym, 0)
        kind = "PARTIAL" if count > 0 else "FULL"
        self._fill_count[sym] = count + 1
        return kind

    def __init__(self) -> None:  # noqa: F811  re-define to add _fill_count
        self._cumulative: dict[str, Decimal] = {}
        self._fill_count: dict[str, int] = {}

    @staticmethod
    def finalize(records: list[dict]) -> list[dict]:
        """Post-processing pass: set the LAST record per market to 'FULL'.

        This corrects the first-pass classification where the last fill in a
        per-market sequence should be tagged FULL (it closes the position),
        not PARTIAL.  The first-pass always sets fill #0 to FULL and the rest
        to PARTIAL; this pass swaps it so the LAST one per market is FULL.

        Mutates records in-place and returns the same list for convenience.
        """
        # Find index of last occurrence per market
        last_idx: dict[str, int] = {}
        for i, rec in enumerate(records):
            sym = rec.get("market_symbol", "")
            last_idx[sym] = i

        for i, rec in enumerate(records):
            sym = rec.get("market_symbol", "")
            if i == last_idx.get(sym):
                rec["realization_kind"] = "FULL"
            elif rec.get("realization_kind") == "FULL" and i != last_idx.get(sym):
                # Was tagged FULL by first pass but is NOT the last for its market
                rec["realization_kind"] = "PARTIAL"

        return records


# ---------------------------------------------------------------------------
# Core reconstruction function (PURE — no I/O)
# ---------------------------------------------------------------------------

def reconstruct_record(
    fill: Trade,
    kind: str,
    running_wins: int,
    running_total: int,
) -> dict:
    """Derive a closed_trades DB record dict from a single HL realizing fill.

    Parameters
    ----------
    fill:
        A Trade object from ``fetch_realizing_fills``.  Must have
        ``realized_pnl`` set (non-None) and ``size > 0``.
    kind:
        "FULL" or "PARTIAL" — the realization kind tag.
    running_wins:
        Number of wins BEFORE this fill (will be incremented here if win).
    running_total:
        Number of total trades BEFORE this fill (will be incremented here).

    Returns
    -------
    dict
        A dict keyed by ``_CLOSED_TRADE_COLUMNS`` plus:
        - all numeric fields stored as str (same as rest of codebase)
        - ``fill_ids``: JSON string "[trade_id]"
        - ``card_path``: None (caller sets this after PNG generation)

    Raises
    ------
    ValueError
        If fill.size == 0 or fill.realized_pnl is None (callers should
        pre-filter, but this provides a clear error message).
    """
    if fill.size == Decimal("0") or fill.size == 0:
        raise ValueError(
            f"reconstruct_record: fill {fill.trade_id} has size=0; skip this fill"
        )
    if fill.realized_pnl is None:
        raise ValueError(
            f"reconstruct_record: fill {fill.trade_id} has realized_pnl=None; "
            "only realizing fills are supported"
        )

    exit_px: Decimal = fill.price
    size: Decimal = fill.size
    realized: Decimal = fill.realized_pnl

    # Back out entry from HL's closedPnl definition:
    #   long:   closedPnl = (exit - entry) * size  →  entry = exit - realized/size
    #   short:  closedPnl = (entry - exit) * size  →  entry = exit + realized/size
    if fill.side == "long":
        entry_px = exit_px - realized / size
    else:
        entry_px = exit_px + realized / size

    notional = size * entry_px

    # Percentage move (guard entry_px != 0)
    if entry_px != 0:
        if fill.side == "long":
            pct = (exit_px - entry_px) / entry_px * Decimal("100")
        else:
            pct = (entry_px - exit_px) / entry_px * Decimal("100")
    else:
        pct = Decimal("0")

    is_win = realized > 0

    # wins/total are computed by reconstruct_all at TRADE granularity (one
    # increment per FULL close, judged on the round-trip's total PnL) and
    # passed in here verbatim. Per-fill incrementing here once put "149/327"
    # on a card's win-rate bar.
    new_total = running_total
    new_wins = running_wins

    return {
        "ts": fill.timestamp.isoformat(),
        "source": fill.source,
        "market_symbol": fill.market_symbol,
        "side": fill.side,
        "entry": str(entry_px),
        "exit": str(exit_px),
        "size": str(size),
        "notional": str(notional),
        "pnl": str(realized),
        "pct": str(pct),
        "is_win": 1 if is_win else 0,
        "leverage": None,
        "wins": new_wins,
        "total": new_total,
        "card_path": None,
        "trade_id": fill.trade_id,
        "fill_ids": json.dumps([fill.trade_id]),
        "realization_kind": kind,
        "source_id": fill.source_id,
        "exchange": fill.exchange or "hyperliquid",
        "market_key": f"{fill.market_id}:{fill.position_side}",
        "position_side": fill.position_side,
        "native_trade_id": fill.native_trade_id or str(fill.trade_id),
        "event_uid": fill.event_uid(),
        "lifecycle_opened_at": None,
        "holding_duration_ms": None,
        "holding_duration_basis": "unavailable",
    }


# ---------------------------------------------------------------------------
# Batch reconstruction (walk fills oldest-first)
# ---------------------------------------------------------------------------

def reconstruct_all(fills: list[Trade]) -> list[dict]:
    """Reconstruct DB records from a list of realizing fills (oldest-first).

    Steps:
    1. Skip fills with size==0 or realized_pnl is None (logged to stderr).
    2. Use RealizationKindTracker (first-pass: first fill per market = FULL,
       rest = PARTIAL).
    3. Post-process with RealizationKindTracker.finalize() so the LAST fill
       per market is FULL (correct for a full close at end of a run).
    4. Accumulate running_wins / running_total across all fills.

    Returns a list of record dicts, one per valid fill, in oldest-first order.
    """
    tracker = RealizationKindTracker()

    records: list[dict] = []
    running_wins = 0
    running_total = 0
    # Running PnL of each symbol's CURRENT round-trip — a FULL close judges
    # win/loss on this total (a trade can close on a red fill yet be a win
    # overall, e.g. NEAR +193 trip ending in a −41 close fill).
    trip_pnl: dict[str, Decimal] = {}
    # Markets where any fill lacked start_position → their tags came from the
    # legacy heuristic and need the finalize() correction pass. Markets where
    # every fill carried start_position are tagged EXACTLY by classify() —
    # finalize() must NOT touch them (it would force one FULL per window and
    # collapse a coin's multiple round-trips into one).
    fallback_syms: set[str] = set()

    for fill in fills:
        # Guard: skip zero-size or no-pnl fills
        if fill.size == 0 or fill.realized_pnl is None:
            continue

        if getattr(fill, "start_position", None) is None:
            fallback_syms.add(fill.market_symbol)
        kind = tracker.classify(fill)
        sym = fill.market_symbol
        trip_total = trip_pnl.get(sym, Decimal(0)) + fill.realized_pnl
        if kind == "FULL":
            trip_pnl[sym] = Decimal(0)
            running_total += 1
            if trip_total > 0:
                running_wins += 1
        else:
            trip_pnl[sym] = trip_total
        rec = reconstruct_record(fill, kind, running_wins, running_total)
        records.append(rec)

    # Post-processing for heuristic-tagged markets only: last fill → FULL.
    if fallback_syms:
        RealizationKindTracker.finalize(
            [r for r in records if r.get("market_symbol") in fallback_syms]
        )

    return records
