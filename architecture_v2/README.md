# Crypto Scientist Architecture V2

This directory is the isolated implementation workspace for the architecture
defined in [`../ARCHITECTURE_BLUEPRINT.md`](../ARCHITECTURE_BLUEPRINT.md).

The concrete incremental migration map is documented in
[`docs/CURRENT_TO_V2_MIGRATION.md`](docs/CURRENT_TO_V2_MIGRATION.md). V2 is a
strangler migration, not a full-code rewrite.

The researched PnL-card execution chart extension is documented in
[`docs/EXECUTION_CHART_DESIGN.md`](docs/EXECUTION_CHART_DESIGN.md).

The existing production application remains outside this directory and is not
modified or replaced until V2 passes its migration, shadow-comparison, and
deployment gates.

## Implementation status

The first local implementation slice is complete and tested:

- immutable, account-scoped execution identities and Decimal/time validation;
- a pure per-account lifecycle projector for long, short, scale, partial close,
  full close, explicit hedge sides, and reversal;
- fill-time `Realization` records and lifecycle-close trade outcomes;
- portfolio composition from independent account projections;
- one `AccountingPeriodReport` handler with `[start, end)` boundaries;
- additive SQLite tables for accounts, portfolios, memberships, executions,
  realizations, lifecycles, checkpoints, and an integration outbox;
- atomic append-and-reproject ingestion with replay idempotency and late-fill
  rebuilding;
- a structural adapter for the current runtime trade shape;
- a shared `TradeChartSpec`, lifecycle-aware marker batching, interval
  selection, and deterministic Pillow PNG rendering;
- a projection invariant evaluator and read-only legacy/V2 metric comparator.

This code has no production import or write path. Commit `fb229f7` has been
installed on the VM as isolated source, where its 54 tests pass. It has not
been enabled for any consumer and did not migrate a production database. See
[`docs/IMPLEMENTATION_STATUS.md`](docs/IMPLEMENTATION_STATUS.md) for the module
map, commands, evidence, and next gates.

## Directory contract

```text
architecture_v2/
  domain/          pure identities, executions, accounting, lifecycles, reports
  application/     ingestion, reconciliation, projection, and query use cases
  adapters/        exchange normalization contracts and adapters
  infrastructure/  repositories, migrations, outbox, and supervision
  tracker/         Tracker API and presentation integration
  journal/         Journal projection consumer and annotation boundary
  migrations/      additive, reversible schema migrations
  fixtures/        anonymized historical accounting fixtures
  tests/           V2 unit, integration, parity, and human-like evaluations
  docs/            decisions, comparisons, rollout evidence, and runbooks
```

## Isolation rules

1. V2 never writes to production databases during development or shadow runs.
2. Production snapshots are opened read-only and copied into temporary test
   databases when mutation is required.
3. V2 modules do not import `src.dashboard` or UI modules.
4. Existing production modules do not import V2 until an explicit cutover.
5. Every comparison records its input snapshot hash and accounting version.
6. No deployment occurs without a fresh VM backup and rollback package.

## Migration order

1. **Complete locally:** pure domain contracts and accounting invariants.
2. **Complete locally:** one-account projection and portfolio composition.
3. **Complete locally:** period report projector and additive V2 storage.
4. **Complete locally:** runtime-edge adapter, chart contract, PNG renderer,
   projection evaluator, and generic shadow comparator.
5. **Next:** normalize anonymized/current snapshots into V2 fixtures and run
   account/symbol/day/lifecycle shadow comparisons.
6. **Later:** add exchange candle providers, artifact cache, and notification
   delivery-group integration.
7. **After gates pass:** cut over read consumers individually, with rollback.

## Test

From the repository root:

```powershell
& ".\.venv\Scripts\python.exe" -m pytest -q architecture_v2\tests `
  --basetemp data\pytest-tmp\v2
```

The normal repository command also discovers this directory:

```powershell
& ".\.venv\Scripts\python.exe" -m pytest -q `
  --basetemp data\pytest-tmp\full
```
