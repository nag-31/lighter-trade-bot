# Crypto Scientist Project Ledger

Last updated: 2026-07-31 (Asia/Calcutta)

## Purpose

This is the living reference for what the owner requested, why architectural
decisions were made, what changed, how it was verified, and what is deployed.
Update it whenever scope, architecture, data, tests, or production state changes.

## Active goal

Build one VM-authoritative Crypto Scientist system with separate applications:

1. **Trade Journal** — reconstructed position lifecycles, reasons, notes,
   execution review, live/realized PnL, and journal analytics.
2. **Trade Tracker** — live positions, orders, account/address filters, alerts,
   and portfolio PnL. It remains a standalone runtime.
3. **TVL Monitor** — TVL-drop detection and review, outside the Command Center
   and outside the journal database.
4. **App Hub** — access directory and service-health surface only. It links to
   each app; it does not merge their runtimes or databases.

The VM is production truth. Local databases are explicit snapshots for
development/auditing, never an independent production state.

## Current design checkpoint

The owner approved starting the latest architecture implementation and required
tests to be written throughout. The target system, diagrams, module boundaries,
accounting invariants, storage contracts, API contracts, migration, test
strategy, and rollout gates remain defined in `ARCHITECTURE_BLUEPRINT.md`.

The first V2 foundation is now implemented under `architecture_v2/`: immutable
execution identities, Decimal/time validation, per-account lifecycle
projection, portfolio composition, one period report, additive SQLite storage,
memberships, checkpoints, outbox, current-runtime adaptation, shared trade-chart
specification, deterministic PNG rendering, projection invariants, and a
read-only shadow metric comparator. Details are recorded in
`architecture_v2/docs/IMPLEMENTATION_STATUS.md`.

All new redesign implementation is isolated under `architecture_v2/`. Existing
production modules remain outside the V2 dependency graph until shadow
comparison succeeds and the owner approves a cutover.

The architecture review concluded that a full rewrite is unnecessary and too
risky. V2 will use an incremental strangler migration: reuse exchange clients,
source loading/supervision, UI, Telegram transport, health, and unrelated apps;
replace only accounting ownership and consumer calculations through explicit
seams. The detailed classification and cutover order live in
`architecture_v2/docs/CURRENT_TO_V2_MIGRATION.md`.

Execution-chart research and the proposed candle-provider, marker, rendering,
Telegram album, Journal interaction, privacy, idempotency, and verification
contracts are recorded in
`architecture_v2/docs/EXECUTION_CHART_DESIGN.md`. The shared chart contract,
batching/interval rules, and static renderer are implemented locally; candle
providers, artifact delivery, interactive Journal integration, and production
enablement remain pending.

## Current architecture decision

| Concern | Production owner | Local behavior |
| --- | --- | --- |
| Live trades, positions, alerts | VM `lighterbot.service` | Read-only snapshot on explicit pull |
| Trade lifecycle facts | VM Trade Journal ingestion | Rebuilt from VM event snapshot for tests |
| Journal reasons and decisions | VM `trading_journal.db` | Snapshot on explicit pull |
| TVL signals and outcomes | Separate TVL Monitor database/service | Snapshot on explicit pull |
| Application code | Repository, then deployed to VM | Local working tree is development source |
| App discovery | Crypto Scientist App Hub | Links and health only |

Page loads must be read-only. Background workers and explicit **Sync now**
actions update production data. Opening or refreshing a page must not trigger a
workspace scan or lifecycle rebuild.

## Request log

### Product and access

- Build a useful Crypto Scientist app hub and deploy it to the VM.
- Protect private apps with the shared password.
- Include access to the Private Portfolio Tracker.
- Keep Trade Tracker accessible as its own app; do not merge it into another app.
- Use clearer semantic colors and highlights for long/short, profit/loss,
  warnings, freshness, and selected rows.

### Trade journal

- Add a populated journal with predefined and custom reasons for taking a trade.
- Allow reasons to be edited after the journal entry is created.
- Group rapid fills into one position lifecycle; separate fills minutes apart
  can represent profit-taking or a distinct execution batch.
- Treat partial exits as part of the whole trade and show whole-position PnL.
- Correctly reconstruct reversals and distinguish the closing leg from the new
  opening leg.
- Open trades must show **ACTIVE**, not **CLOSED**, and show live unrealized PnL.
- Add search, useful filters, and click sorting for every data column.
- Separate journal storage and UI from TVL drops and other signal functions.

### PnL and accounting

- Use one truth base per account/address and one portfolio aggregator.
- Adding or removing an account must update aggregate output without rewriting
  every calculation path.
- Back up production data before architectural or accounting changes.
- Keep baseline data for before/after comparison.
- Show aggregate live PnL in the address/account-filtered Trade Tracker.
- Keep lifecycle review and time-bucket accounting separate:
  - the Journal may show the full lifecycle PnL across every partial exit;
  - daily/weekly/monthly PnL must book each realization fill exactly once at
    that fill's timestamp;
  - the final dust close must never move earlier realized profit into the
    current period or create an additional lifecycle-total ledger cashflow.

### XYZ:MU time-attribution audit

- The closed `SHORT XYZ:MU` lifecycle correctly presents its full-position
  result in the Journal.
- The production snapshot contains individual realization rows at their actual
  timestamps (June 29, July 2, and July 28), rather than a second ledger row for
  the lifecycle total.
- The close-card override is presentation-only; the stored close row retains
  that fill's own realized PnL.
- The daily recap already filters realization rows before lifecycle grouping.
- Follow-up required: Telegram `/today`, `/weekly`, `/pnl 7d`, and any matching
  dashboard time-window projector currently filter completed lifecycle rows
  after grouping. They must use a shared fill-time realization projector so old
  scale-out PnL cannot bleed into the final-close period.

### Alerts and Telegram

- Stop alert spam and historical backfill alerts.
- Correct whole-trade PnL cards and remove the Enkapital website from messages.
- Keep discussion-channel traffic selective.
- Add community/private bot commands.
- Improve Telegram readability with strong hierarchy and long/short cues.
- Pair lifecycle PnL cards with a trade-specific execution chart showing
  candles, buys, sells, entries, scale-ins, partial exits, reversals, and final
  close fills. Static Telegram and interactive Journal charts must use the same
  traceable chart specification.

### Project operations

- The VM is the single production state; local state must be refreshed from it.
- Keep this ledger updated so the project can be explained or handed to another
  person without reconstructing history from chat.
- Implement the approved modular architecture inside `architecture_v2/`, not as
  a full-code rewrite.
- Write focused tests with each implementation slice and include them in the
  default repository test command.

## Why the Command Center used to sync on every page load

`GET /api/bootstrap` called the full workspace ingestor before returning page
data. Therefore every page load or refresh scanned sources and rebuilt derived
state. That coupling caused latency, repeated writes, and confusing freshness
behavior.

Correction implemented locally:

- `/api/bootstrap` becomes a read-only snapshot endpoint.
- The service keeps its scheduled background refresh.
- **Sync now** remains an explicit write action.
- The UI shows last successful refresh, current worker state, and stale/error
  status so freshness is visible without forcing a refresh.

## Lifecycle correction completed locally

The lifecycle builder now:

- gives Hyperliquid `dir` plus signed `start_position` precedence over corrupt
  replayed position cache state;
- distinguishes scale-ins, partial exits, full closes, and both legs of a
  reversal;
- orders same-timestamp close bursts by the position-size chain;
- derives missing entry economics from authoritative realized PnL;
- splits lifecycles when execution side crosses a position boundary;
- preserves journal links during lifecycle re-keying by execution overlap.

Production-snapshot audit result: **134 active lifecycles, 0 side/PnL-direction
contradictions**.

The reported `XYZ:SNDK` record reconstructs as a closed **SHORT** lifecycle,
followed by a separate **LONG** reversal lifecycle.

## Implemented and deployed

- Corrected lifecycle reconstruction and regression coverage.
- Added effective ACTIVE/CLOSED journal status from linked lifecycle truth.
- Added live unrealized PnL to active journal rows.
- Added journal-entry editing, including selected reasons.
- Added status, direction, linkage, and text filters.
- Kept every displayed journal data column click-sortable.
- Added semantic long/short colors and state highlights.
- Added filter-aware aggregate live PnL, notional, position count, and account
  count to Trade Tracker.
- Routed new journal writes to a physically separate `trading_journal.db`;
  frozen legacy tables remain only as a one-time migration source.
- Added an explicit pull-only `scripts/sync_vm_state.ps1` workflow with
  integrity checks, local backup, exact hashes, and `.vm-state/current.json`
  provenance. Local `events.db` and `command_center.db` now match the explicit
  pre-deployment VM snapshots.
- Removed ingestion from both page-load bootstrap endpoints.
- Added a standalone Trade Journal runtime on port 8811 with a dedicated UI,
  API, sync history, database, service definition, and planned
  `journal.8-231-102-153.sslip.io` URL.
- Reframed the old Command Center as Signal Research and removed the visible
  journal and TVL surfaces from it.
- Stopped copying TVL/protocol signals from `hack-alert-bot` into the research
  database; the existing port-8788 app is now identified as **TVL & Protocol
  Monitor**.
- Kept Trade Tracker as the standalone port-8080 app and added explicit hub
  access alongside Trade Journal.
- Fixed duplicate lifecycle cards caused by multiple journal links to one
  lifecycle.
- Missing live marks now display **Awaiting mark** instead of an invented
  `$0.00`.
- Deployed the backup-first, rollback-capable release to the production VM.
- Made Trade Journal the sole writer for lifecycle/position ingestion. Signal
  Research now writes only market signals and outcomes; its verified manual
  sync reports `trades: 0` and `positions: 0`.

## Verification evidence

- V2 focused suite: **54 passed** on 2026-07-30.
- Full repository regression after adding V2 to default discovery:
  **967 passed** on Python 3.12 on 2026-07-30. The run emitted 294 existing
  aiohttp `NotAppKeyWarning` warnings from portfolio-app tests and no failures.
- Full repository suite after the final single-writer boundary: **913 passed**.
- Pre-Git-publication verification on Python 3.12 with the documented
  development dependencies: **913 passed** on 2026-07-30. Windows verification
  uses `--basetemp data/pytest-tmp` so tests do not depend on an inaccessible
  user temp directory.
- Focused lifecycle/journal/app-boundary suite: **22 passed**.
- Production event snapshot lifecycle audit: **0 contradictions**.
- JavaScript syntax check: passed.
- Browser QA against a VM-snapshot copy: **134 unique lifecycle cards**, active
  state and unknown-mark handling correct, 21 editable journal rows, selected
  reasons restored, and click sorting functional.

## Deployment state

Architecture V2 commit `fb229f7325bb7afdb09ad756d65c4a8ecc916608`
is installed on the VM as isolated source from branch
`codex/architecture-v2`. It is not connected to a production database, enabled
as a shadow writer, or selected by any dashboard, Telegram, recap, or Journal
consumer.

V2 source deployment evidence:

- backup:
  `/home/ADMIN/apps/deploy-backups/architecture-v2-20260730T185018Z`;
- follow-up documentation backup:
  `/home/ADMIN/apps/deploy-backups/architecture-v2-docs-20260730T185018Z`;
- backup size: **3,976,194 bytes**;
- all three consistent database snapshots: integrity `ok`;
- V2 tests on VM/Linux/Python 3.12: **54 passed**;
- services restarted: **0**;
- database migrations: **0**;
- post-deployment production `v2_*` tables: **0**;
- post-deployment V2 files in `data/`: **0**;
- all four services active and all four local health endpoints HTTP 200.

The complete source-only deployment and rollback record is
`architecture_v2/docs/VM_SOURCE_DEPLOYMENT.md`.

Production deployment completed successfully on 2026-07-30.

Production applications:

- App Hub: `https://hub.8-231-102-153.sslip.io/`
- Signal Research: `https://command.8-231-102-153.sslip.io/`
- Trade Journal: `https://journal.8-231-102-153.sslip.io/`
- Trade Tracker: `https://dashboard.8-231-102-153.sslip.io/`
- TVL & Protocol Monitor: `https://hack.8-231-102-153.sslip.io/`

Trade Journal and Signal Research use the existing `nag` basic-auth account and
shared password. The journal TLS certificate includes both command and journal
hosts and expires on 2026-10-28; Certbot renewal is scheduled.

Production validation:

- `lighterbot.service`, `command-center.service`, `apps-hub.service`, and
  `trade-journal.service`: **active**
- journal decisions/reasons preserved: **21 / 38**
- active lifecycles: **134**
- duplicated TVL/hack signals in Signal Research: **0**
- journal page-load ingestion: **0 new sync runs**
- Signal Research trade/position writes: **0 / 0**

Rollback snapshots:

- `/home/ADMIN/apps/deploy-backups/app-separation-20260730T094157Z`
- `/home/ADMIN/apps/deploy-backups/journal-single-writer-20260730T094443Z`

Deployment files:

- `.deploy/crypto-scientist-app-separation.tar.gz`
- `.deploy/deploy_app_separation.sh`

The deployment script stops relevant services, backs up code, service
definitions, nginx, and the full data directory; migrates journal data; rebuilds
lifecycles; installs the service and HTTPS route; validates all four app
boundaries and read-only page loads; and rolls back automatically on failure.

Post-deployment VM-to-local pull completed at `20260730T094630Z`. Local
`events.db`, `command_center.db`, and `trading_journal.db` are integrity-checked
snapshots of production. Provenance and exact hashes are stored in
`.vm-state/current.json`; the immediately previous local state is stored in
`.local-backups/vm-sync-20260730T094630Z`.

## Git publication workflow

The owner requested that the complete safe source state be pushed to Git and
that every future change explain what changed, how the system works, and how to
run it. A root `README.md` now serves as the canonical operator guide and
contains an explicit documentation rule for every functional commit or
deployment.

Git excludes credentials, databases, generated cards/logs, `.deploy/`,
`.local-backups/`, and `.vm-state/`. Source, tests, architecture documents,
service definitions, and safe operational scripts are included. Before this
publication, the staged content is reviewed for credential patterns and the
full 913-test suite must pass.

## Completed implementation plan

1. Compared VM code/database manifests and pulled explicit snapshots.
2. Removed ingestion from page-load bootstrap.
3. Formalized VM-to-local snapshots and provenance metadata.
4. Extracted Trade Journal into its own runtime, URL, and database.
5. Kept Trade Tracker standalone and exposed it through App Hub.
6. Removed TVL copies from Signal Research; TVL Monitor remains its own runtime
   and database.
7. Validated migrations, APIs, UI, sorting, editing, lifecycle uniqueness, and
   cross-app links.
8. Backed up, deployed, validated production, and pulled production state back
   to local.

## Open questions resolved by default

- **Production truth:** VM.
- **Local database writes:** development/test only; never silently pushed.
- **Page refresh behavior:** read only.
- **Cross-app integration:** stable links and health metadata, not shared UI
  runtimes.
- **Journal-to-trade relationship:** stable lifecycle identity/execution overlap,
  not fragile row IDs.
