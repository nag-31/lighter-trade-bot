# Trade Tracker Multi-Account Revamp Plan

Status: design plan only; implementation has not started.

## 1. Outcome

Rebuild the trade tracker so one running service can safely track any mixture of:

- Multiple Hyperliquid wallets, including default and HIP-3 DEX positions/fills.
- Multiple Lighter public pools.
- Multiple Binance Futures accounts with separate credentials and network routes.
- Any protocol being absent, disabled, misconfigured, temporarily unavailable, or permanently unsupported from the VM region.

Adding an account must become a configuration operation, not a Python code change. One broken account must never stop healthy accounts, erase positions, generate false close alerts, or crash the dashboard.

## 2. Non-negotiable behavior

1. Every configured account has a stable, explicit, non-secret `source_id`.
2. Human-readable names are display labels only and are never database identity.
3. Wallet addresses, Binance keys, and secrets never enter Git, URLs, logs, Telegram, filenames, or public health payloads.
4. Missing optional protocols do not prevent startup.
5. No configured sources is a supported runtime mode: the dashboard and health endpoints still start and explain what is missing.
6. A failed API request is not the same as an authoritative empty response.
7. Stale data may be displayed as stale; it must not be converted into a position close.
8. Trade/event deduplication is scoped by source, protocol, market namespace, and native trade identity.
9. WebSocket and REST overlap is expected and must be idempotent.
10. Every source has independent retry, circuit-breaker, cursor, cache, health, and shutdown state.
11. Restart recovery must use persisted cursors and must not replay old Telegram alerts.
12. Adding or removing an account must not alter the history of another account.

## 3. Current blockers found in the code

The present implementation is a good single-account base, but these issues must be fixed before enabling multiple accounts:

- Hyperliquid always reads one `HL_ADDRESS` environment variable.
- Binance always reads one `BINANCE_API_KEY` and `BINANCE_API_SECRET` pair.
- Lighter already supports multiple pool entries, but source lifecycle and persistence are not fully account-scoped.
- `load_sources()` raises when no valid source exists, so the dashboard cannot run in an empty/unconfigured state.
- Source names are used in database rows, health keys, audits, privacy restoration, and statistics. Duplicate names would conflate accounts.
- `closed_trades.trade_id` and the boot realization-dedup set are global integers. Trade IDs can collide between sources, DEXes, symbols, and accounts.
- Binance uses millisecond timestamps as synthetic trade IDs. Multiple fills can occur in one millisecond.
- Binance REST recovery queries only symbols with positions that are currently open. A position opened and closed while the bot was offline can be missed.
- Binance open orders are currently a no-op.
- Binance hedge mode is disabled because `PositionTracker` supports only one position per market.
- Several clients return `{}` or `[]` both for “authoritative empty” and “request failed.” The reconciler can interpret a failure as every position being closed.
- `asyncio.gather(*tasks)` has no source-level supervisor boundary. An unexpected unhandled task failure can terminate the whole service.
- Health is keyed by display name and represents a source as one status even when only one capability, such as open orders, is unavailable.
- Telegram text-hash deduplication can suppress two legitimate identical alerts from different accounts.
- Daily reconciliation filters database rows by source display name.
- Events are JSON blobs without indexed `source_id`, exchange, account, market scope, or event UID.
- Runtime configuration and tracked application configuration are mixed in one `config.yaml`.

## 4. Target configuration model

### 4.1 Tracked defaults

Keep non-secret defaults in the repository:

```yaml
settings:
  allow_empty_sources: true
  fail_startup_on_invalid_enabled_source: false
  rest_poll_seconds: 60
  reconciler_interval_seconds: 30
  source_start_concurrency: 4
  source_request_concurrency: 8
  stale_position_warning_seconds: 120
  stale_position_down_seconds: 600
  cursor_flush_seconds: 5
```

### 4.2 Runtime source file

Move account-specific source entries to an untracked runtime file, for example:

```text
/home/ADMIN/apps/lighter-trade-bot/runtime/sources.yaml
```

The systemd unit sets:

```env
LIGHTERBOT_SOURCES_FILE=/home/ADMIN/apps/lighter-trade-bot/runtime/sources.yaml
```

Example:

```yaml
sources:
  - id: hl-main
    type: hyperliquid
    name: HL Main
    enabled: true
    address_env: HL_MAIN_ADDRESS
    min_notional_usd: 1000
    exclude_symbols: [FARTCOIN]
    footer_url: https://enkapital.xyz

  - id: hl-secondary
    type: hyperliquid
    name: HL Secondary
    enabled: true
    address_env: HL_SECONDARY_ADDRESS
    min_notional_usd: 500

  - id: lighter-nk
    type: lighter
    name: My NK Pool
    enabled: true
    pool_id: 281474976684763
    ws_proxy_env: LIGHTER_NK_WS_PROXY

  - id: lighter-second
    type: lighter
    name: Second Pool
    enabled: false
    pool_id: 123456789

  - id: binance-main
    type: binance
    name: Binance Main
    enabled: true
    api_key_env: BINANCE_MAIN_API_KEY
    api_secret_env: BINANCE_MAIN_API_SECRET
    proxy_profile: binance-singapore
    position_mode: auto
    min_notional_usd: 900
```

Example secret environment:

```env
HL_MAIN_ADDRESS=0x...
HL_SECONDARY_ADDRESS=0x...
BINANCE_MAIN_API_KEY=...
BINANCE_MAIN_API_SECRET=...
LIGHTER_NK_WS_PROXY=socks5h://...
```

### 4.3 Validation rules

- `id` is required, unique, stable, lowercase, and matches `[a-z0-9][a-z0-9_-]{1,63}`.
- `name` is required for display but does not need to be unique.
- `type` must be `hyperliquid`, `lighter`, or `binance`.
- `enabled: false` creates a visible disabled health entry and performs no network calls.
- Hyperliquid requires a valid `address_env` name and a 42-character `0x` address in that environment variable.
- Lighter requires a positive integer `pool_id`; optionally allow `pool_id_env`.
- Binance requires both env references and both resolved values. One without the other is a configuration error.
- Unknown keys produce warnings in development and errors in strict validation.
- Duplicate Hyperliquid addresses, Lighter pool IDs, or Binance credential fingerprints are rejected unless an explicit `allow_duplicate_account: true` override is added for a documented use case.
- Raw addresses or API keys present directly in YAML are rejected.
- A source may override polling, alerting, exclusions, privacy, proxy profile, and footer settings.
- Validation returns all errors at once; it does not stop at the first bad source.

### 4.4 Backward compatibility

For one release:

- A legacy Hyperliquid source without `address_env` resolves `HL_ADDRESS`.
- A legacy Binance source without env references resolves `BINANCE_API_KEY` and `BINANCE_API_SECRET`.
- Legacy behavior emits a deprecation warning and a migration example.
- Legacy support is removed only after the runtime source file is deployed and verified.

## 5. Runtime architecture

### 5.1 Separate specification from runtime

Introduce:

- `SourceSpec`: validated non-secret configuration.
- `SecretResolver`: reads environment/systemd credentials without exposing values.
- `SourceFactory`: builds the correct adapter.
- `SourceRuntime`: source identity, adapter, tracker, cursors, caches, health, queues, retry state, and tasks.
- `SourceRegistry`: all configured sources, including disabled and failed ones.
- `SourceSupervisor`: owns one source’s bootstrap, stream, REST poll, reconciler, retries, and shutdown.
- `MarketCatalog`: shared per-protocol metadata so ten HL wallets do not each reload the same markets.

The dashboard operates on `SourceRuntime` records and never constructs exchange clients itself.

### 5.2 Adapter result contract

Replace ambiguous empty returns with a typed result:

```python
FetchResult[T](
    ok=True,
    data=T,
    authoritative=True,
    fetched_at=...,
    stale=False,
    capability="positions",
)
```

Failure example:

```python
FetchResult(
    ok=False,
    data=last_good_value,
    authoritative=False,
    stale=True,
    error_code="timeout",
    retryable=True,
)
```

Rules:

- `ok=True, data={}` means the API authoritatively reports no positions.
- `ok=False, data={}` never closes positions.
- Reconciliation changes position state only from an authoritative response.
- Stale data remains visible with an age badge.
- Each adapter exposes capability health independently:
  - `markets`
  - `positions`
  - `trades_ws`
  - `trades_rest`
  - `orders`
  - `realized_pnl`
  - `auth`
  - `network`

### 5.3 Source lifecycle state machine

Each source moves independently:

```text
disabled
  |
configured -> validating -> starting -> up
                  |            |       |
                  v            v       v
            config_error   retry_wait  degraded
                                           |
                                           v
                                          down
```

Behavior:

- `disabled`: intentionally off; no retries.
- `config_error`: missing or invalid required configuration; retry only on config reload.
- `auth_failed`: credentials rejected or missing permission; slow retry or config reload only.
- `retry_wait`: transient startup/network failure with exponential backoff and jitter.
- `up`: all required capabilities healthy.
- `degraded`: source is usable through a fallback, such as REST while WS is down.
- `down`: no authoritative position or trade path is currently available.

### 5.4 Task supervision

- One supervisor task per source.
- One source task crashing is caught, reported, and restarted without affecting other sources.
- Consumer errors are caught per event and routed to a dead-letter table.
- Use `asyncio.TaskGroup` or an explicit supervisor loop rather than one unguarded global `gather`.
- Backoff: 1s, 2s, 4s, 8s, 15s, 30s, then capped with ±20% jitter.
- Auth/configuration failures use a separate slow retry policy.
- Clean shutdown:
  1. Stop accepting new events.
  2. Cancel producers.
  3. Flush cursors and pending aggregates.
  4. Close WS/HTTP clients.
  5. Close database.
- Pending Telegram aggregates are persisted or deterministically discarded with an audit row; they are not silently duplicated after restart.

## 6. Identity and normalized data model

### 6.1 Stable source identity

Use explicit `source_id` everywhere:

```text
source_id = "hl-main"
display_name = "HL Main"
exchange = "hyperliquid"
```

Display names can change without changing history.

### 6.2 Market and position identity

Replace integer-only market identity with:

```text
market_key = exchange + venue_namespace + native_symbol
position_key = market_key + position_side
```

Examples:

```text
hyperliquid:default:BTC:BOTH
hyperliquid:xyz:TSM:BOTH
lighter:main:94:BOTH
binance:usdtm:BTCUSDT:BOTH
binance:usdtm:BTCUSDT:LONG
binance:usdtm:BTCUSDT:SHORT
```

Keep integer market IDs only as adapter-local metadata.

### 6.3 Trade identity

Change native trade IDs from `int` to lossless strings and derive a stable event UID:

```text
event_uid = SHA256(
  source_id |
  exchange |
  venue_namespace |
  native_symbol |
  native_trade_id |
  execution_fragment
)
```

Protocol-specific native identity:

- Hyperliquid: DEX namespace + `tid`.
- Lighter: pool source + `trade_id`.
- Binance: account source + symbol + Binance native trade `id`; include order/execution fragment if required.

Never use Binance transaction milliseconds as the sole trade ID.

### 6.4 Position tracker

Refactor `PositionTracker` to key by `PositionKey`, not integer market ID.

This enables:

- Same market IDs on different venues.
- HIP-3 namespaces.
- Binance hedge mode with simultaneous LONG and SHORT positions.
- Correct flips and partial closes per position side.

## 7. Persistence revamp

### 7.1 Schema versioning

Add a `schema_meta` table and explicit transactional migrations.

New tables/columns:

```text
sources
  source_id PRIMARY KEY
  exchange
  display_name
  account_fingerprint
  enabled
  created_at
  updated_at

source_cursors
  source_id
  stream
  scope
  cursor
  updated_at
  PRIMARY KEY(source_id, stream, scope)

events_v2
  event_uid PRIMARY KEY
  ts
  source_id
  exchange
  market_key
  position_side
  event_kind
  native_trade_id
  payload

closed_trades_v2
  id PRIMARY KEY
  round_trip_uid UNIQUE
  source_id
  exchange
  market_key
  position_side
  ...

alerts_v2
  alert_uid PRIMARY KEY
  source_id
  event_uid
  destination
  status
  attempts
  last_error
  delivered_at

dead_letters
  id PRIMARY KEY
  source_id
  stage
  payload
  error
  created_at
```

### 7.2 Required uniqueness

- Event uniqueness: `event_uid`.
- Cursor uniqueness: `(source_id, stream, scope)`.
- Round-trip uniqueness: deterministic `round_trip_uid`.
- Telegram delivery uniqueness: `(destination, alert_uid)`.

### 7.3 Migration procedure

1. Stop the service after explicit approval.
2. Copy `events.db` to a timestamped backup.
3. Run `PRAGMA integrity_check`.
4. Create v2 tables without deleting v1 tables.
5. Map legacy rows using a user-reviewed source-name-to-source-ID mapping.
6. Mark ambiguous legacy rows as `legacy-unassigned`; never guess.
7. Backfill deterministic event UIDs.
8. Compare row counts, source totals, PnL totals, and latest timestamps.
9. Run the application in read-only shadow mode against v2.
10. Switch writes to v2 only after validation.
11. Keep v1 tables for at least one rollback release.

## 8. Protocol-specific design

### 8.1 Hyperliquid

Multi-wallet behavior:

- One `SourceRuntime` per wallet.
- Share immutable market metadata across wallets.
- Keep clearinghouse caches, trade cursors, position trackers, and dedup state per source.
- Query default and every discovered HIP-3 DEX for positions and leverage.
- Maintain one cursor per `(source_id, dex)` because DEX `tid` sequences are independent.
- Dedup fills by `(source_id, dex, tid)`.
- Treat WS snapshots as warm state only.
- On reconnect, perform REST gap-fill per DEX cursor before resuming live delivery.
- Sort mixed-DEX recovery by timestamp, then stable event UID.
- If one DEX metadata/state request fails, mark that capability/DEX stale; do not erase positions from that DEX.
- Refresh DEX metadata periodically to discover newly deployed DEXes and markets.
- Query open orders and SL/TP by DEX namespace.
- Validate addresses before startup and log only masked forms.

Failure handling:

- No address env value: `config_error` if enabled, `disabled` if source is disabled.
- Invalid address: `config_error`; no network retry.
- REST down but WS up: keep streaming; positions become stale/degraded.
- WS down but REST up: degraded; poll REST more frequently within rate limits.
- Rate limit: honor server hints, apply jitter, keep last good state.
- Malformed fill: dead-letter the row and continue.
- Unknown market: refresh metadata, then use a deterministic namespace-aware fallback key.

### 8.2 Lighter

Multi-pool behavior:

- One source per public pool ID.
- Share the market catalog and HTTP connection pool.
- Keep WS subscriptions, REST cursors, trackers, and health per pool.
- Reject duplicate pool IDs.
- REST trade polling remains the guaranteed safety path.
- Store one cursor per pool.
- Validate that the account endpoint contains the requested pool before marking it up.

Failure handling:

- No pool ID: `config_error` if enabled.
- Invalid/nonexistent pool: down with a clear 404/not-found reason; retry slowly.
- WS restricted-jurisdiction HTTP 400: mark `trades_ws` degraded, keep source up through REST.
- REST trade endpoint down: source becomes down if WS is also unavailable; retain stale positions.
- Open-orders HTTP 403: mark only `orders` degraded/unsupported. Positions and fills remain up.
- Account response shape changes: retain last good positions and dead-letter a redacted schema sample.
- Duplicate or out-of-order trades: event UID dedup makes them harmless.
- Very old REST cursor outside endpoint history: expose `recovery_gap` health and require bounded reconciliation.

### 8.3 Binance Futures

Multi-account behavior:

- One source per credential pair.
- Resolve credential env names per source.
- Never log API keys; store only a short non-reversible credential fingerprint for duplicate detection.
- Use read-only Futures permissions.
- Maintain independent listen keys, time offsets, rate budgets, proxy selection, symbols, and cursors.
- Implement open-orders support rather than returning an empty stub.

No API/credential cases:

- No Binance source entry: Binance is absent and does not affect health.
- `enabled: false`: visible as disabled; no key required.
- Enabled source with missing key and secret: `config_error`; other protocols continue.
- Only one credential present: `config_error`.
- Invalid key/IP restriction/permission error: `auth_failed`; do not retry every few seconds.
- Spot-only key without Futures permission: `auth_failed` with remediation detail.

Position modes:

- Detect one-way versus hedge mode.
- Support both:
  - One-way uses `position_side=BOTH`.
  - Hedge mode uses independent LONG and SHORT position keys.
- A mode change while running triggers a controlled source resync; it must not synthesize closes.

REST recovery:

- Persist a cursor per symbol, not one account-wide timestamp.
- Use Binance native trade IDs, not millisecond timestamps.
- Query the union of:
  - currently open position symbols,
  - open-order symbols,
  - recently seen WS symbols,
  - symbols present in persisted cursors,
  - symbols indicated by realized-PnL/income activity.
- If downtime may contain a fully opened-and-closed new symbol, run a rate-limited discovery sweep.
- Bound concurrency and honor Binance request weights.
- If complete recovery cannot be proven, mark `recovery_gap` rather than claiming clean state.

Network and proxy behavior:

- A proxy profile is configured per source or shared by name.
- Default `allow_direct_fallback: false` prevents unexpected direct egress.
- Remove free/public proxies from production defaults; use a managed proxy or a VM region where Binance is supported.
- HTTP 451 marks the route unavailable, not the credentials invalid.
- HTTP 429 honors `Retry-After`; repeated 429 opens a circuit breaker.
- HTTP 418 opens a longer ban circuit and sends an owner warning.
- Timestamp error `-1021` triggers server-time synchronization and one safe retry.
- Listen-key creation/renewal failures fall back to REST recovery.
- WS reconnect always creates a fresh listen key and gap-fills before declaring the source current.

## 9. Empty, missing, and partial configuration behavior

### No sources configured

- Dashboard starts.
- Database starts.
- `/healthz` returns 200 because the process is alive.
- `/readyz` returns 503 with `no enabled sources`.
- `/health.json` lists protocol summaries as disabled/unconfigured.
- UI displays an onboarding panel instead of an empty table.
- Telegram jobs do not run.

### Only one protocol configured

- Only that protocol is instantiated.
- Missing settings for other protocols are not warnings.
- Disabled protocols do not make global health fail.

### Source enabled but required data missing

- Source remains visible as `config_error`.
- Other sources start normally.
- The process does not exit.
- A single owner notification is sent if Telegram owner DM is configured.
- Retries wait for config reload; there is no network retry storm.

### Every enabled source is down

- Dashboard and health endpoints remain online.
- Existing last-good positions are shown as stale.
- Telegram trade alerts pause because there is no authoritative event source.
- Source supervisors continue bounded retries.
- `/readyz` returns 503.

### Telegram not configured

- Tracking, database, dashboard, and reconciliation continue.
- Telegram capability is `disabled`, not down.
- Alerts are recorded as `not_configured` only if audit history is desired; they are not repeatedly retried.

### Database unavailable

- Do not send trade alerts without idempotency persistence.
- Enter safe/degraded mode.
- Keep source health visible and optionally keep in-memory snapshots.
- Retry database access with bounded backoff.
- Resume event delivery only after cursors and dedup state are safely restored.

## 10. Health, readiness, and observability

### Endpoints

- `/healthz`: process liveness only.
- `/readyz`: at least one enabled source has an authoritative trade path and database is writable.
- `/health.json`: redacted detailed state.
- `/metrics`: optional Prometheus-format metrics bound internally.

### Per-source health fields

```json
{
  "source_id": "hl-main",
  "name": "HL Main",
  "exchange": "hyperliquid",
  "state": "degraded",
  "last_event_at": "...",
  "last_position_sync_at": "...",
  "position_age_seconds": 42,
  "capabilities": {
    "positions": "up",
    "trades_ws": "down",
    "trades_rest": "up",
    "orders": "degraded"
  }
}
```

Never include raw addresses, pool-private data, API keys, proxy credentials, or full upstream error bodies.

### Metrics and alerts

- Events received/accepted/deduplicated/dead-lettered by source.
- WS reconnect count.
- REST request success/error/rate-limit counts.
- Cursor lag and position snapshot age.
- Telegram delivery attempts/failures.
- Database write latency/errors.
- Proxy health by redacted route ID.
- Startup/bootstrap duration.
- Config reload success/failure.

Owner warnings should be edge-triggered and rate-limited, not emitted every poll.

## 11. Dashboard and Telegram behavior

### Dashboard

- Group by account/source, then exchange, then market.
- Show a stable source label and exchange badge.
- Show stale age and capability status.
- Allow filtering by source ID, exchange, symbol, side, and status.
- Distinguish Binance LONG/SHORT hedge positions.
- Preserve privacy transformations per source.
- Do not infer a close from missing/stale data.
- Disabled/config-error sources appear in a setup/status section, not in position tables.

### Telegram

- Every message includes the source display label.
- Alert dedup key includes `source_id` and `event_uid`.
- Delivery is persisted before/after send using an outbox pattern.
- Retry transient Telegram errors with backoff.
- Do not retry permanent chat/token errors indefinitely.
- Per-source routing can optionally send different accounts to different channels.
- Daily recap can be combined or split by source; default is combined with per-source sections.
- Self-audit runs per `source_id`, never display name.
- Alerts from one source never suppress another source’s identical-looking alert.

## 12. Configuration reload and adding addresses

Target workflow after the revamp:

1. Add the new secret/address to the protected environment or GCP Secret Manager.
2. Add a non-secret source entry to `runtime/sources.yaml`.
3. Validate:

   ```bash
   python -m src.config_cli validate --sources runtime/sources.yaml
   ```

4. Reload:

   ```bash
   sudo systemctl reload lighterbot
   ```

Reload behavior:

- Parse and validate the entire candidate configuration.
- Build and bootstrap new sources before activating them.
- Keep the old runtime untouched if validation/bootstrap fails.
- Atomically swap successful additions.
- Removed sources stop producing events but retain history.
- Changed credentials restart only the affected source.
- Changed labels do not alter source identity/history.
- Every reload writes a redacted audit entry.

No public web endpoint may accept Binance credentials or mutate source configuration.

## 13. Implementation work packages

### Phase 0 — Safety baseline

- Freeze the current production commit.
- Back up VM application, `.env`, runtime config, and SQLite DB.
- Capture current health/API/DB baselines.
- Add a feature branch and deployment rollback package.
- Add regression fixtures for current HL, HIP-3, Lighter, and Binance payloads.

Exit criteria:

- Current 777-test suite remains green.
- Production backup and rollback command are verified.

### Phase 1 — Typed configuration and empty-source mode

Files:

- `src/sources.py`
- new `src/source_config.py`
- new `src/config_cli.py`
- `config.example.yaml`
- `.env.example`
- systemd deployment files

Deliverables:

- `SourceSpec`, validation report, secret references, explicit IDs.
- `load_sources()` no longer raises when zero sources are usable.
- Disabled/config-error sources remain in the registry.
- Legacy single-account compatibility.
- CLI validation with redacted output.

Exit criteria:

- Multiple specs parse.
- Missing HL/Binance/Lighter configuration cannot crash startup.
- No-source dashboard starts.

### Phase 2 — Composite identity and database v2

Files:

- `src/types.py`
- `src/db.py`
- `src/position_tracker.py`
- migration module/CLI

Deliverables:

- `source_id`, exchange, market key, position side, native ID, event UID.
- Source-scoped cursors and dedup.
- Schema versioning and non-destructive v2 migration.
- Legacy mapping report.

Exit criteria:

- Same native trade ID from two accounts creates two valid events.
- Duplicate delivery within one account creates one event.
- Restart does not resend alerts.

### Phase 3 — Result semantics and source supervisors

Files:

- new `src/result.py`
- new `src/source_runtime.py`
- new `src/supervisor.py`
- refactor `src/dashboard.py`
- `src/health.py`

Deliverables:

- Authoritative/stale/error result contract.
- Per-source task isolation and state machine.
- Separate liveness/readiness.
- No false closes from API failures.

Exit criteria:

- Forced timeout on one source leaves all others running.
- Position API failure preserves prior positions as stale.
- Unexpected producer exception restarts only that source.

### Phase 4 — Hyperliquid and Lighter multi-account support

Files:

- `src/hyperliquid_client.py`
- `src/lighter_client.py`
- shared market catalog modules

Deliverables:

- Multiple HL wallet env references.
- Multiple Lighter pools.
- Shared metadata with per-source cursors/state.
- HIP-3 DEX positions, leverage, orders, and fills per wallet.
- Lighter WS/REST capability health.

Exit criteria:

- Two HL wallets and two Lighter pools run simultaneously.
- Same symbol/market ID never collides.
- One wallet/pool outage does not affect others.

### Phase 5 — Binance production support

Files:

- `src/binance_client.py`
- proxy/rate-limit modules
- Binance-specific cursor discovery

Deliverables:

- Multiple credential pairs.
- One-way and hedge-mode positions.
- Native trade IDs.
- Per-symbol persisted cursors and downtime discovery.
- Open orders.
- Time sync, rate-limit, auth, listen-key, and proxy circuits.

Exit criteria:

- Missing API keys show config error without affecting HL/Lighter.
- Invalid credentials stop retry storms.
- WS outage recovers through REST.
- Fully opened-and-closed downtime activity is either recovered or explicitly marked as a gap.

### Phase 6 — Dashboard, Telegram outbox, and audits

Deliverables:

- Source/exchange filters and stale badges.
- Account-scoped statistics and audits.
- Persistent Telegram outbox/idempotency.
- Per-source destination support.
- Redacted health and metrics.

Exit criteria:

- Identical alerts from two sources both send once.
- Daily audit compares the correct account only.
- Public payload contains no private address/credential material.

### Phase 7 — Atomic reload and operational tooling

Deliverables:

- Config validator.
- `systemctl reload` support.
- Atomic source diff/apply.
- Redacted config status command.
- Backup, migrate, deploy, verify, and rollback scripts.

Exit criteria:

- Add one HL address without code deployment.
- Invalid candidate config leaves the running configuration untouched.
- Removing a source stops tasks without deleting history.

### Phase 8 — Canary rollout

1. Deploy with legacy config and v1 behavior behind flags.
2. Enable v2 identity/persistence in shadow mode.
3. Compare source counts, positions, fills, PnL, and alerts.
4. Enable one additional read-only source.
5. Run for at least one complete daily audit cycle.
6. Enable remaining sources one at a time.
7. Keep rollback package and v1 DB available.

## 14. Required test matrix

### Configuration

- Empty source list.
- Missing source file.
- All sources disabled.
- One enabled source missing its env variable.
- Binance key without secret and secret without key.
- Duplicate source ID.
- Duplicate wallet/pool/account.
- Duplicate display names with distinct IDs.
- Invalid address, pool ID, env variable name, proxy URL, and numeric settings.
- Legacy config migration.

### Source isolation

- HL fails while Lighter/Binance continue.
- Lighter WS fails while REST continues.
- Binance auth fails while HL/Lighter continue.
- One supervisor crashes and restarts.
- All sources down while dashboard remains available.

### State correctness

- API error never produces a close.
- Authoritative empty snapshot does produce closes.
- Stale snapshot age increases without mutating positions.
- Out-of-order, duplicate, and delayed fills.
- REST/WS overlap.
- Restart during an aggregate window.
- Position flip and partial reduce.
- Same symbol across accounts.
- Same native trade ID across accounts.
- Binance two fills in the same millisecond.
- Binance LONG and SHORT hedge positions simultaneously.
- HL default and HIP-3 DEX IDs/cursors.
- Lighter pool IDs with overlapping market IDs.

### Recovery

- Cursor persisted before crash.
- Crash after DB event insert but before Telegram send.
- Crash after Telegram send but before acknowledgement.
- WS disconnect with missed fills.
- REST pagination and history-window exhaustion.
- Rate limit, timeout, malformed JSON, schema drift, and clock skew.
- Database locked/corrupt/unwritable.
- Proxy exhaustion and cooldown recovery.

### Security/privacy

- Raw HL addresses absent from source IDs, logs, URLs, health, Telegram, cards, and filenames.
- Binance keys/secrets absent from all output and exception text.
- Proxy credentials redacted.
- Privacy transform remains stable per source and separate between wallets.
- Public dashboard cannot mutate configuration or reveal env variable values.
- Runtime secret/config file permissions verified.

### Deployment

- Migration on a copy of the production DB.
- Rollback to the previous binary and v1 DB.
- Config reload success and rollback on failure.
- Public HTTPS, local health, readiness, WebSocket snapshot, Telegram dry run, and database integrity.

## 15. Definition of done

The revamp is complete only when:

- At least two HL wallets, two Lighter pools, and two Binance accounts can be represented simultaneously.
- Any subset of protocols may be absent.
- The app starts with zero sources.
- One bad source cannot stop or corrupt another.
- Missing Binance credentials are handled without crashes or retries.
- Binance one-way and hedge modes are correctly represented.
- HIP-3 positions/fills are complete per wallet.
- Every event, cursor, alert, and closed trade has stable source-scoped identity.
- API failure cannot generate a false close.
- Restart and WS/REST overlap do not duplicate alerts.
- Config can be validated and reloaded without a code deployment.
- Production migration and rollback have been rehearsed with the real database.
- All existing tests plus the new matrix pass.

## 16. Recommended execution order

Do not start by merely changing `HL_ADDRESS` to a list. The safe dependency order is:

1. Typed source config and explicit IDs.
2. Composite identity and DB v2.
3. Authoritative/stale result semantics.
4. Source supervisors and empty-source startup.
5. Multi-HL and multi-Lighter adapters.
6. Binance native IDs, recovery, and hedge mode.
7. Dashboard/Telegram account scoping.
8. Atomic reload and production migration.

This order prevents new accounts from being added on top of identity and failure semantics that can currently conflate histories or produce false alerts.
