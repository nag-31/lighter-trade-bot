# Portfolio Edge-Case Failure Analysis

Date: 2026-07-13

## Quality contract

A refresh may be complete, degraded, or failed. It must never silently invent value, double count value, replace the last good current valuation with a synthetic zero, or add a failed zero point to history. Every included component must expose its source and errors. A selected-wallet overview must use exactly the selected enabled wallets at every historical event.

External comparisons are time-sensitive. A match is acceptable when the app and reference API are sampled in the same run and differ only by documented pricing or reward accrual drift.

## Failure matrix

| Area | Edge case | Current risk | Required behavior | Evidence gate |
| --- | --- | --- | --- | --- |
| Address input | Mixed-case, suffix, surrounding text | Wrong address extraction | Normalize one valid EVM address | Unit and API test |
| CSV import | BOM, quoted labels, headers, blank lines | Header or label parsed as data | Import valid rows and retain quoted labels | Parser and API tests |
| CSV import | Duplicate wallet with conflicting labels | Duplicate DB rows or unpredictable label | One row; deterministic first useful label | API test |
| CSV import | More than 1,000 unique wallets | Long blocking request | Reject atomically without partial writes | API test |
| CSV import | Invalid-only paste | Empty success response | HTTP 400 with no DB mutation | API test |
| DB migration | Old schema, repeated initialization | Migration failure | Idempotent in-place upgrade | DB test |
| DB history | Failed refresh after good refresh | Fake drop to zero | Retain diagnostic row but omit it from valuation history | DB/API test |
| Current summary | Failed refresh after good refresh | Latest error payload replaces good current value with zero | Surface error and last-good valuation separately; totals use last good | DB/API test |
| History | Interleaved wallet refresh times | False total jumps | Carry forward each selected wallet's last good value | DB test |
| History | Empty selection, unknown IDs, disabled/excluded IDs | Leakage into overview | Empty means empty; unknown/disabled/excluded contribute zero | DB/API test |
| History | Duplicate timestamps and out-of-order timestamp strings | Unstable chart ordering | Stable by timestamp then snapshot id | DB test |
| Refresh | Refresh-all with zero wallets | Stuck running state | Finish immediately with 0/0 | API test |
| Refresh | Concurrent refresh-all and per-wallet refresh | Double snapshots/races | Return conflict or serialize deterministically | API test |
| Refresh | Wallet removed/imported during a job | Key errors or missed state | Current job uses a stable starting set; next job sees changes | API test |
| Refresh | API timeout/429/5xx | Zero or permanent failure | Bounded retry/backoff where safe; degraded result and explicit error | Fetcher tests |
| Token catalog | CoinGecko 429 or malformed data | No top-200 balances | Use cached last-good catalog, then fallback catalog | Fetcher test |
| EVM RPC | One chain down | Whole wallet fails | Other venues/chains remain valued; status degraded | Fetcher test |
| EVM RPC | Missing/malformed or silently omitted batch IDs | Crash or wrong zero | Retry omitted IDs in smaller batches; explicit error if unresolved | Fetcher/live test |
| ERC-20 | Invalid decimals, huge decimals, duplicate/native-system contracts | Overflow or duplicate value | Bound decimals; deduplicate; exclude ERC-20-compatible native coin | Fetcher/live test |
| Pricing | Token balance without price | Hidden value mistaken for zero | Preserve quantity; WETH uses ETH fallback; unknown assets remain visibly unpriced | Fetcher/UI test |
| Lighter accounts | Pagination repeats or exceeds cap | Infinite loop or silent truncation | Detect repeated cursor and expose truncation warning | Fetcher test |
| Lighter accounts | Duplicate subaccount/account rows | Double count | Deduplicate by account index | Fetcher test |
| Lighter LIT | Spot, locked, staked, pending unstake coexist | Double count | Spot includes locked; staked is pool shares; pending is informational | Fixture test |
| Lighter pool | Zero/missing total shares | Division by zero or fake zero | Keep position unresolved and degrade with pool error | Fetcher test |
| Lighter pool | User shares greater than pool shares | Impossible inflated value | Reject/clamp as invalid and report error | Fetcher test |
| Lighter pool | Same pool held in multiple subaccounts | Missed or duplicate pool fetch/value | Fetch pool once; value each distinct share position once | Fetcher test |
| Lighter pool | Pool contains LIT and USDC | Non-LIT component omitted | Count LIT as staking and USDC as pool deposit independently | Fetcher test |
| Lighter pool | Account-style pool has total_asset_value but no assets | Missing Rabby yield position | Value pro-rata from total_asset_value | Fixture/live test |
| Hyperliquid | Positive or negative unrealized PnL | Equity shown before PnL | accountValue remains authoritative perp equity; expose uPnL reconciliation | Fixture/live test |
| Hyperliquid | Spot and perp balances both exist | Double count or undercount | Reconcile distinct spot holdings and perp equity according to official account semantics | Official API/live test |
| Hyperliquid | Non-HYPE spot token uses @index mid | Unpriced spot balance | Resolve token and pair metadata from spotMeta | Fixture/live test |
| Hyperliquid | Agent wallet instead of master/subaccount | False empty account | Identify empty role where possible and document master-address requirement | API/live test |
| Hyperliquid | Vault deposits or staking delegation | Missing venue assets | Either include as separate components or explicitly mark unsupported | Official API/test |
| Frontend | Overview deselection | Wallet card/refresh disappears | Selection affects overview only, never tracking controls | JS/runtime test |
| Frontend | Newly imported wallets | Imported but absent from selected overview | Select imported wallets automatically | JS/API test |
| Frontend | Refresh returns degraded/error | Old good data visually disappears | Keep last-good value and show stale/error status | Runtime/API test |
| Security/privacy | Local DB/logs/wallet list | Wallets committed to Git | Runtime state remains ignored | Git check |
| Scale | 100+ wallets and 200-token catalog | UI/API stalls, rate limits | Four-wallet bounded concurrency, progress, stable job set, no lost snapshots | 100-wallet load test |

## Live validation wallet classes

The live corpus must include:

1. Empty EVM wallet: proves clean zero behavior.
2. EVM-only wallet with native and ERC-20 balances.
3. Hyperliquid perp wallet with non-zero positive uPnL.
4. Hyperliquid perp wallet with negative uPnL.
5. Hyperliquid spot plus perp wallet, including at least one non-HYPE spot token if available.
6. Hyperliquid agent-address negative control.
7. Lighter account with spot LIT and locked LIT.
8. Lighter LIT staking pool participant.
9. Lighter non-LIT yield/public-pool participant.
10. Lighter pending-unstake position if a public example can be found.
11. Multi-venue wallet with EVM plus Hyperliquid or Lighter.

Public addresses are test fixtures only. They are not seeded into the user's local database.

## Completion gates

- Failure hypotheses above are either covered by a passing test or explicitly documented as unsupported with a visible app warning.
- Focused portfolio suite and full repository suite pass.
- JavaScript parses and the local app passes desktop/mobile smoke interaction checks.
- Live validation report records raw API timestamps, source fields, app totals, reference totals, absolute/percentage differences, and explanation for every non-trivial difference.
- No public validation wallet or user wallet is committed as runtime state.

## Live discovery status (2026-07-13)

Completed:

- Hyperliquid standard, unified, and portfolio-margin modes.
- Hyperliquid positive and negative unrealized PnL.
- Hyperliquid non-HYPE spot tokens priced through `spotMeta` indexes.
- Hyperliquid multi-DEX standard balances.
- Hyperliquid vault equity and delegated HYPE.
- Hyperliquid agent-wallet negative control.
- Lighter public LIT staking wallet with spot, locked, and pool-share fields checked.
- Lighter non-LIT LLP deposit matched official pool-share value within $0.002.
- Large Lighter operator counted only pro-rata operator equity across 195 pools.
- Representative EVM wallet validated on Ethereum, Base, Polygon, BNB,
  HyperEVM, Arbitrum, and Avalanche.
- Public-RPC silent truncation, native pseudo-token duplication, and missing core
  stable deployments are fixed and regression-tested.
- Refresh-all is load-tested with 100 imported wallets and bounded concurrency.

Public addresses and reconciliations are in `PORTFOLIO_LIVE_VALIDATION.md`. A
currently active Lighter pending unstake is the only open live-fixture item;
historical events and payload shape are documented and fixture-tested.

