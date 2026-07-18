# Portfolio UI Revamp — Build Brief (v1)

Orchestrator-authored brief. Two agents build in parallel from this contract:
- **Backend agent**: owns `src/portfolio_app.py`, `src/portfolio_db.py`, `tests/`.
- **Frontend agent**: owns `src/portfolio_static/` (new directory) only. May READ any src file.

Neither agent edits the other's files. This document is the interface contract — follow it exactly.

## Architecture decisions (final)

1. Kill the embedded `INDEX_HTML` string. Frontend lives in `src/portfolio_static/`:
   - `index.html`, `app.js`, `style.css`, `vendor/uPlot.iife.min.js`, `vendor/uPlot.min.css` (vendored, no CDN at runtime).
2. aiohttp serves `src/portfolio_static/` at `/static/`; `GET /` reads `portfolio_static/index.html` from disk at request time (so it hot-reloads on edit) with `Cache-Control: no-cache, must-revalidate`. If the file is missing, return a plain 503 "frontend not built" page (do NOT crash).
3. Vanilla JS, no build step, no framework. uPlot for the history line chart only; donut is hand-rolled SVG.
4. Exclusion moves server-side: new `excluded` column on `portfolio_addresses` (localStorage no longer used for it).
5. Refresh-all becomes a background job with a polling status endpoint (progress UI).
6. CSV/JSON export is client-side (Blob download) — no backend export endpoint.
7. Per-source retry buttons are OUT of scope (per-address refresh is the granularity).
8. All new-UI rendering must defensively handle the degraded/error payload shape (missing `perp`/`spot`/`staking`/`accounts` keys etc.).

## API contract (backend implements, frontend consumes — EXACTLY this)

### GET /api/summary  (existing — extended)
Same shape as today, with these additions:
- each `addresses[]` entry gains `"excluded": bool`
- top level gains `"totals_included"`: same shape as `totals` but summing only non-excluded addresses
- `totals` stays the all-addresses sum (back-compat)

### GET /api/addresses/{id}/history?limit=300
Uses existing `portfolio_db.snapshot_history`. Response (ascending by ts, no payloads):
```json
{"ok": true, "history": [{"id": 1, "ts": "ISO", "status": "ok", "total_usd": 123.4}]}
```

### GET /api/history?limit=1000
Aggregate portfolio history across enabled, non-excluded addresses. Algorithm (new DB helper `aggregate_history(path, limit)` in portfolio_db.py):
- fetch (address_id, ts, total_usd) for all snapshots of enabled non-excluded addresses, ordered by ts asc
- walk events carrying forward each address's last-known total; emit one point per event ts with the sum of last-knowns
- return the last `limit` points
```json
{"ok": true, "history": [{"ts": "ISO", "total_usd": 123.4}]}
```

### PATCH /api/addresses/{id}
Body: `{"label": "optional str", "excluded": optional bool}` — either or both.
Response: `{"ok": true, "address": {id, address, label, enabled, excluded, created_at, updated_at}}`
404 if unknown/disabled id.

### POST /api/refresh  (changed: now async job)
Starts a background refresh of all enabled addresses. Returns immediately:
- `{"ok": true, "started": true}` (HTTP 200)
- if already running: `{"ok": false, "error": "refresh already running"}` (HTTP 409)

### GET /api/refresh/status
```json
{
  "ok": true,
  "running": false,
  "total": 7, "completed": 7,
  "current_address_id": null,
  "started_at": "ISO|null", "finished_at": "ISO|null",
  "results": [{"address_id": 1, "status": "ok"}]
}
```
In-memory job state guarded by the existing refresh lock. `results` accumulates as addresses finish.

### POST /api/addresses/{id}/refresh  (unchanged)
Stays blocking, returns `{"ok": true, "snapshot": <full payload>}`.

### POST /api/addresses, DELETE /api/addresses/{id}  (unchanged)

### DB changes (portfolio_db.py)
- `excluded INTEGER NOT NULL DEFAULT 0` on `portfolio_addresses`, added via guarded migration
  (check `PRAGMA table_info`, `ALTER TABLE` if missing) inside `init_portfolio_db`.
- `set_address_fields(path, address_id, *, label=..., excluded=...)` (or equivalent) for PATCH.
- `aggregate_history(path, limit)` per algorithm above.
- All address dicts returned by db functions include `excluded` as bool.
- New tests in tests/test_portfolio_db.py for: migration adds column to a pre-existing db, excluded toggle, aggregate_history carry-forward math.

## Design system (frontend implements)

### Palette (CSS custom properties on :root, dark default)
```
--bg-0:#08090c --bg-1:#0e1015 --bg-2:#131620 --bg-3:#1a1e2b --bg-4:#212637
--border-subtle:#1f2330 --border-default:#2a2f42 --border-strong:#3a4160
--text-primary:#f3f5f9 --text-secondary:#a4abc2 --text-tertiary:#6b7290 --text-disabled:#454a5f
--accent:#7c5cff --accent-hover:#9178ff --accent-dim:#7c5cff1a --accent-glow:#7c5cff4d
--success:#22c55e --success-dim:#22c55e1a --danger:#f5455c --danger-dim:#f5455c1a
--warn:#f5a623 --warn-dim:#f5a6231a --info:#38bdf8 --info-dim:#38bdf81a
--venue-evm:#627eea --venue-lighter:#00d4a0 --venue-hl:#ff5fa2
```
Light theme via `[data-theme="light"]` swapping the same custom properties (dark is the designed experience; light is a sane inversion). Respect `prefers-color-scheme` on first load only; persist choice in localStorage.

### Typography
- `--font-ui: -apple-system, "Segoe UI", "Inter", system-ui, sans-serif`
- `--font-mono: "SF Mono", "Cascadia Code", "JetBrains Mono", ui-monospace, Consolas, monospace`
- No font files vendored (system stack only — skip Inter download).
- `.num { font-variant-numeric: tabular-nums; font-feature-settings: "tnum" 1; }` on every number.
- Addresses in mono, truncated `0x71C7…9e3F`, full address in title attr + copy button.
- Scale: 11/13/14/16/20/28/40px; hero total is 40px bold.

### Spacing/radius/shadows/motion
- Spacing: 4/8/12/16/20/24/32/40/48px. Card padding 20px, grid gutter 16px, page margin 32px (16px mobile), max-width 1440px centered.
- Radius: 6 (chips) / 10 (inputs, buttons) / 14 (cards) / 20 (modals) / 999 (pills).
- Shadows: `0 1px 0 0 rgba(255,255,255,.03) inset, 0 4px 12px rgba(0,0,0,.4)` cards; accent glow reserved for hero card border + active refresh button.
- Glass (blur 12px, saturate 140%) ONLY on sticky header + modals/toasts, never on cards.
- Easing `cubic-bezier(.2,.8,.2,1)`, durations 120/200/320ms. Animate only transform/opacity/background/box-shadow (+ the grid-template-rows 0fr→1fr expand trick).

### Layout (desktop-first, breakpoints 1024/768/480)
1. Sticky glass header (64px): wordmark "Portfolio", global search input (Ctrl+K), privacy-eye toggle, theme toggle, refresh button, overflow menu (export, copy all addresses, shortcuts cheatsheet, settings drawer).
2. KPI hero row `2fr 1fr 1fr 1fr`: hero card (total included USD, 40px, count-up on first paint only, 24h delta ±$ and ±% colored, inline SVG sparkline of last ~30 aggregate points, "Data as of Xm ago"), then 3 stat cards: 24h change detail, active addresses count, sources status breakdown (ok/degraded/error/idle counts as colored dots).
3. History chart card (full width, ~320px): uPlot line of GET /api/history, range pills 1D 7D 30D 90D ALL (default 30D), accent gradient fill under line, crosshair tooltip, empty state "Refresh at least twice to see history". Export button (CSV/JSON of history) in card header.
4. Row `5fr 7fr`: allocation card (segmented Venue|Chain|Token toggle, hand-rolled SVG donut with rAF arc-morph ~250ms, legend top-6 + Other, legend rows cross-highlight arcs) | filter/control bar (chain filter chips multi-select from chains with nonzero balance + Lighter/Hyperliquid venue chips, search-bound, sort dropdown, cards/table view toggle persisted, dust toggle with editable $1.00 threshold, hide-empty, status filter, "Clear filters" when active).
5. Addresses section: cards grid `repeat(auto-fill, minmax(340px,1fr))` OR dense table (sortable column headers with ▲▼, sticky header). Each address: color-hash avatar dot, label (inline-editable → PATCH), truncated address + copy + Etherscan-style explorer links, total USD, delta vs previous snapshot, status pill, venue dots, last-refreshed relative time (30s interval re-render), per-card refresh button, exclude toggle, remove (with confirm). Excluded addresses render at 50% opacity grouped in a collapsed "Excluded (N)" sub-section at the bottom.
6. Expandable address detail (accordion, grid-rows trick, multiple can be open, Esc collapses all): tabs Chains | Tokens | Lighter | Hyperliquid.
   - Chains: per-chain USD bar list sorted desc, coverage "n/m tokens found", per-chain error string if any, explorer link per chain.
   - Tokens: sortable table symbol/chain/balance/price/24h%/value/% of address.
   - Lighter: collateral, available balance, staked LIT amount + USD + staking PnL, per-subaccount blocks, positions table (symbol, side badge, size, entry, value, uPnL colored, liq price, leverage).
   - Hyperliquid: perp account value, withdrawable, margin used/notional, positions table (coin, side badge LONG/SHORT, size, entry, value, uPnL, ROE%, liq price), spot balances table.
   - Degraded/error addresses show an error panel listing each error string (full list, not truncated).
7. Global Holdings section (collapsible, closed by default, state persisted): all tokens merged by symbol across addresses+chains+HL spot — total balance, total USD, count of addresses/chains, expandable per-row breakdown. Dust filter applies.

### Behaviors
- Initial load: skeleton shimmer blocks matching layout (only on first paint; refreshes keep data on screen).
- Refresh-all: POST /api/refresh then poll /api/refresh/status every 1s; header refresh icon shows determinate progress ring (completed/total); on finish, toast "Refreshed N addresses — M degraded" and re-fetch summary+history. Per-address refresh: blocking POST, thin indeterminate bar on that card.
- Toasts bottom-right, max 3, auto-dismiss 5s (8s errors), slide-in.
- Privacy mode: blur(6px) + user-select:none on all USD/token quantities, hover-to-reveal, Ctrl+Shift+P, persisted.
- Keyboard: R refresh-all (when no input focused), Ctrl/Cmd+K search, Ctrl+Shift+P privacy, Esc collapse/close, 1/2 cards/table view, ? shortcuts modal.
- Number formatting: ≥$100K abbreviate ($482.4K/$1.23M/$12.1B), below full with separators; token qty ≤6 decimals trimmed, <0.01 shows "<0.01"; full precision in title tooltip; signed percents with U+2212 minus; 1 decimal.
- Export: client-side Blob. JSON = full summary state; CSV = one row per token holding/position (address, venue, chain, symbol, balance, price, value) and history CSV = ts,total_usd.
- Empty states: no addresses → centered "Add your first address" CTA; filters match nothing → inline "No matches — clear filters".
- Explorer URL map: read exact chain keys from `EVM_CHAINS` config in src/portfolio_fetcher.py and map each to its explorer base URL (etherscan.io, basescan.org, arbiscan.io, optimistic.etherscan.io, polygonscan.com, bscscan.com, snowtrace.io, gnosisscan.io, celoscan.io, lineascan.build, scrollscan.com, explorer.zksync.io, mantlescan.xyz, blastscan.io, ftmscan.com, cronoscan.com, moonbeam.moonscan.io, explorer.metis.io, opbnb.bscscan.com, kavascan.com, sonicscan.org, hyperevmscan.io — verify/adjust names sensibly).
- Settings drawer (right side): theme, default view, dust threshold, default chart range; "your data never leaves this machine" note.

### uPlot vendoring
Download once at build time (curl) into `src/portfolio_static/vendor/`:
- https://cdn.jsdelivr.net/npm/uplot@1.6.31/dist/uPlot.iife.min.js
- https://cdn.jsdelivr.net/npm/uplot@1.6.31/dist/uPlot.min.css
If download fails, hand-roll a minimal SVG line chart with the same features (range pills, hover tooltip) instead — do not reference any CDN at runtime.

## Payload reference (what the frontend can render)

Every `addresses[].snapshot` in /api/summary is the full normalized payload:
- `totals`: total_usd, chains_usd, lighter_usd, hyperliquid_usd, lit_staked, lit_staked_usd
- `chains[]`: key, name, chain_id, ok, error, native{symbol,balance,price_usd,value_usd}, tokens[]{symbol,name,id,contract,balance,decimals,price_usd,value_usd,rank,change_24h}, total_usd, coverage{tokens_checked,tokens_nonzero}
- `lighter`: ok, errors[], account_count, total_usd, account_assets_usd, collateral, available_balance, staking{staked_lit,staked_lit_value_usd,lit_price_usd,staking_pnl}, accounts[]{index,name,available_balance,collateral,total_asset_value,assets[]{symbol,balance,locked_balance,margin_balance},positions[]{symbol,side,size,entry_price,position_value,unrealized_pnl,realized_pnl,liquidation_price,leverage},staking{...,staking_inflow,staking_outflow}}
- `hyperliquid`: ok, errors[], total_usd, perp{account_value,withdrawable,margin_summary{total_margin_used,total_notional_position,total_raw_usd},positions[]{coin,side,size,entry_price,position_value,unrealized_pnl,return_on_equity,liquidation_price}}, spot{total_usd,balances[]{coin,total,hold,price_usd,value_usd}}
- `token_catalog`: source, updated_at, top_market_count, target_count, targets_by_chain{}
- `errors[]`: flattened strings

DEGRADED SHAPE WARNING: error-path payloads have `chains: []`, lighter missing most keys, hyperliquid missing `perp`/`spot` entirely. Null-guard everything.

## Acceptance
- Full pytest suite passes (`python -B -m pytest`), including new tests.
- `python -B -m py_compile src/portfolio_app.py src/portfolio_db.py src/portfolio_fetcher.py` passes.
- `node --check src/portfolio_static/app.js` passes.
- Server starts, `GET /` serves the new dashboard, all API routes respond.
