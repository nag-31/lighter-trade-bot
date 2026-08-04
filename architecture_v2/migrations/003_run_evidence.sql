CREATE TABLE IF NOT EXISTS v2_projection_runs (
    run_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('LIVE', 'BACKFILL', 'REPAIR', 'SHADOW')),
    accounting_version TEXT NOT NULL,
    context_start TEXT,
    report_start TEXT NOT NULL,
    report_end TEXT,
    as_of TEXT,
    timezone TEXT NOT NULL,
    input_snapshot_hash TEXT NOT NULL,
    projection_hash TEXT NOT NULL,
    rows_read INTEGER NOT NULL,
    rows_written INTEGER NOT NULL,
    alerts_created INTEGER NOT NULL CHECK (alerts_created = 0 OR mode = 'LIVE'),
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_v2_projection_runs_time
ON v2_projection_runs(account_id, created_at, run_id);

CREATE TABLE IF NOT EXISTS v2_shadow_comparisons (
    comparison_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    dimension TEXT NOT NULL,
    subject_uid TEXT NOT NULL,
    classification TEXT NOT NULL CHECK (
        classification IN (
            'MATCH', 'EXPECTED_IDENTITY_REKEY', 'EXPECTED_CUTOFF_POLICY',
            'EXPECTED_PNL_BASIS', 'UNEXPLAINED'
        )
    ),
    legacy_value TEXT,
    v2_value TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES v2_projection_runs(run_id)
);

CREATE INDEX IF NOT EXISTS ix_v2_shadow_comparisons_run
ON v2_shadow_comparisons(run_id, classification, account_id, dimension);
