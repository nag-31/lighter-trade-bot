# Lighter Trade Bot — Agent Handover

> Complete context for any AI agent picking up this project cold.
> Read this BEFORE touching any code or the VM.
> Last updated: 2026-07-18

---

## What this is

A Python async bot that watches a **Lighter public pool** + a **Hyperliquid wallet**
and:
- Posts trade alerts (open / close / scale-in / scale-out) to a **Telegram channel**
- Generates **PnL card images** (Pillow, 920×500 PNG) for CLOSE events showing
  ±$PnL, % move, entry/exit pills, psychology quote, win-rate bar
- Serves a **web dashboard** at `https://dashboard.enkapital.xyz` (aiohttp +
  Chart.js + SQLite) with live positions, closed-trades grid, equity curve,
  and a health page
- Persists all events + closed trades to **SQLite** (`data/events.db`)
- Runs as a **systemd service** (`lighterbot`) on an Azure Ubuntu VM

---

## Where the code lives

| Location | Purpose |
|---|---|
| `D:\content\crypto scientist\lighter-trade-bot\` | Local repo (Windows dev machine) |
| `~/lighter-trade-bot/` | Deployed copy on the Azure VM |
| Git remote | `origin` (GitHub — check `git remote -v` for URL) |
| Current active branch | `main` (production branch) |

### Key source files

| File | Role |
|---|---|
| `src/dashboard.py` | **Entrypoint** — aiohttp app, all event handlers, CLOSE logic, WebSocket hub, daily jobs |
| `src/sources.py` | `BotSettings`, `Source`, `load_settings()` — reads config.yaml |
| `src/stats.py` | `aggregate_round_trips()`, `filter_trades()`, `compute_stats()` — display-layer aggregation |
| `src/db.py` | SQLite helpers — `closed_trades` schema, save/load/query |
| `src/types.py` | `Trade`, `Position`, `Event`, `EventKind` dataclasses |
| `src/hyperliquid_client.py` | HL WebSocket + REST fills; `fetch_realizing_fills()`, `_parse_fill()` |
| `src/lighter_client.py` | Lighter REST + WebSocket |
| `src/pnl_card.py` | Pillow PnL card generator; `record_result()`, `peek_result()` |
| `src/display_transform.py` | Privacy fuzzing for HL price/size/notional display |
| `src/health.py` | `/health` JSON endpoint |
| `src/proxy_pool.py` | Rotating SOCKS5 proxy pool (for Binance geo-block bypass) |
| `scripts/reconcile_hl_pnl.py` | One-shot HL DB rebuild from exchange fills |
| `scripts/hl_pnl_logic.py` | Pure reconstruction logic (no I/O) — testable |
| `config.yaml` | All tunable settings + source definitions (no secrets) |
| `requirements.txt` | Python dependencies |
| `Dockerfile` | Docker build (not used in prod — systemd native on VM) |

---

## VM (Azure Ubuntu)

| Property | Value |
|---|---|
| SSH alias | `lighter-bot` |
| IP | `20.29.250.158` |
| User | `azureuser` |
| Key | `C:\Users\ADMIN\.ssh\lighter-bot.pem` |
| SSH command | `ssh -i "C:/Users/ADMIN/.ssh/lighter-bot.pem" azureuser@20.29.250.158` |
| Repo path on VM | `~/lighter-trade-bot/` |
| Python venv | `~/lighter-trade-bot/.venv/` |
| Service name | `lighterbot` |
| Dashboard URL | `https://dashboard.enkapital.xyz` (Caddy → localhost:8080) |
| Raw port | `http://20.29.250.158:8080` (same, no TLS) |

> **IMPORTANT**: If the VM doesn't respond to ping/SSH it is likely **deallocated**.
> Start it from the Azure portal (`portal.azure.com`) before attempting any SSH.

### SSH config (`~/.ssh/config`)
```
Host lighter-bot
    HostName 20.29.250.158
    User azureuser
    IdentityFile C:\Users\ADMIN\.ssh\lighter-bot.pem
```

---

## Deploy / Update procedure

```bash
# 1. LOCAL — commit and push changes
git add <files>
git commit -m "description"
git push

# 2. VM — pull + restart (ALWAYS ask user before restarting the live service)
ssh lighter-bot "cd ~/lighter-trade-bot && git pull && pip install -r requirements.txt -q"
# Then, with explicit user authorization:
ssh lighter-bot "sudo systemctl restart lighterbot"
```

### Service management commands
```bash
ssh lighter-bot "sudo systemctl status lighterbot --no-pager"
ssh lighter-bot "sudo journalctl -u lighterbot -n 100 --no-pager"
ssh lighter-bot "sudo journalctl -u lighterbot -f --no-pager"   # live tail
```

### Run the reconcile script on VM
```bash
# Dry run (safe — no DB writes)
ssh lighter-bot "cd ~/lighter-trade-bot && .venv/bin/python scripts/reconcile_hl_pnl.py --days 10"

# Apply (DESTRUCTIVE — always ask user first, DB is backed up automatically)
ssh lighter-bot "cd ~/lighter-trade-bot && .venv/bin/python scripts/reconcile_hl_pnl.py --days 10 --apply"
```

---

## Environment variables (`.env` on VM)

File lives at `~/lighter-trade-bot/.env`. **Never commit this file.**

```
TELEGRAM_BOT_TOKEN=<bot token>
TELEGRAM_CHANNEL_ID=<channel id, e.g. -100...>
TELEGRAM_OWNER_USER_ID=609660228          # private DM target for self-audit alerts
HL_ADDRESS=0x...                          # Hyperliquid wallet — NEVER log or post publicly
PRIVACY_SECRET_KEY=<generated on VM>      # price fuzzing salt — NEVER show in chat
BINANCE_API_KEY=<read-only key>           # Binance futures read-only
BINANCE_API_SECRET=<secret>
```

### Security rules (hard — never violate)
1. **HL_ADDRESS** — `.env` only. Never in config, logs, TG, filenames, or chat.
   Masked as `0x…4c74` if it must appear in output.
2. **PRIVACY_SECRET_KEY** — generated on the VM, never shown in chat.
3. **Binance creds** — `.env` only. READ-ONLY keys only.
4. **footer_url in config** — only a public website (e.g. `https://enkapital.xyz`).
   Never an account/wallet/explorer URL.
5. **Restarting the live `lighterbot` service** — requires explicit user
   authorization each time. Never restart without asking.

---

## config.yaml key knobs

All in `config.yaml` at the repo root. Safe to commit (no secrets).

```yaml
settings:
  default_min_notional_usd: 1000   # filter small trades from TG alerts
  aggregate_window_seconds: 60     # batch add/reduce fills within window
  digest_window_seconds: 20        # combine multi-coin alerts into one TG message
  rest_poll_seconds: 60            # REST fallback poll interval
  reconciler_interval_seconds: 30  # silent-close detection interval
  daily_recap_enabled: true        # 00:01 UTC channel recap
  daily_self_audit_enabled: true   # 00:01 UTC private DM audit to owner
  dashboard_port: 8080
  stats_start_date: "2026-06-01"  # dashboard shows trades from this date
  privacy_enabled: true            # HL display fuzzing (flip to false for real values)

sources:
  - type: lighter
    name: "My NK pool"
    pool_id: 281474976684763

  - type: hyperliquid
    name: "HL"
    min_notional_usd: 1000
    exclude_symbols: ["FARTCOIN"]
    footer_url: "https://enkapital.xyz"
    # HL_ADDRESS loaded from .env

  # Binance commented out — HTTP 451 geo-block from Azure region
```

---

## Architecture overview

```
config.yaml + .env
      │
load_sources()          ← src/sources.py
      │
Source[]  (client + tracker + db + settings)
      │
┌─────┴──────────────────────────────────────────────┐
│  per source, three concurrent async tasks:         │
│  ws_producer()              ← real-time WS fills   │
│  rest_safety_producer()     ← 60s REST fallback    │
│  position_reconciler()      ← 30s silent-close sync│
└─────┬──────────────────────────────────────────────┘
      │  asyncio.Queue[(source_id, Trade)]
consumer()
      │
PositionTracker.apply(trade) → Event[]
      │
┌─────┴──────────────────────────────────────────────┐
│  OPEN        → immediate TG alert                  │
│  CLOSE       → PnL card PNG → TG photo             │
│                (card total = full round-trip PnL)  │
│  SIZE_CHANGE → 60s batch → digest → one TG msg     │
│  REDUCE      → 60s batch → digest → one TG msg     │
└──────────────────────────────────────────────────-─┘
      │
Hub.broadcast()   ← aiohttp WebSocket → dashboard
db.save_event()   ← SQLite events.db
db.save_closed()  ← SQLite closed_trades table
```

### SQLite database

- File: `data/events.db` on the VM
- Tables: `events`, `closed_trades`, `open_orders`
- `closed_trades` schema: `ts, source, market_symbol, side, entry, exit, size,
  notional, pnl, pct, is_win, leverage, wins, total, card_path, trade_id,
  fill_ids, realization_kind`
- `realization_kind`: `"FULL"` (position closed) / `"PARTIAL"` (scale-out) /
  `NULL` (legacy rows from before fill tracking was added)
- `card_path`: web URL like `/cards/<filename>.png` (NOT a filesystem path)

---

## Display-layer aggregation rules

The DB stores one row **per fill**. The dashboard collapses into one tile per
**round-trip** via `stats.aggregate_round_trips()`:

- A round-trip ends on ANY non-`PARTIAL` row (`FULL`, `NULL`, or unknown kind)
- `PARTIAL` rows keep the round-trip open (in-progress tile)
- Win-rate is judged on the **round-trip total PnL**, not individual fill PnL
- **Order matters**: `filter_trades(cutoff)` FIRST, then `aggregate_round_trips()`
  — reversing this bleeds pre-cutoff fill PnL into the window

### Legacy NULL rows

8 rows (IDs 1–8) from a migration window have `realization_kind=NULL` and no
`fill_ids`. Six of them are duplicates of later fill-based rows (same coin, same
instant) — `_drop_legacy_duplicate_rows()` in `stats.py` drops them when a
fill-based counterpart exists within 1 hour. Two (ENA +$165.01, SPX −$25.93)
are genuine standalone trades — kept.

---

## Telegram alert behavior

- **OPEN**: immediate single alert per new position
- **CLOSE**: PnL card PNG photo with caption `"COIN SIDE · ±$PnL\nfooter_url"`
- **SIZE_CHANGE / REDUCE**: buffered 60s, then flushed as single "Added"/"Reduced"
  alert. Gate: net notional ≥ `min_notional_usd` (sub-threshold moves suppressed
  but still recorded to DB)
- **Session digest**: if multiple coins flush within `digest_window_seconds` (20s)
  on the same source, they are combined into ONE message ("📦 HL — N updates")
- **Daily recap**: at 00:01 UTC — closed-trades stats for previous day to channel
- **Daily self-audit**: at 00:01 UTC — compares DB per-coin PnL vs HL exchange;
  any mismatch >$1 → **private DM to TELEGRAM_OWNER_USER_ID** (not public)
- **Dedup**: identical messages within 90s are silently dropped

---

## Known quirks and gotchas

| Issue | Fix |
|---|---|
| Lighter WS geo-blocked (HTTP 400 code 20558) from Azure | REST poll covers fills (60s delay). Set `LIGHTER_WS_PROXY` in `.env` for real-time |
| Binance HTTP 451 from Azure | Source commented out; `binance_proxies` list in config for when needed |
| PnL cards cached in browser | `/cards/` route has `Cache-Control: no-cache` — do Ctrl+Shift+R after deploys |
| Card filename unchanged across rebuilds | No-cache header means browser always re-fetches; disk PNG is authoritative |
| `realization_kind=NULL` rows | Legacy only; handled by dedup logic in `stats.py` |
| Win-rate on card | Shows `wins/total` from `_score_window` (last 50 closed trades, resets on restart) |
| Card click behavior | Opens in-page lightbox (`showCardModal()`), not a new tab |
| In-progress tile label | Shows "LATEST SCALE-OUT — NOT THE TRADE TOTAL" to avoid confusion |
| Privacy fuzzing | HL only. PnL/% exact; price/size/notional fuzzed. Master switch: `privacy_enabled` in config |
| HL address masked | Logged/displayed as `0x…4c74` everywhere |

---

## Running tests locally

```bash
cd "D:\content\crypto scientist\lighter-trade-bot"
python -m pytest tests/ -v
```

Test files cover: round-trip aggregation, reconcile logic, Binance client,
HL client, display transforms, privacy, stats, PnL cards, filters, health.

---

## Reconcile script (HL DB rebuild)

Used to rebuild `closed_trades` from HL exchange fills when the DB is stale/wrong.

```bash
# Dry run — prints what would change, no writes
python scripts/reconcile_hl_pnl.py --days 10

# Apply — ALWAYS BACK UP FIRST (script does this automatically), ask user
python scripts/reconcile_hl_pnl.py --days 10 --apply
```

**Before running `--apply`:**
- Confirm with user
- Script auto-backups DB to `data/events.db.bak-<timestamp>`
- Will re-generate all PnL card PNGs (slow for large windows)
- Post-apply: verify per-coin PnL totals match HL manually

---

## Dashboard access

- Public URL: `https://dashboard.enkapital.xyz`
- Local (on VM): `http://localhost:8080`
- Raw (from internet): `http://20.29.250.158:8080`
- Health endpoint: `https://dashboard.enkapital.xyz/health` (JSON)
- Stats API: `POST /api/send_stats` (rate-limited 1/hr, sends to TG)

---

## What the dashboard shows

- Live positions (WebSocket-updated)
- Closed trades grid (cards, per round-trip, newest first)
- Equity curve (date x-axis, cumulative PnL)
- PnL per trade (bar chart, labelled by ticker)
- Win rate, total PnL, trade count — all from `stats_start_date` onwards
- Open orders panel (if `open_orders_enabled: true`)

---

## Current state (as of 2026-07-18)

- Bot commit: `0ca1707` (cache headers + in-progress tile label)
- DB: 365 closed-trade rows (June 1+: 74 trades, +$1,676.51, 48.6% win)
- 1 open position: LIT short
- Daily recap + self-audit: live (runs at 00:01 UTC)
- Binance source: disabled (geo-block)
- Lighter WS: geo-blocked, REST poll active (60s)
- TELEGRAM_OWNER_USER_ID: set in .env on VM

---

## Multi-account trade tracker revamp (implemented locally 2026-07-23)

Status: deployed to the GCP VM on 2026-07-23 from commit `12b7019`
(`codex/private-portfolio-app`). The production SQLite database was backed up
and migrated to schema v2, `lighterbot` was restarted, and local/public health
reported ready. Lighter wallet-address source support was added on 2026-07-23.
The complete suite now contains 793 passing tests.

Production backup:

```text
/home/ADMIN/backups/20260723T100838Z/lighter-trade-bot
```

### Supported source model

- Any number of Hyperliquid wallets, each using a stable `id` and its own
  `address_env`.
- Any number of Lighter public pools, each using a stable `id` and `pool_id`.
- Any number of Lighter wallet accounts using `address_env`; raw wallet
  addresses stay only in `.env`. `account_slot` selects a master/subaccount
  from Lighter's `accountsByL1Address` response and defaults to `0`.
- Any number of Binance accounts, each using a stable `id` and separate
  `api_key_env` / `api_secret_env`.
- Any protocol may be absent. The dashboard starts with zero active sources.
- Missing credentials disable only the affected source and appear in health.
- Duplicate source IDs and duplicate account/pool configurations are rejected.

Example:

```yaml
sources:
  - type: hyperliquid
    id: hl-main
    name: "HL"
    address_env: HL_ADDRESS

  - type: hyperliquid
    id: hl-second
    name: "HL 2"
    address_env: HL_ADDRESS_2

  - type: lighter
    id: lighter-main
    name: "Lighter"
    pool_id: 281474976684763

  - type: lighter
    id: lighter-wallet
    name: "Lighter Wallet"
    address_env: LIGHTER_ADDRESS
    account_slot: 0

  - type: binance
    id: binance-main
    name: "Binance"
    api_key_env: BINANCE_API_KEY
    api_secret_env: BINANCE_API_SECRET
```

Put the corresponding values in `.env`; never put wallet addresses or Binance
credentials in `config.yaml`. Lighter wallet lookup is public and needs no API
key. If one L1 address has subaccounts, repeat the source with the same
`address_env`, a unique `id`, and `account_slot: 1`, `2`, etc.

### Safety and recovery changes

- Position fetches now distinguish authoritative empty snapshots from stale or
  failed requests. A failed API request cannot create a false position close.
- Last-good positions remain visible with a `STALE` badge during an outage.
- Each source's WebSocket, REST recovery, and position reconciler are supervised
  independently with bounded restart backoff.
- Source, market, position-side, and native trade IDs form the v2 event identity.
- SQLite schema v2 adds source-scoped IDs, persisted cursors, unique event IDs,
  and a persistent Telegram outbox.
- Binance supports one-way and hedge mode, uses native trade IDs, discovers
  fully closed downtime symbols through realized-income history, paginates
  user trades, loads open orders, syncs clock drift, and pauses auth/rate-limit
  retry storms.
- Hyperliquid partial DEX failures and Lighter account failures are
  non-authoritative, so existing positions are retained instead of closed.
- Dashboard filters support exchange and account selection.
- Reconciliation requires `--source-id` when multiple HL wallets exist.

### Validation, migration, and reload

Redacted validation:

```bash
python -B scripts/validate_tracker_config.py --config config.yaml
```

Check DB migration status without writing:

```bash
python -B scripts/migrate_tracker_db_v2.py --db data/events.db
```

Apply migration (backs up the DB first):

```bash
python -B scripts/migrate_tracker_db_v2.py --db data/events.db --apply
```

After editing `.env` and `config.yaml` on Linux, validate first, then atomically
reload source configuration without restarting the service:

```bash
sudo systemctl kill -s HUP lighterbot
```

An invalid candidate config is rejected and the currently running source
registry stays active. The bot re-reads `.env` with the candidate YAML before
applying a SIGHUP reload.

---

## Files NOT to touch

- `data/events.db` — live database; never edit directly without backup
- `.env` — secrets; never commit, never show in chat
- `data/*.db-wal`, `data/*.db-shm` — SQLite WAL files; don't delete while service runs

---

## Codex Windows sandbox ACL recovery (2026-07-18)

### Symptom

Every normal sandboxed shell or `apply_patch` call fails before execution with:

```
windows sandbox: helper_unknown_error: apply deny-read ACLs
```

Elevated shell commands still work.

### Confirmed local causes and cleanup

This workspace had stale Codex security descriptors from older sandbox identities:

- Two orphaned SID grants on `lighter-trade-bot/`.
- Orphaned **deny** ACEs on `lighter-trade-bot/.git`.
- Several `.pytest_cache` directories with ACL inheritance disabled.
- The thread visualization writable root had only read/execute for `CodexSandboxUsers`.

The repair was:

1. Back up ACLs before changing them (`.deploy/lighter-trade-bot-acl-before.txt` and `.deploy/visualization-root-acl-before.txt`).
2. Purge only the confirmed orphaned SIDs with the Windows ACL API (`Get-Acl` / `PurgeAccessRules` / `Set-Acl`).
3. Re-enable inheritance on protected pytest caches with `icacls <cache> /inheritance:e /T /C`.
4. Grant `CodexSandboxUsers` Modify on the configured visualization writable root.
5. Audit the complete workspace for protected directory ACLs or deny ACEs. The 2026-07-18 post-fix audit found 19,373 directories and zero remaining ACL issues.

Do **not** broadly reset the workspace ACL or delete caches/repos. Remove only confirmed stale principals and always save ACLs first.

### Current conclusion

The workspace ACL cleanup is valid and retained, but neither a full Codex update/restart nor switching the supported Windows mode from `elevated` to `unelevated` resolved the helper error. The global config was restored from `.deploy/config.toml-before-sandbox-fix` to avoid retaining an unproven workaround. Treat this exact error as an unresolved Codex Windows helper defect on this machine; normal tool calls require scoped elevation until the product is repaired. The application deployment can continue through approved elevated commands.

---

## Active GCP deployment (2026-07-18)

This section supersedes the Azure VM and dashboard endpoint information above.

### VM and access

- GCP project: `project-55b8aafe-d086-47bd-8dd`
- VM: `crypto-apps-vm`
- Zone: `asia-south1-a`
- Static public IP: `8.231.102.153`
- Machine: `e2-standard-2`, Ubuntu 24.04, 50 GB balanced persistent disk
- SSH user: `ADMIN`
- Local SSH key: `C:\Users\ADMIN\.ssh\google_compute_engine`
- Direct SSH: `ssh -i "C:\Users\ADMIN\.ssh\google_compute_engine" ADMIN@8.231.102.153`
- Application root on VM: `/home/ADMIN/apps`
- Timestamped deployment backups: `/home/ADMIN/backups/<timestamp>`

The local projects remain in `D:\content\crypto scientist`; they were copied for deployment, not moved.

### Central access hub and public applications

- Hub: `https://hub.8-231-102-153.sslip.io/`
- Lighter dashboard: `https://dashboard.8-231-102-153.sslip.io/`
- Portfolio tracker: `https://portfolio.8-231-102-153.sslip.io/`
- PnL analytics: `https://analytics.8-231-102-153.sslip.io/`
- Pro PnL dashboard: `https://pnl.8-231-102-153.sslip.io/`
- Futures importer: `https://importer.8-231-102-153.sslip.io/`
- Hack alert: `https://hack.8-231-102-153.sslip.io/`
- Full bot: `https://bot.8-231-102-153.sslip.io/`
- eNKapital: `https://enkapital.8-231-102-153.sslip.io/`

Nginx redirects HTTP to HTTPS. Certbot issued one certificate covering all nine hostnames; it expires 2026-10-16 and `certbot.timer` is enabled for automatic renewal. The previous custom domain `dashboard.enkapital.xyz` has not been repointed to GCP; use the URLs above until its DNS is updated.

### Services

The following systemd units are installed and enabled:

```bash
lighterbot.service
portfolio.service
pnl-analytics.service
apps-hub.service
full-fledged-bot.service
hack-alert.service
specbot.service
importer.service
enkapital-site.service
```

Check status without changing anything:

```bash
sudo systemctl status lighterbot portfolio pnl-analytics apps-hub full-fledged-bot hack-alert specbot importer enkapital-site --no-pager
curl -fsS https://hub.8-231-102-153.sslip.io/api/status
```

The Pro PnL app is a Docker Compose stack in `/home/ADMIN/apps/pnl-dashboard` with `app`, `worker`, `db`, and `redis`. Its ports are bound to `127.0.0.1`; only Nginx is internet-facing. Inspect it with:

```bash
cd /home/ADMIN/apps/pnl-dashboard
sudo docker compose ps
sudo docker compose logs --tail=100 app worker
```

Database migrations and Timescale setup were applied. Existing schema setup emits non-blocking warnings because several snapshot/fill tables lack a `ts`-inclusive unique index and `positions_snapshots` is not currently a hypertable; the application and worker are healthy despite those warnings.

### Verified deployment state

- All nine systemd services are active.
- All eight hub applications report `Online` from `/api/status`.
- Every internal health endpoint returned HTTP 200.
- The PnL app, database, Redis, and worker are healthy.
- Lighter test suite: 765 passed.
- Hack alert test suite: 218 passed.
- All copied SQLite databases passed `PRAGMA integrity_check`.
- The hack alert project has no `.env`, so its Telegram delivery remains disabled/dry-run until credentials are intentionally supplied.

### Safe update procedure

1. Make changes in the local project under `D:\content\crypto scientist` and run its tests.
2. Back up the VM application/database before replacing files.
3. Copy only the intended changed files to the matching directory under `/home/ADMIN/apps`.
4. Never print, commit, or overwrite `.env` files. They are mode `0600` on the VM.
5. Validate configuration and health before restarting.
6. A restart is a state-changing production action: obtain the user's explicit approval immediately before running `systemctl restart` or `docker compose up -d --build`.
7. After restart, verify the unit/container, its local health endpoint, the public HTTPS endpoint, and `https://hub.8-231-102-153.sslip.io/api/status`.

Deployment definitions live in `deploy/gcp/`, including the systemd units and `crypto-apps-nginx.conf`. The Apps Hub's cloud hostname handling is in `apps_hub/access_page.py` and uses `APP_HUB_PUBLIC_SUFFIX` plus `APP_HUB_PUBLIC_SCHEME`.

### HIP-3 Hyperliquid tracking fix (2026-07-23)

Hyperliquid builder-deployed perps (for example `xyz:AMD`) use separate DEX metadata universes, asset-ID offsets, and `tid` sequences. The tracker now loads `perpDexs` plus each DEX's `meta(dex=...)`, maps HIP-3 assets with the SDK's `110000 + DEX index * 10000` offsets, keeps per-DEX REST/WS cursors, and deduplicates by `(DEX namespace, tid)`. Mixed-DEX REST results are limited by fill timestamp so default-Dex volume cannot hide recent HIP-3 fills. The dashboard consumer uses the same namespaced dedup key. This preserves the existing snapshot-warm behavior: WS snapshots seed cursors but are not emitted as new trade events.

The position reconciler also queries `user_state(address, dex=...)` for the default DEX and every discovered HIP-3 DEX, with independent 5-second caches. This is required for open positions such as `xyz:TSM`, `xyz:MU`, `xyz:SNDK`, `xyz:SKHX`, and `xyz:DRAM`; the default `user_state(address)` response does not include them. Leverage lookup follows the market's DEX namespace as well.
