# Master Agent Log

Precise handoff log for agents working on Crypto Scientist.

## Current status - 2026-08-04

- Chart integration and V2 source are deployed on the VM at commit
  `2bf745a6b23a52f4357e7ee8c07dc5c335767c8b`.
- Four production services were restarted and verified healthy:
  `lighterbot.service`, `command-center.service`, `apps-hub.service`, and
  `trade-journal.service`.
- Prior inert VM V2 source verification: 54 passed.
- Latest local V2 suite: 64 passed; full repository suite: 1001 passed, with 294 pre-existing aiohttp warnings.
- Full-close PnL cards now send a Telegram album containing the PnL card and an
  execution-only BUY/SELL chart.
- Production remains on the prior inert V2 source; no database migrations, V2 alert activation, or consumer cutovers have been performed.

## Architecture V2 boundary - 2026-08-04

- Implemented the isolated V2 account/catalog boundary in commits `b27eed0` through `57739f7`.
- Added immutable per-account ledger storage, central account state/label history,
  the fixed `2026-06-01T00:00:00Z` report cutoff, LIVE/BACKFILL/REPAIR/SHADOW
  run policy, deterministic projection hashes, persisted shadow comparisons,
  rollout gates, and read-only Dashboard/Journal adapters.
- Initialized local `data/catalog.db` from the existing account-ledger metadata.
  It records `HL` -> `HL Swing Wallet` label history and keeps `My NK pool`
  ingestion/alerts/portfolio disabled while retaining historical visibility.
- Catalog integrity check: `ok`; four account records and five label-history rows.
- Raw account ledgers, events, Journal, portfolio, and runtime snapshots were not
  rewritten. No production migration, deploy, alert activation, or consumer cutover
  occurred.
- Verification: V2 **64 passed**; repository **1001 passed** with 294 existing
  aiohttp `NotAppKeyWarning` warnings.

## Deployment evidence

- Latest backup: `/home/ADMIN/apps/deploy-backups/chart-integration-20260802T081021Z`
- Database integrity: `ok` for events, command-center, and trade-journal DBs.
- Health endpoints: HTTP 200 for all four application endpoints.
- Chart import smoke check: `CHART_IMPORT=ok`.
- Candle provider is not connected yet; charts are execution-only for now.

## Rules for future agents

- Update this file after every meaningful code, test, commit, or deployment step.
- Record exact commit IDs, service names, test counts, backup paths, and known
  limitations.
- Never claim a feature is live when it is only implemented locally or deployed
  as inert source.
- Keep secrets, wallet addresses, and Telegram tokens out of this file.

## Change log

### 2026-08-03 - source rename and NK pool removal deployed

- Deployed the tracked `config.yaml` from commit `f030f04` to the GCP VM.
- Renamed `hl-main` to `HL Swing Wallet` without changing its stable ID.
- Removed the `lighter-main` / `My NK pool` source from the active config; its
  existing database history was not deleted.
- Backup: `/home/ADMIN/backups/config-rename-20260803T182348Z`.
- Restarted `lighterbot.service`, `command-center.service`,
  `trade-journal.service`, and `apps-hub.service`; all are active.
- Post-restart health checks: dashboard, Command Center, Journal, and Hub all
  returned HTTP 200.

### 2026-08-02 - chart integration implemented

- Added `src/execution_chart.py`, a read-only legacy-to-V2 chart adapter.
- Updated `src/dashboard.py` to send a Telegram media group containing the PnL
  card and execution chart, with card-only fallback.
- Added `tests/test_execution_chart.py`.
- Focused chart/alert tests: 116 passed.
- Full repository suite: 968 passed.

### 2026-08-02 - chart integration deployed

- Pushed commit `2bf745a6b23a52f4357e7ee8c07dc5c335767c8b`.
- Deployed `src/dashboard.py`, `src/execution_chart.py`, and both master docs.
- Restarted all four application services.
- All four health endpoints returned HTTP 200.
- Database migrations: none; post-deployment integrity: `ok` for all three DBs.

### 2026-08-04 - review handoff added

- Added `AGENT_CODE_REVIEW_INSTRUCTIONS.md`.
- The file explains the system, recent chart work, review questions, test
  commands, safety boundaries, and the required bug-report format.
