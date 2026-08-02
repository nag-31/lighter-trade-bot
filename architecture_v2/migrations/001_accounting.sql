PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS v2_schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS v2_accounts (
    account_id TEXT PRIMARY KEY,
    exchange TEXT NOT NULL,
    display_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS v2_portfolios (
    portfolio_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS v2_portfolio_memberships (
    portfolio_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    included INTEGER NOT NULL CHECK (included IN (0, 1)),
    active_from TEXT NOT NULL,
    active_until TEXT,
    PRIMARY KEY (portfolio_id, account_id),
    FOREIGN KEY (portfolio_id) REFERENCES v2_portfolios(portfolio_id),
    FOREIGN KEY (account_id) REFERENCES v2_accounts(account_id)
);

CREATE TABLE IF NOT EXISTS v2_executions (
    execution_uid TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    exchange TEXT NOT NULL,
    market_key TEXT NOT NULL,
    position_side TEXT NOT NULL,
    native_trade_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity TEXT NOT NULL,
    price TEXT NOT NULL,
    fee TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    FOREIGN KEY (account_id) REFERENCES v2_accounts(account_id)
);

CREATE INDEX IF NOT EXISTS ix_v2_executions_account_time
ON v2_executions(account_id, occurred_at, execution_uid);

CREATE TABLE IF NOT EXISTS v2_realizations (
    realization_uid TEXT PRIMARY KEY,
    execution_uid TEXT NOT NULL,
    lifecycle_uid TEXT NOT NULL,
    account_id TEXT NOT NULL,
    market_key TEXT NOT NULL,
    position_side TEXT NOT NULL,
    direction TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    quantity TEXT NOT NULL,
    entry_price TEXT NOT NULL,
    exit_price TEXT NOT NULL,
    gross_pnl TEXT NOT NULL,
    fees TEXT NOT NULL,
    funding TEXT NOT NULL,
    net_pnl TEXT NOT NULL,
    kind TEXT NOT NULL,
    accounting_version TEXT NOT NULL,
    FOREIGN KEY (execution_uid) REFERENCES v2_executions(execution_uid),
    FOREIGN KEY (account_id) REFERENCES v2_accounts(account_id)
);

CREATE INDEX IF NOT EXISTS ix_v2_realizations_account_time
ON v2_realizations(account_id, occurred_at, realization_uid);

CREATE TABLE IF NOT EXISTS v2_lifecycles (
    lifecycle_uid TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    position_key TEXT NOT NULL,
    market_key TEXT NOT NULL,
    position_side TEXT NOT NULL,
    direction TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    holding_duration_ms INTEGER,
    holding_duration_basis TEXT NOT NULL DEFAULT 'unavailable',
    status TEXT NOT NULL,
    entry_vwap TEXT NOT NULL,
    exit_vwap TEXT,
    max_quantity TEXT NOT NULL,
    closed_quantity TEXT NOT NULL,
    gross_pnl TEXT NOT NULL,
    fees TEXT NOT NULL,
    funding TEXT NOT NULL,
    realized_pnl TEXT NOT NULL,
    execution_uids_json TEXT NOT NULL,
    realization_uids_json TEXT NOT NULL,
    accounting_version TEXT NOT NULL,
    FOREIGN KEY (account_id) REFERENCES v2_accounts(account_id)
);

CREATE INDEX IF NOT EXISTS ix_v2_lifecycles_account_close
ON v2_lifecycles(account_id, closed_at, lifecycle_uid);

CREATE TABLE IF NOT EXISTS v2_projection_checkpoints (
    account_id TEXT PRIMARY KEY,
    accounting_version TEXT NOT NULL,
    last_execution_at TEXT NOT NULL,
    last_execution_uid TEXT NOT NULL,
    projected_at TEXT NOT NULL,
    FOREIGN KEY (account_id) REFERENCES v2_accounts(account_id)
);

CREATE TABLE IF NOT EXISTS v2_integration_outbox (
    sequence_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_uid TEXT NOT NULL UNIQUE,
    topic TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS ix_v2_outbox_pending
ON v2_integration_outbox(delivered_at, sequence_id);

INSERT INTO v2_schema_meta(key, value)
VALUES ('schema_version', '1')
ON CONFLICT(key) DO UPDATE SET value=excluded.value;
