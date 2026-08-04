# V2 Architecture Rollout Acceptance Checklist

This is a read-only planning gate. It is not a deployment script and does not
authorize a production migration or consumer cutover.

## Required evidence bundle

| Gate | Evidence required | Pass condition |
| --- | --- | --- |
| Snapshot | Per-account ledger backup, central catalog backup, source hash | Restore succeeds into an isolated temporary directory |
| Integrity | SQLite `PRAGMA integrity_check`, schema manifest, row counts | Every database reports `ok`; counts are recorded |
| Identity | Account IDs, venue namespaces, label history, UID collision report | No display-name-derived identities or UID collisions |
| Cutoff | `context_start`, `report_start=2026-06-01T00:00:00Z`, `report_end`, timezone | Pre-cutoff context cannot create report PnL, trades, or alerts |
| Projection | Accounting version, run mode, input snapshot hash, projection hash | Replaying the same snapshot produces the same hash |
| Accounting | Fill-level PnL, lifecycle totals, holding duration, fees/funding | Legacy/V2 values match or every difference is classified |
| Completeness | Mark/candle freshness and lifecycle-feature basis | Missing inputs produce `incomplete`, never fabricated metrics |
| Alerts | Notification-outbox before/after counts and run mode | BACKFILL/REPAIR/SHADOW create zero alert rows |
| Journal | External lifecycle/execution UID links and annotation preservation | Rebuild/re-key does not orphan notes or reasons |
| Runtime | Read-only Dashboard and Journal/API health checks | All required endpoints return HTTP 200 |
| Rollback | Prior source hash, projection selection, service state | Prior read path can be restored without touching raw fills |

## Comparison dimensions

Shadow evidence must be queryable by:

- account;
- market/symbol;
- UTC day;
- realization UID;
- lifecycle UID or explicit lineage mapping;
- report window.

Every difference is classified as `MATCH`, `EXPECTED_IDENTITY_REKEY`,
`EXPECTED_CUTOFF_POLICY`, `EXPECTED_PNL_BASIS`, or `UNEXPLAINED`.

## Cutover rule

No consumer flag changes until:

1. all required evidence exists;
2. all `UNEXPLAINED` differences are zero;
3. replay hashes are stable;
4. non-live alert deltas are zero;
5. restore and rollback have been rehearsed;
6. the owner explicitly approves that individual consumer.

Consumers are enabled one at a time in this order:

1. internal read-only comparison;
2. Dashboard analytics;
3. Telegram period reports;
4. daily recap;
5. Journal synchronization;
6. accounting write-path replacement.

