-- Lifecycle holding-time migration (applied idempotently by sqlite_store.py).
-- Duration is stored as integer milliseconds for analysis; open lifecycles stay
-- NULL and are evaluated against the current time by read APIs.
CREATE INDEX IF NOT EXISTS ix_v2_lifecycles_holding
ON v2_lifecycles(status, holding_duration_ms, closed_at, lifecycle_uid);
CREATE INDEX IF NOT EXISTS ix_v2_lifecycles_analysis
ON v2_lifecycles(account_id, market_key, direction, status, closed_at);
