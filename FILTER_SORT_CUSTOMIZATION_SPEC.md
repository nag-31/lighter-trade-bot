# Portfolio Filter / Sort / Customization Spec ("DeBank-grade overview")

Spec for upgrading the portfolio webapp's filtering, sorting, and view
customization to match — and where cheap, exceed — the DeBank / Rabby
portfolio-overview experience. Grounded in live reverse-engineering of
debank.com against the user's real wallet (see "Research basis" at the bottom).

Builds ON TOP of the shipped UI revamp (`UI_REVAMP_BRIEF.md`,
`src/portfolio_static/`). Nothing here changes the fetcher or the snapshot
payload; unless a section says "backend", everything is client-side over the
existing `/api/summary` data.

---

## 0. Design stance (read first)

1. **DeBank's power is NOT filters — it's structure.** DeBank ships almost no
   user controls (no sortable headers, no search, no threshold setting). What
   makes it feel powerful: chain chips with $ and %, schema-per-position-type
   tables with badges, wallet-vs-protocol split, and smart dust folding. We
   copy that structure first.
2. **Then we exceed parity** with the controls DeBank/Rabby famously LACK and
   users request: sortable columns everywhere, token search, a user-set dust
   threshold, portfolio-% and 24h-% columns, column show/hide, saved views.
3. **Filter state is layered**: transient scoping lives in the **URL** (like
   DeBank's `?chain=arb` — shareable, resets cleanly), while preferences live
   in **localStorage** (dust mode, hidden tokens, column config, saved views).
   Server stays stateless about views.

---

## 1. The View Scope model (single source of truth)

One client-side `viewState` object drives EVERY section (KPI hero, donut,
token tables, venue sections, global holdings). No section may filter
privately; all read the same scope.

```js
viewState = {
  // URL-persisted (query params) — transient scope
  chain:   null | "ethereum" | ... | "lighter" | "hyperliquid",  // ?chain=
  address: null | <id>,                                          // ?address=
  q:       "",                                                   // ?q= search
  type:    null | "spot"|"staked"|"perp"|"pool"|"pending",       // ?type=
  // localStorage-persisted — preferences
  dustMode:        "relative" | "absolute" | "off",   // default "relative"
  dustAbsoluteUsd: 1.00,                              // used when "absolute"
  hiddenTokens:    ["<chainKey>:<contract-or-symbol>", ...],  // manual blocklist
  columns:         { tokens: {price:true, amount:true, value:true, pct24h:true, portfolioPct:true, chain:true} },
  sort:            { tokens: {key:"value", dir:"desc"}, holdings: {...}, positions: {...}, addresses: {...} },
  savedViews:      [ {name, urlParams, prefsSubset}, ... ],
}
```

Rules:
- URL params are read on load, written via `history.replaceState` on change
  (no page reload). Copying the URL reproduces the exact scoped view.
- Plain reload with no params = "All" view (DeBank behavior).
- Clearing filters resets URL params only, never preferences.

## 2. Chain / venue chip bar (replaces current chain+venue chips)

DeBank's signature control, upgraded:

- One horizontal bar under the KPI hero: `All` chip + one chip per chain
  with a nonzero balance + `Lighter` + `Hyperliquid` venue chips.
- **Each chip shows: icon/dot + name + USD subtotal + % of included total.**
  Venue chips (Lighter/Hyperliquid) show $ but no % IF we roll their value
  into a parent-chain % is NOT applicable to us (we treat venues as their own
  buckets) — so: venue chips DO show %, computed against the same included
  total. (Deviation from DeBank, deliberate: our venues aren't chain-nested.)
- Chains beyond the top 8 by value fold behind an **"Unfold N chains"** chip;
  unfolded list includes zero-balance chains greyed at $0 0%.
- **Single-select** (click chip → scope to it; click again or click `All` →
  clear). Writes `?chain=`. Scopes ALL sections: token tables, donut, global
  holdings, venue sections (e.g. `?chain=lighter` hides HL + all EVM chains).
- Multi-select is explicitly OUT (DeBank/Rabby are single-select; multi adds
  UI complexity with near-zero real use — the sum of two chains is what the
  All view already shows).

## 3. Dust folding (the DeBank "relative threshold" trick)

Replace the current fixed `$1.00` toggle with three modes (Settings drawer +
quick toggle in filter bar):

- **`relative` (default, DeBank-style):** hide token rows worth less than
  `0.1% of the currently visible total` — recomputed whenever the scope
  changes (chain-filtered view of $58k → threshold $58; all-view of $1.09M →
  threshold $1,090... capped: `min(max(total*0.001, $1), $500)` so huge
  portfolios don't hide real money and small ones don't hide everything).
- **`absolute`:** user-set $ threshold (existing behavior; input in settings).
- **`off`:** show everything.
- Folded rows are NOT deleted: footer row per table —
  *"N tokens with small balances are not displayed. Show all"* ↔
  *"Hide tokens with small balances."* (binary reveal, transient, resets on
  reload). Dust stays inside totals and folds into the donut "Other" slice.
- Same pattern at the **venue/section level**: a venue section worth less
  than the threshold collapses to a one-line header with "Show".

## 4. Manual hide-token blocklist (Rabby-style)

- Every token row gets a hover "eye-off" action → adds
  `chainKey:contract` (or `venue:symbol` for venue assets) to
  `hiddenTokens` in localStorage.
- Hidden tokens are removed from tables AND **subtracted from displayed
  totals** (Rabby semantics — this is the anti-scam/junk escape hatch), with
  a persistent, visible counter chip in the filter bar: `Hidden: 3` →
  clicking opens a manage-list popover with per-token "unhide" and
  "unhide all". Never silently hide.
- We have no scam feed (local app, no risk DB) — manual blocklist covers it.

## 5. Sortable columns + search (exceeds DeBank/Rabby)

- **Every table gets clickable sort headers** with tri-state cycling
  (desc → asc → default) and ▲/▼ indicator: token tables (Price, Amount,
  Value, 24h%, Portfolio%), global holdings (Value, Balance, Addresses,
  Chains), positions tables (Value, PnL, Leverage, Size), address table
  (already shipped). Sort prefs persist per-table in localStorage.
- Default sort everywhere: **USD value descending** (DeBank's fixed order).
- **Token search** (the existing header search) additionally narrows scoped
  token tables and writes `?q=`; searching by chain name scopes like a chain
  chip would.

## 6. Position-type badges + schema-per-type tables (DeBank's real moat)

Adopt DeBank's controlled vocabulary as colored badges, mapped to our data:

| Badge | Source in our payload | Table schema |
|---|---|---|
| `Wallet` | `chains[].native` + `chains[].tokens[]` | Token, Price, Amount, 24h%, Value, Port.% |
| `Staked` | `lighter.staking` (pool-share staked LIT) | Asset, Amount, Value (+ staking PnL line) |
| `Deposit` | `lighter.accounts[].assets`, `hyperliquid.spot.balances` | Asset, Pool label (Spot / Perps Available), Balance, Value |
| `Perpetuals` | `lighter.accounts[].positions`, `hyperliquid.perp.positions` | Pair, Side, Leverage, Margin, Entry, PnL, Liq., Value — sub-account badge per row (DeBank shows `Main-Account`; we show Lighter sub-account name/index) |
| `Pool` | `lighter.pool_deposits` | Pool, Principal, Shares, Value |
| `Pending` | `lighter.staking.pending_unlocks` | Amount, Unlocks at |

- `?type=` scopes to one badge type across all addresses/venues (e.g.
  `?type=perp` = every open perp across Lighter + HL in one view — something
  DeBank can't do across protocols).
- PnL cells colored, leverage formatted `3.00x`, liq price shown when present
  (danger-first fields, per DeBank).

## 7. Section-jump nav ("$ per section" summary)

Sticky compact strip under the chip bar (or inside the hero card):
`Wallet $X · Lighter $Y · Hyperliquid $Z · Staked $W` — each item is both an
allocation read-out and an anchor that scrolls to that section. Values respect
the current scope. Pure client-side.

## 8. Column customization (exceeds parity)

- Small "columns" gear on each major table → popover with checkboxes per
  optional column (24h%, Portfolio %, Price, Chain badge, Liq. price, ROE…).
  Value column is always on. Persists in `viewState.columns`.
- Portfolio-% column = row value / included total (recomputed per scope).

## 9. Saved views (exceeds parity — cheap once 1–8 exist)

- "Save view" in the overflow menu snapshots current URL params + dust mode +
  sort prefs under a user-given name (e.g. "Perps only", "ETH L1 blue chips").
- Saved views render as pills in the filter bar; click = apply, right-click/
  long-press = rename/delete. Stored in localStorage (max ~20).

## 10. Trust & state affordances

- **Staleness chip** (DeBank: "Data updated 12 mins ago"): already have
  relative last-refresh in the hero — additionally tint it amber when
  > 1h and red when > 24h, with a refresh CTA.
- **Filtered-empty state**: when scope+filters hide everything in a section:
  *"No matches in this view — Clear filters"* inline (never a blank table).
- **Scoped totals labeling**: when any scope is active, the hero total gets a
  small `filtered` pill and shows `scoped $X of $TOTAL` so a filtered view is
  never mistaken for net worth.

## 11. Explicitly OUT of scope

- NFTs, decoded transaction history, arbitrary-protocol adapters (DeBank's
  backend moat), automatic scam feeds, currency switcher (USD only),
  multi-select chain filter, DeBank "Time Machine" (our snapshot history
  chart already covers the useful subset), bundles (multi-address is native
  to our app already).

## 12. Backend changes required

**None mandatory.** All of the above is computable from the existing
`/api/summary` payload client-side.

Optional (nice-to-have, small):
- `GET /api/summary?chain=<key>` server-side pre-filter — NOT needed for
  correctness; skip unless payloads become huge.
- Persist `hiddenTokens` server-side (new column/table + PATCH) if the user
  ever wants blocklist sync across browsers — localStorage is fine today.

## 13. Implementation phases

- **P0 (core parity):** viewState + URL params; chain/venue chip bar with $ + %
  and fold; scope-everything wiring; relative dust mode + footer reveal;
  section-jump nav; filtered-empty states; scoped-total labeling.
- **P1 (exceed parity):** sortable headers everywhere + persisted sorts;
  position-type badges + `?type=` scope + per-type schemas (perp table
  upgrade with sub-account badges); manual hide-token blocklist + manager.
- **P2 (customization):** column show/hide gear; portfolio-% + 24h% columns;
  saved views; staleness tinting.

Each phase must ship with: `node --check` clean, no regression in the
existing pytest suite (frontend-only), and a manual browser pass (headless
Chrome CDP works in this environment) covering: chip scoping affects ALL
sections, URL round-trip reproduces the view, dust modes, hidden-token
subtraction from totals, sort persistence.

## 14. Acceptance criteria

1. Clicking a chain chip scopes every section and the URL; sharing the URL
   reproduces the view; `All` restores.
2. Chips show correct $ and % that sum to ~100% for included addresses.
3. Relative dust mode hides < 0.1%-of-visible-total rows, footer reveals
   them, thresholds recompute on scope change.
4. Every major table sorts by any visible column with persisted preference.
5. `?type=perp` shows all Lighter + HL perps in one view with Side, Leverage,
   Margin, Entry, PnL, Liq., Value and sub-account badges.
6. Hidden tokens are excluded from totals with a visible counter and full
   undo path.
7. No backend/API change required; degraded payloads never break any view.

---

## Research basis (2026-07-07)

Live headless-Chrome inspection of debank.com against the user's real wallet
plus a high-variety reference profile; Rabby from official docs/GitHub.
Key verified facts encoded above: chain chips carry $ + % with venue values
rolled into parent chains; `?chain=` URL param is DeBank's only filter
persistence; the dust fold threshold is RELATIVE (~0.1% of visible wallet
total, recomputed per active filter) with a binary Show-all footer; DeBank
profile token tables have NO sortable headers, NO search, NO user threshold —
fixed USD-desc; protocol sections use schema-per-position-type sub-tables
with badges (Staked/Deposit/Yield/LP/Lending/Rewards/Perpetuals) and
danger-first fields (Health Rate, PnL, leverage); Rabby ships auto spam
filtering + manual per-token block (subtracts from total) + small-asset
folding, and no sort controls either.
