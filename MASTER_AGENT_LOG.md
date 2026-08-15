# Master Agent Log

Precise handoff log for agents working on Crypto Scientist.

## Current status - 2026-08-04

- Chart integration and V2 source are deployed on the VM at commit
  `2bf745a6b23a52f4357e7ee8c07dc5c335767c8b`.
- Four production services were restarted and verified healthy:
  `lighterbot.service`, `command-center.service`, `apps-hub.service`, and
  `trade-journal.service`.
- Prior inert VM V2 source verification: 54 passed.
- Latest local V2 suite: 69 passed; full repository suite: 1006 passed, with 294 pre-existing aiohttp warnings.
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
- Catalog integrity check: `ok`; schema version 2, four account records, five label-history rows, and 16 initial account-state audit rows.
- Raw account ledgers, events, Journal, portfolio, and runtime snapshots were not
  rewritten. No production migration, deploy, alert activation, or consumer cutover
  occurred.
- Verification: V2 **69 passed**; repository **1006 passed** with 294 existing
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

### 2026-08-04 - V2 ingestion and audit evidence hardened

- Added account-state history with actor, reason, timestamp, and old/new values.
- Added ledger-first ingestion coordination, ledger/projection drift detection,
  and projection-only repair from immutable account facts.
- Recorded LIVE/BACKFILL/REPAIR/SHADOW provenance on fill observations.
- Made projection-run manifests and shadow comparisons immutable and
  idempotent; conflicting evidence IDs are rejected instead of overwritten.
- Added five focused architecture-hardening tests.
- Verification: V2 **69 passed**; full repository **1006 passed**, with 294
  pre-existing aiohttp warnings.
- Production deployment, consumer cutover, alerts, and raw account ledgers were
  unchanged.

### 2026-08-04 - bug hunt + architecture documentation

- Reviewed `src/dashboard.py`, `execution_chart.py`, `pnl_card.py`, `db.py`,
  `stats.py`, `sources.py`, `architecture_v2/`, and the peripheral apps.
- Baseline: full suite 1006 passed.
- Fixed 3 bugs:
  1. [P1] HL privacy leak in the dashboard events table (real prices/sizes sent
     to browsers; per-row `_disp` missing). Added `_recent_event_payload()`.
  2. [P2] `closed_trades[0]` race after `await record_realization` � close card
     could be a different coin's. Now uses the returned `card_path`.
  3. [P2] TOCTOU in `record_realization` dedup � consumer + reconciler could
     both record the same fill (duplicate in-memory rows, double-counted PnL).
     Keys are now claimed synchronously before any await.
- Added 3 regression tests in `tests/test_dashboard_contract.py`.
- Verification: full repository **1009 passed**; `py_compile` clean on
  `src/dashboard.py` and `src/execution_chart.py`.
- Created `docs/architecture/`:
  - `easy/` � human-facing overview, checklist, config, privacy.
  - `detailed/` � module map, accounting rules, data flow, invariants,
    V2 status, bug-hunt report.
  - `diagrams/` � 6 Mermaid diagrams rendered to PNG (system overview, trade
    lifecycle, data storage, apps, reconcile flow, privacy flow).
- No production changes; V2 remains inert; no restart performed.

### 2026-08-11 - execution charts made optional and open-interest command added

- Added `settings.execution_chart_enabled`, defaulting to `false`. Full-close
  Telegram alerts continue to send the existing PnL card/text; entry/exit chart
  PNG generation and media attachment are skipped unless explicitly enabled.
- Added community commands `/oi` and `/openinterest`. They report gross open
  interest across all currently tracked positions, split into long/short
  notional, with position count and combined unrealized PnL. An optional source
  argument (for example `/oi HL`) applies the same source filter as `/positions`.
- Added focused regression coverage for command permissions/totals, dispatch,
  and the chart toggle/menu registration. Focused tests: **44 passed**; final
  command/dashboard menu checks: **33 passed**. Full repository suite:
  **1013 passed**, with 294 existing aiohttp `NotAppKeyWarning` warnings.
- Local source/config/tests only; no VM deployment, service restart, alert send,
  or database mutation was performed.
### 2026-08-12 - chart attachments disabled and open-interest command deployed

- Pushed commit `12de4d377da24416c48b2f0eb55ab0714e4d4982` to
  `origin/codex/architecture-v2`.
- Deployed the runtime files (`config.yaml`, `holding_time.py`,
  `src/dashboard.py`, `src/sources.py`, and `src/telegram_commands.py`) to the
  GCP VM at `/home/ADMIN/apps/lighter-trade-bot`.
- Backup-first deployment evidence:
  `/home/ADMIN/apps/deploy-backups/execution-chart-off-20260811T202234Z`.
  The three production SQLite backups passed `PRAGMA integrity_check = ok`.
- Restarted `lighterbot.service`, `command-center.service`,
  `apps-hub.service`, and `trade-journal.service`; all are active.
- Post-deploy health: tracker `8080/healthz`, Command Center `8810/health`,
  Trade Journal `8811/health`, and App Hub `8800/api/status` all returned HTTP
  200. Runtime settings reported `EXECUTION_CHART_ENABLED=False`; the `/oi`
  formatter imported successfully. Database migrations: none.
- Two earlier attempts rolled back automatically before the successful run: one
  had a verification working-directory error, and one exposed the missing
  `holding_time.py` helper. No database writes were made by those attempts.
### 2026-08-12 - current uPnL filters and dashboard breakdown deployed

- Pushed commit `4143ea6` to `origin/codex/architecture-v2`.
- Added community commands `/upnl` and `/livepnl`; both accept optional
  `long`/`short` and wallet/source tokens. `/positions`, `/oi`,
  `/openinterest`, and owner `/risk` now accept the same side/wallet filters.
- Added dashboard Direction controls (All/Long/Short) alongside multi-wallet
  selection. Current uPnL, Long uPnL, Short uPnL, notional, positions, and
  account cards now recompute from the active wallet/direction filter.
- Local verification: full repository **1014 passed**, with 294 existing
  aiohttp `NotAppKeyWarning` warnings.
- Deployed runtime commit `4143ea6` to the GCP VM and restarted
  `lighterbot.service`, `command-center.service`, `apps-hub.service`, and
  `trade-journal.service`; all are active.
- Backup-first evidence:
  `/home/ADMIN/apps/deploy-backups/execution-chart-off-20260812T154016Z`.
  All three production SQLite backups and post-deploy databases passed
  `PRAGMA integrity_check = ok`; no migrations were performed.
- Post-deploy health: tracker `8080/healthz`, Command Center `8810/health`,
  Trade Journal `8811/health`, and App Hub `8800/api/status` all returned HTTP
  200. Live dashboard HTML and deployed formatter smoke checks passed.

### 2026-08-16 - professional portfolio, App Hub link, and simplified uPnL deployed

- Deployed the professional Naga Portfolio static site and downloadable
  nine-slide pitch deck to `https://enkapital.8-231-102-153.sslip.io/`.
- Replaced the old App Hub website entry with a prominent **Naga Portfolio**
  card that links to the live site and describes the Crypto Scientist case
  study and pitch deck.
- Removed the redundant Long uPnL and Short uPnL summary cards from the Trade
  Tracker. The single Current uPnL card remains filter-aware; Direction and
  wallet filters continue to provide long/short and account-specific views.
- Focused local verification: `tests/test_dashboard_contract.py` and
  `tests/test_trade_journal.py`, **22 passed**.
- Final full repository verification: **1014 passed**, with 294 existing aiohttp
  `NotAppKeyWarning` warnings.
- Backup-first production evidence:
  `/home/ADMIN/backups/portfolio-hub-upnl-20260815T200438Z`. The previous static
  site directory remains available at
  `/home/ADMIN/apps/eNKapital/eNKapital-main/eNKapital-main.predeploy-20260815T200438Z`.
- Restarted only `enkapital-site.service`, `apps-hub.service`, and
  `lighterbot.service`; all are active. No database migration or runtime data
  replacement was performed.
- Public HTTPS verification returned HTTP 200 for the portfolio, App Hub,
  Trade Tracker, and pitch-deck download. Live HTML confirms the Naga Portfolio
  Hub link, one Current uPnL card, and absence of the Long/Short summary cards.
