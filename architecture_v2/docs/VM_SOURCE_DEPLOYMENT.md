# Architecture V2 VM Source Deployment

Status: source installed and verified; runtime activation intentionally disabled

Deployment date: 2026-07-31 IST  
VM: `project-55b8aafe-d086-47bd-8dd / asia-south1-a / crypto-apps-vm`  
Branch: `codex/architecture-v2`  
Commit: `fb229f7325bb7afdb09ad756d65c4a8ecc916608`

## Purpose

The deployment places the isolated V2 implementation on the production VM for
inspection and VM-parity testing. It is not an accounting cutover. Existing
services do not import V2, and production databases do not contain the V2
schema.

## Backup

The deployment created this server-side rollback directory before replacing
source:

```text
/home/ADMIN/apps/deploy-backups/architecture-v2-20260730T185018Z
```

After the evidence was written to the tracked documentation, those documentation
files were synchronized to the VM with their previous copies backed up at:

```text
/home/ADMIN/apps/deploy-backups/architecture-v2-docs-20260730T185018Z
```

Contents:

- `code-before.tar.gz` — exact source targets replaced by the deployment;
- `existing-targets.txt` — paths that existed before deployment;
- `databases/events.db` — consistent SQLite backup;
- `databases/command_center.db` — consistent SQLite backup;
- `databases/trading_journal.db` — consistent SQLite backup;
- `database-manifest.json` — byte counts, SHA-256 hashes, and integrity results;
- `services-before.txt` — service state before source replacement;
- `deployed-source.sha256` — hashes of the installed V2 core and documents;
- `postdeploy-database-integrity.json` — post-deployment integrity evidence.

Backup database hashes:

| Database | Bytes | SHA-256 |
| --- | ---: | --- |
| `events.db` | 1,626,112 | `a80c87243cb1334a62d99f9cc9494fa2cc8c939e11c785112454d7f19c4fa924` |
| `command_center.db` | 1,585,152 | `c2762d6283551301b95da7212756614448b00db3a1fff25ab3620df7ad568217` |
| `trading_journal.db` | 757,760 | `64b4b8ebe7608f8b4be20a880574037f1351e1784cd959ff8cd513cd4169708e` |

Every backup returned `PRAGMA integrity_check = ok`.

## Deployment procedure

1. Confirm all four application services are active.
2. Record production database sizes, hashes, and integrity.
3. Build a Git archive from the exact commit, limited to `architecture_v2/`
   and its tracked documentation/configuration.
4. Upload the archive and backup-first installer to `/tmp`.
5. Validate the remote archive hash, contents, and installer syntax.
6. Back up the replaced source and all three databases.
7. Install the isolated source without restarting services.
8. Compile V2 and run a fill-time/lifecycle-close smoke scenario.
9. Run all V2 tests on the VM.
10. Recheck services, health endpoints, database integrity, and schema.

## Verification result

- V2 tests: **54 passed** on Linux/Python 3.12.
- `lighterbot.service`: active.
- `command-center.service`: active.
- `apps-hub.service`: active.
- `trade-journal.service`: active.
- Tracker, Signal Research, Journal, and App Hub local health endpoints: HTTP
  200.
- Production database integrity: `ok` for all three databases.
- Production `v2_*` tables: zero.
- V2 files under production `data/`: zero.
- Services restarted: zero.
- Database migrations: zero.

## Rollback scope

Rollback restores only the source targets changed by this deployment:

```text
architecture_v2/
README.md
PROJECT_LEDGER.md
ARCHITECTURE_BLUEPRINT.md
pytest.ini
.gitignore
```

No database rollback is required because the deployment did not write or
migrate production databases. The database snapshots are retained as
independent evidence and emergency recovery material.

Before a rollback, resolve and verify that the application directory is exactly
`/home/ADMIN/apps/lighter-trade-bot`. Remove only the six explicit targets
above, then extract `code-before.tar.gz` into that application directory. No
service restart is necessary because no running service imports V2.

## Next gate

Do not enable V2 with a dashboard, Telegram, recap, Journal, or write-path
feature flag until snapshot-backed shadow comparisons have zero unexplained
differences and the owner explicitly approves that consumer’s cutover.
