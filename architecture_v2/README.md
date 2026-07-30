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

## Initial implementation order

1. Pure domain contracts and accounting invariants.
2. One-account projection.
3. Portfolio composition.
4. Period report projector.
5. Additive storage and migration.
6. Shadow comparison against current production behavior.
7. Consumer adapters and controlled cutover.
