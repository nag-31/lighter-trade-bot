# Portfolio Overview Webapp Spec And Agent Handoff

## Purpose

Build and continue a local-only portfolio overview webapp inside this repository.
The app lets the user import one or more EVM wallet addresses, persist them in
SQLite, refresh balances on demand, and view portfolio totals across onchain EVM
chains, Morpho, Aave, Spark, Lighter, and Hyperliquid.

The current implementation is already started and should be continued from the
existing files, not rebuilt from scratch.

## Current User Requirements

- Import one or more EVM addresses.
- Persist addresses and refresh snapshots in a local DB.
- Support multiple addresses.
- Support Lighter balances/positions and Hyperliquid balances/positions.
- Support Ethereum, Arbitrum, BNB, HyperEVM.
- Expanded requirement: support as many major EVM chains as practical, including
  Base and Polygon.
- Scan top 200 tokens, excluding non-EVM chains.
- Use public endpoints first; user may later provide Alchemy or another endpoint.
- Include staked LIT balance.
- Local-only app, no auth required for now.
- Manual refresh button.
- Nice, clean, appealing portfolio UI.
- Sort options and filters.
- Option to toggle addresses out of totals without deleting them.
- Include Morpho, Aave V3, SparkLend, Spark Savings vaults, sUSDS, legacy
  Spark sUSDC, and stSPK staking with supply, collateral, debt, net equity,
  health factor, market, vault, asset, and chain details.

## Important Files

- `src/portfolio_app.py`
  - Local `aiohttp` web server and JSON API. The embedded `INDEX_HTML` string
    has been removed; the frontend is now served from disk.
  - Entry point: `python -B -m src.portfolio_app`.
  - Serves `src/portfolio_static/` at `/static/` (via `add_static`).
  - `GET /` reads `src/portfolio_static/index.html` from disk at request time
    (hot-reloads on edit) with `Cache-Control: no-cache, must-revalidate`. If the
    file is missing it returns a plain `503` "frontend not built" page instead of
    crashing.
  - API routes for addresses, refresh (async job + status), summary, and history.

- `src/portfolio_static/` (new — the entire frontend, vanilla JS, no build step)
  - `index.html` — dashboard markup; references assets under `/static/`.
  - `style.css` — full design system (dark default, light via `[data-theme]`).
  - `app.js` — all rendering + API calls; no framework.
  - `vendor/uPlot.iife.min.js`, `vendor/uPlot.min.css` — vendored uplot@1.6.31
    (history line chart only). No CDN referenced at runtime. Donut is hand-rolled SVG.

- `src/portfolio_fetcher.py`
  - Public data fetchers.
  - EVM JSON-RPC native/ERC20 balance scanner.
  - CoinGecko top-200 token catalog.
  - Lighter account, asset, position, PnL/staked-LIT fetches.
  - Hyperliquid perp/spot state fetches.
  - Aggregates lending net equity and removes detected receipt-token duplicates.

- `src/portfolio_defi.py`
  - Morpho positions from the official Morpho GraphQL API.
  - Aave V3, SparkLend, Spark Savings, and stSPK positions from official
    deployments through public chain RPCs.
  - Values ERC-4626 savings shares with onchain convertToAssets; values stSPK
    1:1 in SPK; uses DefiLlama only for underlying token prices.
  - Excludes Spark Liquidity Layer treasury assets because they belong to the
    protocol ALMProxy, not to the wallet being scanned.
  - Supports both the current Aave Origin four-field user-reserve ABI and the
    legacy seven-field ABI retained by older Aave-compatible deployments.
  - Isolates failures by protocol and deployment.

- `tests/test_portfolio_defi.py`
  - Morpho market/vault parsing, Aave-compatible balances and debt, Spark
    ERC-4626 conversion, stSPK accounting, stablecoin pricing fallback, health
    factor decoding, receipt-token deduplication, and failure-isolation tests.

- `src/portfolio_db.py`
  - SQLite persistence for imported addresses and refresh snapshots.

- `tests/test_portfolio_fetcher.py`
  - Address parsing, token target mapping, staking parser tests.

- `tests/test_portfolio_db.py`
  - Address persistence and snapshot persistence tests.

- `requirements.txt`
  - Includes `truststore>=0.10.0` so Python HTTP clients can use the Windows OS
    certificate store for public HTTPS endpoints.

- `data/portfolio.db`
  - Runtime SQLite DB, generated locally.
  - Should remain ignored/untracked unless the user explicitly asks otherwise.

## How To Run

From repository root:

```powershell
Set-Location "D:\content\crypto scientist\lighter-trade-bot"
& "C:\Python314\python.exe" -B -m src.portfolio_app --host 127.0.0.1 --port 8790
```

Optional seed address:

```powershell
Set-Location "D:\content\crypto scientist\lighter-trade-bot"
& "C:\Python314\python.exe" -B -m src.portfolio_app --host 127.0.0.1 --port 8790 --seed-address 0x2222222222222222222222222222222222222222/stream
```

Open:

```text
http://127.0.0.1:8790/
```

## DeFi Lending Sources And Accounting

- Morpho: `https://api.morpho.org/graphql`. The API returns wallet market,
  MetaMorpho vault, and Vault V2 positions with protocol-calculated USD values.
- Aave: direct `eth_call` reads against deployments from the official Aave
  address book. Public RPC endpoints are used first.
- SparkLend: Aave V3-compatible positions are read onchain from the official
  Spark deployment.
- Spark Savings: current Spark Vaults V2, sUSDS deployments, and legacy sUSDC
  are read directly from contracts. Share balances are converted to currently
  redeemable underlying assets with ERC-4626 convertToAssets.
- Spark Staking: stSPK is read directly and accounted 1:1 as staked SPK.
- Spark Liquidity Layer is deliberately not added to a wallet because its
  ALMProxy holds protocol-owned liquidity rather than user-attributable shares.
- DefiLlama is not a wallet-position source. It supplies current underlying
  prices for Spark savings/staking after positions are discovered onchain.
  Stablecoin parity is used only if a stablecoin price is temporarily absent.

Portfolio accounting rules:

```text
Morpho market net = lending supply + separate collateral - borrowed
Morpho vault net  = vault assets
Aave/SparkLend net = supplied aToken balance - stable/variable debt
Spark Savings net   = redeemable underlying assets
Spark Staking net   = stSPK balance x SPK price
Portfolio total     = wallet assets + DeFi net + Lighter + Hyperliquid
```

For Aave and SparkLend, collateral is an informational subset of supplied
assets and is never added a second time. Aave aTokens, Spark aTokens, Spark
Savings shares, stSPK, and Morpho vault-share tokens detected in ordinary wallet
holdings are removed before the chain total
is calculated, preventing receipt-token double counting. Debt is always
subtracted from DeFi net equity.

The DeFi payload is stored inside each normal SQLite snapshot. No new database
table or destructive migration is required; existing overview and per-wallet
history automatically include the new net value after the next refresh.

## Default Or Seed Address

The user-provided example address is:

```text
0x2222222222222222222222222222222222222222/stream
```

The app normalizes this to:

```text
0x2222222222222222222222222222222222222222
```

There is no hard-coded permanent default address. To change the startup seed,
start the server with a different `--seed-address`. Once an address is saved in
SQLite, edit the visible list through the dashboard: add the replacement address
and remove or exclude the old one. A future improvement can add inline address
replacement, but today address replacement is remove+add.
## Current API Routes

### `GET /`

Reads and returns `src/portfolio_static/index.html` from disk (no-cache). `503`
"frontend not built" if the file is missing.

### `GET /static/...`

Static file mount for `src/portfolio_static/` (css, js, vendored uplot).

### `GET /api/summary`  (extended)

Returns:

- enabled addresses; each entry gains `"excluded": bool`
- latest snapshot per address (`snapshot` + `latest`)
- `totals` — all-addresses aggregate sum (back-compat, unchanged shape)
- `totals_included` — same shape as `totals`, summing only NON-excluded addresses
- token catalog metadata
- status: `idle`, `ok`, `degraded`, or `error`

### `POST /api/addresses`

Body:

```json
{ "address": "0x...", "label": "Main" }
```

Behavior:

- Extracts the first valid `0x` EVM address from the string.
- Accepts suffixes like `/stream`.
- Upserts the address; re-adding the same address updates the label.
- Response: `{"ok": true, "address": {id, address, label, enabled, excluded, created_at, updated_at}}`.

### `PATCH /api/addresses/{id}`  (new)

Body: `{"label": "optional str", "excluded": optional bool}` — either or both.

- Updates label and/or server-side exclusion state.
- Response: `{"ok": true, "address": {id, address, label, enabled, excluded, created_at, updated_at}}`.
- `404` if the id is unknown or disabled.

### `DELETE /api/addresses/{id}`

Soft-disables the address in SQLite.

Important: this is distinct from the "Exclude" toggle. Delete/remove hides the
address from future summaries entirely. Exclude (see PATCH) is now SERVER-SIDE
and non-destructive — it drops the address from `totals_included` but keeps it in
`totals` and in the address list.

### `POST /api/addresses/{id}/refresh`

Refreshes one address and saves a snapshot. Stays BLOCKING; returns
`{"ok": true, "snapshot": <full payload>}`.

### `POST /api/refresh`  (changed: now an async background job)

Starts a background refresh of all enabled addresses and returns immediately:

- `{"ok": true, "started": true}` (HTTP 200)
- if a refresh is already running: `{"ok": false, "error": "refresh already running"}` (HTTP 409)

Job state is in-memory, guarded by the refresh lock.

### `GET /api/refresh/status`  (new)

Poll for background-refresh progress:

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

`results` accumulates one entry per address as each finishes (`status` is the
snapshot status: `ok`/`degraded`/`error`).

### `GET /api/history?limit=1000`  (new)

Aggregate portfolio history across enabled, non-excluded addresses. Walks all
snapshot events in ts order, carries forward each address's last-known total, and
emits one summed point per event ts. Returns the last `limit` points:

```json
{"ok": true, "history": [{"ts": "ISO", "total_usd": 123.4}]}
```

Backed by `portfolio_db.aggregate_history(path, limit)`.

### `GET /api/addresses/{id}/history?limit=300`  (new)

Per-address snapshot history, ascending by ts, no payloads:

```json
{"ok": true, "history": [{"id": 1, "ts": "ISO", "status": "ok", "total_usd": 123.4}]}
```

Backed by the existing `portfolio_db.snapshot_history`.

## SQLite Schema

`portfolio_addresses`

- `id`
- `address`
- `label`
- `enabled`
- `excluded` — `INTEGER NOT NULL DEFAULT 0`. Added via a guarded migration in
  `init_portfolio_db` (checks `PRAGMA table_info`, runs `ALTER TABLE` only if the
  column is missing, so it upgrades pre-existing DBs in place). All address dicts
  returned by db functions include `excluded` as a Python `bool`.
- `created_at`
- `updated_at`

New `portfolio_db.py` helpers:

- `set_address_fields(path, address_id, *, label=..., excluded=...)` — backs PATCH.
- `aggregate_history(path, limit)` — carry-forward portfolio-total time series
  across enabled, non-excluded addresses (see `GET /api/history`).

`portfolio_snapshots`

- `id`
- `address_id`
- `ts`
- `status`
- `total_usd`
- `payload`
- `error`

Snapshots store the normalized portfolio payload as JSON. Latest snapshot is
selected by max snapshot id per address.

## Snapshot Payload Shape

High-level:

```json
{
  "address": "0x...",
  "address_masked": "0x1234...abcd",
  "timestamp": "...",
  "status": "ok",
  "totals": {
    "total_usd": 0,
    "chains_usd": 0,
    "lighter_usd": 0,
    "hyperliquid_usd": 0,
    "lit_staked": 0,
    "lit_staked_usd": 0
  },
  "chains": [],
  "lighter": {},
  "hyperliquid": {},
  "token_catalog": {},
  "errors": []
}
```

## Supported EVM Chains

Current chain config covers 25 chains:

- Ethereum
- Base
- Arbitrum
- Optimism
- Polygon
- BNB Chain
- Avalanche
- Gnosis
- Celo
- Linea
- Scroll
- zkSync Era
- Mantle
- Blast
- Fantom
- Cronos
- Moonbeam
- Metis
- opBNB
- Kava
- Sonic
- HyperEVM
- Unichain
- X Layer
- Robinhood Chain

The app uses public RPC endpoints. Chain refreshes are concurrency-limited to 5
chains at a time to reduce public endpoint rate-limit failures.

## Token Scanning

Current behavior:

- CoinGecko `/coins/markets` with `per_page=200`.
- CoinGecko `/coins/list?include_platform=true`.
- Filters CoinGecko platforms to supported EVM platform ids.
- Builds chain+contract ERC20 targets from top-200 market coins, then merges a
  deduplicated core-contract list for metadata gaps such as Base, Polygon, and
  HyperEVM stablecoins.
- Excludes ERC-20-compatible native system contracts so native balances are not
  counted twice.
- Calls ERC20 `balanceOf(address)` and `decimals()` through public JSON-RPC.
  Silently omitted batch IDs are retried in progressively smaller groups.
- Includes only non-zero balances in the UI.
- Fetches native-token prices for all supported chain native tokens, plus LIT.

Current CoinGecko platform mappings include:

- `ethereum`
- `base`
- `arbitrum-one`
- `optimistic-ethereum`
- `polygon-pos`
- `binance-smart-chain`
- `avalanche`
- `xdai`
- `celo`
- `linea`
- `scroll`
- `zksync`
- `mantle`
- `blast`
- `fantom`
- `cronos`
- `moonbeam`
- `metis-andromeda`
- `opbnb`
- `kava`
- `sonic`
- `hyperevm`
- `unichain`
- `x-layer`
- `robinhood`

Known issue:

- CoinGecko public endpoints can return `429 Too Many Requests`.
- When that happens, the app falls back to a small common-token list and marks
  the snapshot `degraded`.
- The process retains the last good top-200 catalog and uses it stale on later
  failures; a small common-token list is the cold-start fallback.
- Future improvement: persist that catalog across process restarts.

## Lighter Support

Current Lighter endpoints:

- `GET /api/v1/accountsByL1Address`
- `GET /api/v1/account`
- `GET /api/v1/pnl` (best-effort only — see below)
- `GET /api/v1/tokenlist` was used during research to confirm LIT metadata:
  `symbol=LIT`, CoinGecko id `lighter`, ERC-20 on Ethereum
  `0x232ce3bd40fcd6f80f3d55a522d03f25df784ee2`.

Current Lighter behavior:

- Resolve an L1 wallet into main accounts, subaccounts, and owned public-pool
  accounts, deduplicated by account index.
- For up to 20 account indexes, fetch details per account. For larger operators,
  use one address-level detailed account request to stay within the public
  endpoint's 60-request/minute budget.
- Fetch account assets, collateral, available balance, positions, and pool-share
  metadata.
- Value an owned public pool as
  `operator_shares / total_shares * full_pool_total_asset_value`; the pool's
  full TVL is display-only and is never counted as the operator's wealth.
- Treat Lighter HTTP 400 on address lookup as "no Lighter account found", not a
  fatal error.

### Staked LIT (source of truth: public-pool shares)

LIT staking on Lighter is a **public pool** (the LIT staking pool, account
index `281474976624800`). A user's staked LIT is NOT an asset field on their
own account — it is their `shares[]` entry for that pool, valued
proportionally:

```
staked_lit = user shares_amount / pool total_shares × pool LIT balance
```

Implementation: `fetch_lighter_pool()` fetches the pool account
(`GET /api/v1/account?by=index&value=<pool_index>`) for `pool_info.total_shares`
and the pool's asset balances; each user `shares[]` entry is valued by that
ratio. Verified live to high precision against the official staking page
(`https://app.lighter.xyz/staking` uses `GET /api/v1/publicPoolsMetadata
?filter=stake` + the same pool account/shares data) and against DeBank's
"LIT Staking" position (wallet `0xde95...4c74`: 15,043 LIT ≈ DeBank's
15,041.62; drift = accrued rewards/timing).

Wrong approaches, kept documented so nobody regresses to them:
- The LIT asset's `locked_balance` is an exchange-side spot lock, NOT staking.
  It reports 0 for most real stakers and a wrong partial number for others. It
  is surfaced only as informational `staking.locked_lit` / `lit.locked_lit`.
- `GET /api/v1/pnl` chart fields are **never** the source of `staked_lit`
  (inconsistent account coverage; HTTP 400 `code 21100` for many accounts).
  `staking_pnl` / `staking_inflow` / `staking_outflow` are attached best-effort
  only (`staking.pnl_source` = `"pnl"` / `"unavailable"`); a `/pnl` failure can
  never zero out `staked_lit`.
- There is no separate on-chain L1 staking contract to query: the user wallets
  hold 0 LIT on Ethereum L1. DeBank's figure comes from the same zkLighter
  pool-shares data.

### pending_unlocks and pool deposits

- pending_unlocks on /account = in-flight unstake requests
  ({unlock_timestamp, asset_index, amount}, about 3-day lockup). LIT-relevant
  ones are normalized to staking.pending_unlocks with rollups
  staking.pending_unstake_lit / pending_unstake_lit_value_usd.
- Non-staking shares entries (other public/yield pools / LLP) are shaped to
  per-account pool_deposits with public_pool_index, principal_amount,
  shares_amount, entry_timestamp, current value, and underlying rows when known.
- Two pool valuation shapes are supported:
  - asset-backed pools such as LIT staking: user shares / total shares x pool
    asset balances. LIT-valued pools feed staking.staked_lit.
  - account-style yield pools such as Rabby/Lighter NK pool: user shares / total
    shares x pool total_asset_value. These feed pool_deposits_usd as USDC value
    and include pool_name, pool_value_usd, and value_source.


### Totals formula (double-count-safe)

```
total_usd = account_assets_usd + lit_value_usd + pool_deposits_usd
```

- `account_assets_usd` = personal main/subaccount USDC and perp equity plus the
  operator's pro-rata equity in owned public pools. It does not include LIT.
- `operator_pool_value_usd` is the owned-pool component already contained in
  `account_assets_usd`; it is exposed for detail and must not be added again.
- Full owned-pool `total_asset_value` is TVL metadata only.
- `lit_value_usd` covers spot + pool-share-staked LIT exactly once.
- `pool_deposits_usd` covers non-staking pool shares held by personal accounts.
See the return dict of `fetch_lighter()` in `src/portfolio_fetcher.py` for the
exact key layout (`staking.staked_lit`, `staking.pool_staked_lit`,
`staking.locked_lit`, `staking.source` = `"public_pool_shares"` / `"none"`).

Live reconciliation (wallet `0xde95...4c74`): staked ≈ 15,043 LIT ≈ $33.3k,
matching the official staking page and DeBank.

## Hyperliquid Support

Current Hyperliquid endpoint:

- `POST https://api.hyperliquid.xyz/info`

Payload types used:

- `clearinghouseState` for default and builder-deployed perp DEXs
- `spotClearinghouseState`
- `allMids`
- `spotMeta`
- `userAbstraction`
- `perpDexs`
- `userVaultEquities`
- `delegatorSummary`
- `userRole`

Current Hyperliquid behavior:

- `marginSummary.accountValue` is authoritative perp equity and already
  includes open unrealized PnL. The UI separately exposes
  `accountValue - total_unrealized_pnl` and total unrealized PnL.
- Account mode controls the direct-equity formula:
  - Standard (`disabled`): spot and every perp DEX are separate, so direct
    equity is spot value plus the sum of all perp DEX account values.
  - Unified or portfolio margin: `spotClearinghouseState` is the unified
    balance source; individual perp DEX states are detail only and are not
    added again.
- Standard accounts enumerate `perpDexs` and query every DEX. This is
  required for HIP-3 balances such as the `xyz` DEX.
- Spot token prices are resolved from `spotMeta` token/pair indexes and
  `allMids["@<pair index>"]`. Named `@TOKEN` lookups are invalid.
- Vault equity from `userVaultEquities` is added once.
- Delegated, undelegated, and pending-withdrawal HYPE from
  `delegatorSummary` are valued with the metadata-resolved HYPE price and
  added once.
- Agent wallet addresses are detected through `userRole` and produce a
  visible warning instructing the user to import the master or subaccount.
- Linked subaccounts are not rolled into the master address. Hyperliquid's
  official master portfolio sample excludes them, and importing both would
  otherwise double count.
- Unpriced nonzero spot tokens and unavailable required account-mode data
  degrade the venue result instead of silently reporting a complete total.

The exact total is:

```
hyperliquid_total =
    direct_trading_equity(account_mode)
  + vault_equity
  + staking_hype_value
```

Live reconciliation and public fixture addresses are recorded in
`PORTFOLIO_LIVE_VALIDATION.md`.

## Dashboard UI

The UI was rebuilt as a single-page vanilla-JS dashboard (`src/portfolio_static/`)
with a full dark-first design system. Feature list:

Layout / sections:

- Sticky glass header: wordmark, global search (Ctrl+K), privacy-eye toggle,
  theme toggle, refresh-all button (with determinate progress ring), overflow
  menu (export JSON, export holdings CSV, copy all addresses, shortcuts, settings).
- KPI hero row: total included USD (40px, count-up on first paint), 24h delta
  (± $ and ± %), inline sparkline of recent aggregate history, plus stat cards
  (24h change, active address count, source status breakdown).
- History chart card: uPlot line of `GET /api/history` with range pills
  (1D/7D/30D/90D/ALL, default 30D), gradient fill, crosshair tooltip, empty state,
  and per-chart CSV/JSON export.
- Allocation card: segmented Venue | Chain | Token toggle, hand-rolled animated
  SVG donut, legend (top-6 + Other) with arc cross-highlight.
- Filter/control bar: venue chips (Lighter/Hyperliquid), chain chips (from chains
  with nonzero balance), sort dropdown, status filter, cards/table view toggle,
  dust toggle with editable threshold, hide-empty, "Clear filters".
- Addresses section: cards grid or dense sortable table. Per address: avatar dot,
  inline-editable label (→ PATCH), truncated address + copy + explorer links,
  total USD, status pill, venue dots, relative last-refreshed time, per-card
  refresh, exclude toggle, remove (confirm). Excluded addresses render at 50%
  opacity in a collapsed "Excluded (N)" sub-section.
- Expandable address detail (accordion): tabs Chains | Tokens | Lighter |
  Hyperliquid; degraded/error addresses show a full error panel.
- Global Holdings section (collapsible): all tokens merged by symbol across
  addresses/chains/HL-spot.

Behaviors:

- Exclusion is now SERVER-SIDE (PATCH `excluded`), not `localStorage`. The old
  `portfolio.excludedAddressIds` key is gone; excluded addresses are dropped from
  `totals_included` server-side.
- Refresh-all: POST `/api/refresh` then poll `/api/refresh/status` every 1s; the
  header ring shows completed/total; on finish a toast reports "Refreshed N — M
  degraded" and summary + history re-fetch. Per-address refresh is blocking.
- Skeleton shimmer on first paint only; later refreshes keep data on screen.
- Toasts bottom-right (auto-dismiss). Privacy mode blurs USD/quantities
  (Ctrl+Shift+P, persisted). Keyboard: R, Ctrl/Cmd+K, Ctrl+Shift+P, Esc, 1/2, ?.
- Number formatting: abbreviate ≥$100K, tabular-nums, U+2212 minus, "<0.01" for
  tiny quantities, full precision in tooltips.
- Export is fully client-side (Blob download): JSON = full summary state; holdings
  CSV = one row per token/position; history CSV = `ts,total_usd`. No backend
  export endpoint.
- Preferences (theme, default view, dust threshold, default chart range, chart
  range, holdings-open) persist in `localStorage`; exclusion does not.

Note: the current frontend renders an in-app add-address modal from the header.
The modal accepts pasted EVM addresses, tolerates suffixes like /stream, posts
to POST /api/addresses, then refreshes the new address and reloads summary /
history state. Per-address history is fetched through
GET /api/addresses/{id}/history and used for card/table deltas; a larger
per-address history drilldown can still be added later.

## Current Verification State

Last pass (2026-07-07):

- Full suite: 735 passed, 70 warnings.
- Focused portfolio tests: tests/test_portfolio_fetcher.py,
  tests/test_portfolio_app.py, and tests/test_portfolio_db.py pass (59 tests).
- Live /api/summary on port 8790 responded ok:true with persisted addresses,
  snapshots, and aggregate totals.
- Hyperliquid accounting is corrected: total uses spot/collateral when present,
  otherwise perp account value. Perp equity, balance-before-uPnL, and open uPnL
  remain visible as detail fields.
- Lighter staked LIT uses public-pool shares; tests cover that /pnl failures do
  not zero the staked balance.
- Lighter account-style yield pools are valued from pool total_asset_value; live kl/NK pool verification matched Rabby within timing drift.
- Frontend includes add-address modal, exclude toggles, filters, sorting, history,
  allocation, exports, and venue detail tabs.
- API tests cover re-adding a disabled address and accepting suffixes like /stream.
- app.js display glyph mojibake was repaired for minus signs, ellipses, arrows,
  and separators.

JavaScript syntax passes `node --check`. Automated browser screenshot QA was
blocked by the local Windows sandbox ACL; API/runtime interaction smoke checks
were completed instead.

Run:

    python -B -m src.portfolio_app --host 127.0.0.1 --port 8790

Open http://127.0.0.1:8790/ and hard-refresh after frontend edits.

## Known Limitations

1. CoinGecko public rate limits can degrade refreshes.
   - Recommended next task: cache successful CoinGecko catalog persistently.

2. Public RPC endpoints can rate limit or intermittently fail.
   - Recommended next task: allow user-configured RPC URLs per chain.
   - Later: plug in Alchemy/Ankr/Infura/etc.

3. ERC20 discovery is limited to CoinGecko top-200 token contracts on supported
   platforms.
   - This is not a full wallet indexer.
   - NFTs, LP positions, long-tail tokens, and protocol positions are out of
     scope for the current public-endpoint pass.

4. HyperEVM supports native HYPE, CoinGecko `hyperevm` platform contracts, and
   a core USDC fallback. Long-tail assets still require a full indexer.

5. RESOLVED (Jul 2026): Lighter staked LIT is validated live — pool-share
   method (see "Staked LIT" section) reproduces the official staking page and
   DeBank figures for a real staking wallet.

6. Tooling caveat (Jul 2026): browser/screenshot verification can be blocked by
   local Windows sandbox ACLs or Codex usage limits. When available, use browser
   visual QA to compare the dashboard against user screenshots.

## Recommended Next Work

### High Priority

1. Persist CoinGecko token catalog.
   - Add table or JSON cache:
     - source
     - updated_at
     - targets
     - prices
   - On CoinGecko 429, use stale cached top-200 targets and mark a small warning,
     not full degraded.

2. Add configurable RPC endpoints.
   - Environment variables or YAML.
   - Example:
     - `PORTFOLIO_RPC_ETHEREUM`
     - `PORTFOLIO_RPC_BASE`
     - `PORTFOLIO_RPC_POLYGON`
   - Use public defaults when unset.

3. Add address-replacement UI.
   - Current behavior supports in-app add, inline label edit, exclude, and remove.
   - Editing an address itself is still remove+add.
   - A future improvement can provide a direct replace-address flow.

### Done In The UI Revamp (kept here for history)

- Refresh progress state in UI — DONE (async `/api/refresh` + `/api/refresh/status`
  polling with a header progress ring).
- Snapshot history view — DONE (uPlot history chart via `/api/history`, plus
  `/api/addresses/{id}/history`).
- CSV/JSON export — DONE (client-side Blob download).
- Server-side exclude — DONE (`excluded` column + PATCH + `totals_included`).
- Chain filter chips — DONE in the filter bar.

### Medium Priority

4. Add a larger per-address history drilldown.
   - The frontend already fetches per-address history for card/table deltas.
   - A future detail view could show a full address-specific chart and snapshot log.

### Later

8. Add full indexer support when user provides keys.
   - Alchemy/Moralis/Covalent/Zerion/DeBank options.
   - This would solve long-tail token discovery and more accurate USD pricing.

9. Add authentication if app is ever exposed beyond localhost.

## Handoff Instructions For Another Agent

1. Do not rewrite the app from scratch.
2. Start by reading:
   - `src/portfolio_app.py`
   - `src/portfolio_fetcher.py`
   - `src/portfolio_db.py`
   - `tests/test_portfolio_fetcher.py`
   - `tests/test_portfolio_db.py`
3. Run:
   ```powershell
   python -B -m pytest tests\test_portfolio_fetcher.py tests\test_portfolio_db.py
   ```
4. Then run full suite:
   ```powershell
   python -B -m pytest
   ```
5. Start app:
   ```powershell
   python -B -m src.portfolio_app --host 127.0.0.1 --port 8790
   ```
6. Validate:
   - `GET /` serves the disk-backed dashboard; `/static/` assets return 200.
   - Add address (`POST /api/addresses`).
   - Refresh (async `POST /api/refresh`, poll `/api/refresh/status`).
   - Confirm `totals` and `totals_included`.
   - Toggle exclude via `PATCH /api/addresses/{id} {"excluded": true|false}` and
     confirm `totals_included` changes while `totals` stays constant.
   - Confirm `/api/history` and `/api/addresses/{id}/history` return points.
   - Use filters/sorts in the UI.
   - Confirm staked LIT appears for a Lighter account with staking.

## Acceptance Criteria

The app is acceptable when:

- Multiple addresses can be added and persist across restarts.
- Refresh all (async job + status polling) and refresh one (blocking) work.
- Address exclude/include (server-side PATCH) changes `totals_included` without
  deleting the address; `totals` stays the all-addresses sum.
- `GET /` serves the disk-backed dashboard and `/static/` assets load.
- Top-200 token catalog scans supported EVM chains when CoinGecko is available.
- Public endpoint failures are visible but do not crash the app.
- Lighter section shows assets, positions, and staked LIT fields.
- Hyperliquid section shows spot/collateral total, perp equity detail, and open uPnL without double-counting collateral/equity.
- UI filters and sorts work without page reload.
- Full test suite passes.

