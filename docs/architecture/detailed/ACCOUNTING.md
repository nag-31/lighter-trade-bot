# Accounting model — the rules that must never break

This is the source of truth for "is the PnL right?". Every rule here has a test
or an invariant behind it.

## 1. One row per realization, one card per round-trip

Two layers exist and must not be confused:

- **Record layer (`record_realization`)** writes ONE `closed_trades` row per
  realization: every REDUCE-batch flush is a `PARTIAL` row, and every full close
  is a `FULL` row. Each row keeps **its own fill's** PnL.
- **Display layer (`aggregate_round_trips`)** collapses a coin's `PARTIAL` rows +
  its final `FULL` into ONE grid tile / chart bar / stats entry. It sums the
  rows once — no double counting.

```
Scale-out +$20   → PARTIAL row (pnl=20)
Scale-out +$30   → PARTIAL row (pnl=30)
Final close +$10 → FULL row    (pnl=10)
-----------------------------------------
Tile shows +$60  (aggregate sums 20+30+10)
```

## 2. Round-trip boundaries

- A round-trip **ends** on ANY non-`PARTIAL` row: `FULL`, legacy `None`, unknown.
- Only an explicit `PARTIAL` keeps it open.
- A complete close = position flat = **a distinct trade**. A later reopen of the
  same ticker is a **brand-new round-trip** — never summed with the old one.
- In-progress round-trips (partials with no close yet) render as `OPEN` /
  "IN PROGRESS" tiles and are **excluded from closed-only stats**.

## 3. Long vs short

- Long: `realized = (exit − entry) × size`
- Short: `realized = (entry − exit) × size`
- HL provides `closedPnl` directly — used verbatim (exact).
- Lighter has no per-fill PnL → computed from prices (`_lighter_realized`).

## 4. Unknown PnL is never zero

If any fill in a round-trip has unknown PnL, the whole round-trip's PnL is
`None` (excluded from net PnL, win rate, and equity curve). The merge helper
`_merge_realized_pnl` propagates "unknown" forward. **Never** treat missing as 0.

## 5. The close card shows the whole trade

When a position has scale-outs, the FULL close card displays the **round-trip
total** via `card_pnl_override` (= sum of that round-trip's partials + the
close). The DB row still stores only the close fill's own PnL, and the display
layer sums to the same total. Card, tile, chart bar, and stats therefore agree.

## 6. HL exchange-truth upgrade

For HL closes with recorded scale-outs, the card total is re-summed from the
exchange's own `closedPnl` over the round-trip window
(`fetch_realizing_fills`), falling back to the local sum on API failure. This
repairs fills the WebSocket missed. Missing scale-outs are inserted as `PARTIAL`
rows first, so DB + card stay consistent.

## 7. Idempotency & dedup

- Fill dedup: `seen_tids` (in-memory) + persisted cursors + `event_uid` columns.
- Realization dedup: `_recorded_realizations` set, rebuilt from DB at boot.
  **Claimed synchronously before any await** so the consumer and the reconciler's
  silent-close backstop cannot both record the same fill (this was a race —
  fixed 2026-08-04).
- Notifications: `notification_outbox` with `pending/sent/failed` states; a
  failed row can be re-claimed after 5 minutes; `sent` is never re-sent.

## 8. Silent closes (backstop)

When the position reconciler sees a position disappear from the exchange but no
fill was observed:

- Fetch realizing fills for the market over a **bounded** recent window
  (≥ max(900s, 10×reconciler_interval)) — never unbounded.
- Mark all-but-last as `PARTIAL`, last as `FULL` so they collapse to one trade.
- The FULL card shows the prior partials + this batch.

## 9. Reconciliation sweep (`scripts/reconcile_hl_pnl.py`)

- Dry-run by default; `--apply` backs up, scoped-deletes only rows with
  `ts >= T0`, re-inserts rebuilt rows, regenerates cards.
- Requires `bootstrap_markets()` first (stable `_perp_universe`) so all coins parse.
- Re-running produces the same result (idempotent).
- Lighter rows are never touched.

## 10. Stats window filtering order

**Filter fills by the cutoff FIRST, then aggregate.** If you aggregate first and
filter after, a trade that closed after the cutoff but opened before it drags
old fills into the window (this bled old PnL into the window — fixed 2026-06-04).

## 11. Legacy duplicate rows

Rows with `realization_kind = None` (legacy migration) that exactly re-state a
fill-based sequence (same source/symbol/side + total PnL within 2s) are dropped
at the display layer to avoid double-counting. A legacy row with no fill
counterpart is kept (flagged for manual review).

## 12. Canonical ledger

- `canonical_ledger_entries` is append-only: `trade_realization` +
  `retraction` entries. A repair writes a retraction, never a delete.
- Portfolio membership is independent of ledger history; removing an account
  sets `included=0` without deleting facts.
- Per-account ledgers (`data/accounts/*.db`) hold immutable `exchange_fills` and
  rebuildable `pnl_realizations`.
