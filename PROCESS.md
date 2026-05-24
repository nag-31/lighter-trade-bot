# Lighter Trade Bot — Process & Learning Document

> **Purpose**: Living reference for any developer or AI agent picking up this project.
> Captures architecture decisions, bugs found, fixes applied, and lessons learned
> as the bot evolves. Updated after every significant change.
>
> **Last updated**: 2026-05-24

---

## Table of Contents
1. [Project Overview](#1-project-overview)
2. [Architecture](#2-architecture)
3. [Source Types](#3-source-types)
4. [Event Pipeline](#4-event-pipeline)
5. [Config & Secrets](#5-config--secrets)
6. [VM Deployment](#6-vm-deployment)
7. [Bug Log](#7-bug-log)
8. [Known Limitations](#8-known-limitations)
9. [Changelog](#9-changelog)

---

## 1. Project Overview

A Python async bot that:
- Watches one or more trading sources (Lighter pool, Hyperliquid wallet, Binance account)
- Detects trade events (OPEN / CLOSE / SIZE_CHANGE / REDUCE)
- Posts formatted alerts to a **Telegram channel**
- Generates **PnL card images** (Pillow) for CLOSE events with win-rate bar + psychology quotes
- Serves a **local web dashboard** (aiohttp) at `http://localhost:8080/`
- Persists events to **SQLite** (`data/events.db`)
- Runs as a **systemd service** (`lighterbot`) on an Azure Ubuntu VM

Entry point: `python -m src.dashboard`

---

## 2. Architecture

```
config.yaml + .env
       │
  load_sources()          ← sources.py
       │
  Source[]  (client + tracker + url + min_notional)
       │
  ┌────┴────────────────────────────────────┐
  │  per source, three concurrent tasks:    │
  │  ws_producer()     ← real-time fills    │
  │  rest_safety_producer() ← 60s fallback  │
  │  position_reconciler()  ← 60s sync      │
  └────┬────────────────────────────────────┘
       │  asyncio.Queue[(source_id, Trade)]
  consumer()
       │
  PositionTracker.apply(trade) → Event[]
       │
  ┌────┴──────────────────────────────────────────┐
  │  per EventKind:                               │
  │  OPEN       → immediate TG alert             │
  │  CLOSE      → PnL card photo (or text)       │
  │  SIZE_CHANGE → 30s batch → one TG alert      │
  │  REDUCE     → 30s batch → one TG alert       │
  └───────────────────────────────────────────────┘
       │
  Hub.broadcast()  ← dashboard WebSocket clients
  save_event()     ← SQLite
```

### Key design rules
- **Positions always come from the API** (blockchain truth). The tracker's internal
  calculation is only used for fill-by-fill event classification. Every 60 s the
  reconciler calls `current_positions()` and seeds the tracker with real data.
- **Dedup is set-based** (`Source.seen_tids: set[int]`). Catches WS replay and
  REST/WS overlap regardless of ordering.
- **`url=""`** for HL and Binance sources — account addresses/keys must never
  appear in public Telegram messages or logs.

---

## 3. Source Types

### 3.1 Lighter (`src/lighter_client.py`)

| Item | Value |
|---|---|
| REST base | `https://mainnet.zklighter.elliot.ai/api/v1` |
| WS URL | `wss://mainnet.zklighter.elliot.ai/stream` |
| Auth | None (public pool reads) |
| WS channel | `account_all_trades/{pool_id}` |
| WS Origin header | **Required**: `Origin: https://app.lighter.xyz` — CloudFront returns 400 without it |
| trade_id | Integer from API |
| PnL per fill | Not available — pnl_card.py calculates from entry/exit prices |
| Public URL | `https://app.lighter.xyz/public-pools/{pool_id}` — safe to post in TG |

**Config:**
```yaml
- type: lighter
  name: "My NK pool"
  pool_id: 281474976684763
```

### 3.2 Hyperliquid (`src/hyperliquid_client.py`)

| Item | Value |
|---|---|
| REST base | `https://api.hyperliquid.xyz` |
| WS URL | derived from REST base by SDK |
| Auth | None (public wallet reads) |
| WS channel subscribed | `userFills` |
| **WS response channel** | **`"user"`** (NOT `"userFills"`) — critical, caused all HL silence |
| WS snapshot | `isSnapshot=true` → warms `_last_tid` + `seen_tids` but NOT yielded as events |
| trade_id | `tid` field (integer) |
| PnL per fill | `closedPnl` field → `Trade.realized_pnl` |
| Clearinghouse cache | 5 s TTL shared between `current_positions()` + `fetch_leverage()` |
| Address source | `HL_ADDRESS` env var only — never in config.yaml |
| Public URL | **None** — `url=""` enforced |

**Config:**
```yaml
- type: hyperliquid
  name: "HL"
  min_notional_usd: 10
  # address loaded from HL_ADDRESS env var
```

**`.env`:** `HL_ADDRESS=0x...`

### 3.3 Binance USDT-M Futures (`src/binance_client.py`)

| Item | Value |
|---|---|
| REST base | `https://fapi.binance.com` |
| WS URL | `wss://fstream.binance.com/ws/<listenKey>` |
| Auth | HMAC-SHA256 (`X-MBX-APIKEY` header + `signature` query param) |
| WS event | `ORDER_TRADE_UPDATE` where `o.x == "TRADE"` |
| Position side | One-way mode only (`o.ps == "BOTH"`). Hedge mode → source disabled |
| trade_id | Synthetic: `T` field (transaction time ms) — Binance `id` is per-symbol |
| PnL per fill | `o.rp` (realized profit USD) |
| listenKey | POST to get, PUT every 25 min, new key on each reconnect |
| positionRisk cache | 5 s TTL shared between `current_positions()` + `fetch_leverage()` |
| Credentials source | `BINANCE_API_KEY` + `BINANCE_API_SECRET` env vars only |
| Public URL | **None** — `url=""` enforced |
| Permissions needed | Futures read-only |

**Config:**
```yaml
- type: binance
  name: "Binance"
  min_notional_usd: 100
```

**`.env`:**
```
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
```

---

## 4. Event Pipeline

### EventKind values
| Kind | Trigger | Telegram action |
|---|---|---|
| `OPEN` | New position | Immediate alert if notional ≥ min |
| `CLOSE` | Position gone | PnL card photo (or text fallback) — always sent |
| `SIZE_CHANGE` | Same-side fill added to existing | 30 s batch → one "Added" alert |
| `REDUCE` | Opposite-side fill on existing | 30 s batch → one "Reduced" alert |

### Filters (`src/filters.py`)
- CLOSE → always passes (regardless of notional)
- REDUCE → judged by `position_before.notional_usd`
- SIZE_CHANGE → judged by `position_before.notional_usd`
- OPEN → judged by `trade.notional_usd`

### PnL card (`src/pnl_card.py`)
- Pillow-generated 920×500 PNG
- Only for CLOSE events
- Shows: source · market, LONG/SHORT · CLOSED, huge ±$PnL, % change,
  ENTRY / EXIT / SIZE / NOTIONAL row, psychology quote, win-rate bar
- Rolling 50-trade win-rate (`_score_window: deque[bool]`, resets on restart)
- 13 green quotes (discipline/execution), 13 red quotes (risk rules)
- Watermark: "NK Capital"
- Falls back to plain text if Pillow not installed

### Position reconciler (every 60 s per source)
1. Calls `current_positions()` → API truth
2. Detects positions in tracker but not in API → **silent close** → TG alert
3. Detects positions in API but not in tracker → seeds them (no alert)
4. **Always** calls `tracker.seed(actual)` — blockchain wins over local calc
5. Broadcasts fresh snapshot to dashboard WS clients

---

## 5. Config & Secrets

### `config.yaml` — safe to commit
Contains source types, names, pool_id, min_notional. No credentials.

### `.env` — **never commit**, in `.gitignore`
```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHANNEL_ID=...
HL_ADDRESS=0x...           # Hyperliquid wallet — never anywhere public
BINANCE_API_KEY=...        # Binance — never anywhere public
BINANCE_API_SECRET=...
```

### Security rules (hard requirements)
1. **HL wallet address** — `.env` only. Never in config.yaml, logs, TG messages, git
2. **Binance credentials** — `.env` only. Same rule
3. **Lighter pool URL** — public, safe to post in TG messages
4. **`url=""`** set on HL and Binance sources in `sources.py` to prevent accidental TG exposure

---

## 6. VM Deployment

**Platform**: Azure Ubuntu VM  
**Service**: `lighterbot.service` (systemd)  
**Repo path**: `~/lighter-trade-bot/`  
**Python env**: `~/lighter-trade-bot/.venv/`  
**SSH alias**: `lighter-bot`

### Update procedure
```bash
# From local machine:
git add <files> && git commit -m "..." && git push

# Then on VM:
ssh lighter-bot "cd ~/lighter-trade-bot && git pull && pip install -r requirements.txt -q && sudo systemctl restart lighterbot"

# Check logs:
ssh lighter-bot "sudo journalctl -u lighterbot -f --no-pager"
```

### Service commands
```bash
ssh lighter-bot "sudo systemctl status lighterbot"
ssh lighter-bot "sudo systemctl restart lighterbot"
ssh lighter-bot "sudo journalctl -u lighterbot -n 50 --no-pager"
```

---

## 7. Bug Log

### BUG-001 — Hyperliquid WS completely silent
**Date**: Early sessions  
**Symptom**: HL fills never reached the dashboard or Telegram  
**Root cause**: `_extract_fills()` checked `msg.get("channel") != "userFills"` but
the HL WS sends `"channel": "user"` — every message was silently dropped  
**Fix**: Accept both: `_FILLS_CHANNELS = {"userFills", "user"}`  
**File**: `src/hyperliquid_client.py`

---

### BUG-002 — HL WS snapshot discarded, fills missed at startup
**Symptom**: Fills that arrived between bot start and WS subscription were lost  
**Root cause**: `isSnapshot=true` frame was skipped entirely  
**Fix**: Snapshot warms `_last_tid` and `seen_tids` (anchors dedup) but is NOT
yielded as trade events. REST `fetch_trades_since()` gap-fills any missed fills  
**File**: `src/hyperliquid_client.py`

---

### BUG-003 — Lighter WS HTTP 400 on Azure VM
**Date**: Mid-session  
**Symptom**: Lighter WS disconnected immediately with HTTP 400 on the VM (worked locally)  
**Root cause**: Lighter's CloudFront CDN requires `Origin: https://app.lighter.xyz`
header on the WS upgrade request. The local machine browser sends it automatically;
a raw websockets connection does not  
**Fix**: Added `extra_headers={"Origin": "https://app.lighter.xyz"}` to `websockets.connect()`  
**File**: `src/lighter_client.py`  
**Later issue**: `extra_headers` was renamed to `additional_headers` in websockets >= 13.
Updated to `additional_headers`. VM runs websockets 16.0.

---

### BUG-004 — REDUCE event spam (15 TG messages for one position reduction)
**Symptom**: A large position closed via 15 small fills → 15 individual TG messages  
**Root cause**: Each fill was processed and sent as its own REDUCE alert  
**Fix**: REDUCE events now batched over a 30 s window (same as SIZE_CHANGE).
Buffer accumulates fills; one "Reduced ... across N fills" message fires after 30 s  
**Files**: `src/dashboard.py`, `src/formatter.py`

---

### BUG-005 — CLOSE alerts missing for positions closed via small REDUCEs
**Symptom**: A position closed via many small fills (each below `min_notional`) → no CLOSE alert  
**Root cause**: Each fill was classified as REDUCE, fell below the threshold filter,
and was silently dropped. The position closed without the bot ever noticing  
**Fix**: Position reconciler now detects positions that disappeared from the API and
sends a Telegram alert. Also, CLOSE events always pass the filter regardless of notional  
**File**: `src/dashboard.py` (reconciler), `src/filters.py`

---

### BUG-006 — HL wallet address exposed in Telegram messages
**Date**: Mid-session  
**Symptom**: Every HL alert included the full `0x...` wallet address as the pool URL  
**Root cause**: HL source was built with `url=f"https://.../{address}"` like Lighter  
**Fix**:
- HL source: `url=""` in `sources.py`
- Added `_footer(pool_url)` helper to `formatter.py` — returns empty string if url=""
- Reconciler close alert also respects `src.url`
- Binance source: same `url=""` rule enforced from day one  
**File**: `src/sources.py`, `src/formatter.py`, `src/dashboard.py`

---

### BUG-007 — Only 1 source running on VM (HL missing)
**Symptom**: `bot-status` showed only Lighter source after deploying both  
**Root cause**: `HL_ADDRESS` env var was missing from the VM's `.env` file  
**Fix**: `ssh lighter-bot "echo 'HL_ADDRESS=0x...' >> .env"` then restart  
**Rule**: Any source with missing required env vars is **skipped** with a warning log;
other sources continue unaffected. Check logs at startup for `[source] ... skipping`

---

### BUG-008 — `extra_headers` → `additional_headers` (websockets 16.x)
**Date**: 2026-05-24  
**Symptom**: Lighter WS silently failing with `unexpected keyword argument 'extra_headers'`  
**Root cause**: websockets 16 dropped the old parameter name entirely  
**Fix**: Renamed to `additional_headers` in `lighter_client.py`  
**Note**: After the rename, Lighter WS still gets HTTP 400 — see Limitation-001

---

### BUG-009 — Positions calculated locally (wrong)
**Symptom**: Dashboard showed stale/wrong position sizes after partial closes  
**Root cause**: Position state was computed from fills alone; any missed or out-of-order
fill corrupted the local view  
**Fix**: Reconciler always calls `tracker.seed(actual)` with API data. Tracker local
calculation is only used for fill-by-fill event classification, never for display  
**File**: `src/dashboard.py`

---

### BUG-010 — Lighter WS + Binance geo-blocked (Azure India VM)
**Date**: 2026-05-24  
**Symptom**: Lighter WS → CloudFront 400 `code 20558 "restricted jurisdiction"`;
Binance REST + WS → HTTP 451. HL unaffected.  
**Root cause**: Azure India DC IP ranges flagged by CloudFront geo-filter (financial
services IP reputation scoring) and Binance's India compliance block.  
**Fix**: SOCKS5 proxy support added per-client.

**Dante SOCKS5 server setup on VPS (Ubuntu):**
```bash
sudo apt install dante-server
sudo tee /etc/danted.conf << 'EOF'
logoutput: syslog
internal: 0.0.0.0 port = 1080
external: eth0
clientmethod: none
socksmethod: none
client pass { from: 0.0.0.0/0 to: 0.0.0.0/0 }
socks pass { from: 0.0.0.0/0 to: 0.0.0.0/0 }
EOF
sudo systemctl enable --now danted
# Lock to Azure VM IP only:
sudo ufw allow from <AZURE_VM_IP> to any port 1080
sudo ufw enable
```

**Bot side:** add to `.env`: `SOCKS_PROXY_URL=socks5h://vps_ip:1080`  
**Test:** `curl --socks5-hostname vps_ip:1080 https://api.binance.com/fapi/v1/ping`  
**Files**: `src/lighter_client.py`, `src/binance_client.py`, `src/sources.py`, `requirements.txt`

---

## 8. Known Limitations

### LIMIT-001 — Lighter WS + Binance geo-blocked on current VM
**Discovered**: 2026-05-24  
**Detail**: Azure VM IP is in a restricted jurisdiction per Lighter's CloudFront rules
(`code 20558: "restricted jurisdiction"`) and Binance's terms (`HTTP 451`).
HL is unaffected.

**Resolution**: SOCKS5 proxy support added to Lighter + Binance clients.
Set `SOCKS_PROXY_URL=socks5h://vps_ip:1080` in `.env`.
HL never uses the proxy (it works fine directly).

**VPS setup** (one-time, ~20 min):
1. Spin up Vultr/Hetzner VPS in **Singapore** (~$3.50/mo)
2. On VPS: `sudo apt install dante-server` + configure `/etc/danted.conf` (see BUG-010)
3. On Azure VM: add `SOCKS_PROXY_URL=socks5h://vps_ip:1080` to `.env`, restart bot
4. Verify: `curl --socks5-hostname vps_ip:1080 https://api.binance.com/fapi/v1/ping`

**Cloudflare WARP is NOT recommended** — WARP IPs are on CloudFront's
abuse/reputation blocklist, same block as without WARP. Avoid.

---

### LIMIT-002 — Binance uses synthetic trade_id (ms timestamp)
**Detail**: Binance `id` field is per-symbol, not global. We use `T` (transaction
time in milliseconds) as a synthetic global trade_id.  
**Risk**: Two fills on different symbols at the exact same millisecond get the same
trade_id → one is deduped away. Extremely unlikely in practice.

---

### LIMIT-003 — Binance REST gap-fill requires open position symbols
**Detail**: `fetch_trades_since()` queries only currently-open symbols. Fills for
a position that fully closed before the gap-fill runs will not be fetched.  
**Mitigation**: The reconciler detects the closed position and sends a TG alert.

---

### LIMIT-004 — Win-rate resets on bot restart
**Detail**: `_score_window` in `pnl_card.py` is in-memory only.  
**Future**: Could persist to SQLite if needed.

---

### LIMIT-005 — Lighter per-fill PnL not available
**Detail**: Lighter doesn't expose realized PnL per fill. The PnL card calculates
it from `(exit_price - entry_price) × size` using the `position_before` snapshot.
This may differ slightly from exchange-calculated PnL due to fee deduction.

---

## 9. Changelog

| Date | Change | Files |
|---|---|---|
| Early | Initial Lighter pool tracker + Telegram alerts | `lighter_client.py`, `dashboard.py` |
| Early | Add REDUCE EventKind, REDUCE/SIZE_CHANGE 30s batching | `types.py`, `position_tracker.py`, `dashboard.py` |
| Early | Add HL wallet tracking (no auth, public reads) | `hyperliquid_client.py`, `sources.py` |
| Early | Fix HL WS silent (wrong channel name "user" not "userFills") | `hyperliquid_client.py` |
| Early | Fix HL WS snapshot — warm state only, not yielded | `hyperliquid_client.py` |
| Early | Add clearinghouseState 5s TTL cache (shared pos + leverage) | `hyperliquid_client.py` |
| Early | Add set-based dedup (`seen_tids`) replacing last-id-only | `sources.py`, `dashboard.py` |
| Early | Fix filters: CLOSE always passes, REDUCE by position_before notional | `filters.py` |
| Early | Fix HL wallet address leaking into TG (url="" + _footer helper) | `sources.py`, `formatter.py` |
| Early | Add position reconciler: API seeds tracker every 60s, silent-close TG | `dashboard.py` |
| Early | Add unrealized P&L + liquidation price to Position + dashboard | `types.py`, `dashboard.py` |
| Early | Add PnL card images (Pillow): 920×500 PNG sent as TG photo | `pnl_card.py`, `dashboard.py` |
| Early | Add psychology quotes + 50-trade rolling win-rate bar to cards | `pnl_card.py` |
| 2026-05-24 | Fix Lighter WS HTTP 400 on Azure: add Origin header | `lighter_client.py` |
| 2026-05-24 | Rewrite HL client: anchor tid, set_anchor(), correct channel names | `hyperliquid_client.py` |
| 2026-05-24 | Add Binance USDT-M Futures source | `binance_client.py`, `sources.py`, `config.yaml` |
| 2026-05-24 | Fix `extra_headers` → `additional_headers` for websockets 16.x | `lighter_client.py` |
| 2026-05-24 | Discover geo-blocking: Lighter WS + Binance blocked on Azure VM | — (no code change) |
| 2026-05-24 | Add SOCKS5 proxy support (geo-unblock Lighter WS + Binance) | `lighter_client.py`, `binance_client.py`, `sources.py`, `requirements.txt` |
