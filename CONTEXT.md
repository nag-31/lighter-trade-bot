# CONTEXT — lighter-trade-bot working memory

> Living context file. Survives chat compaction. Update it when decisions change.
> Narrative history lives in [BUILD_STORY.md](BUILD_STORY.md); this file is the
> "where we are right now + the rules that must never be broken" snapshot.

Last updated: 2026-06-04

---

## What this is

A Python bot that watches a **Lighter pool** + a **Hyperliquid (HL) wallet** and:
- posts fills to **Telegram** (open / close / scale-in / scale-out cards),
- serves a **public web dashboard** (aiohttp + Chart.js + SQLite) with live
  positions, closed-trades grid, analytics, open orders, and a health page.

Source of truth = this repo. The VM deploys via
`git fetch origin && git checkout -- . && git merge --ff-only origin/master`
then `sudo systemctl restart lighterbot` (**restart requires explicit user OK each time**).

---

## SECURITY CONSTRAINTS — never violate

- **HL wallet address** → `.env` only (`HL_ADDRESS`). Never in chat, config,
  Telegram, filenames, or logs. Masked `0x…4c74` if it ever must appear. Source
  id uses a sha256 hash of the address, never the address itself.
- **PRIVACY_SECRET_KEY** → generated on the VM, never shown in chat, env-only.
  The price/size fuzzing is reversible if this salt leaks.
- **Binance API creds** → `.env` only (`BINANCE_API_KEY` / `BINANCE_API_SECRET`),
  READ-ONLY keys. Never in config/chat.
- **footer_url** → only a public website (e.g. enkapital.xyz). Never an
  account/wallet/explorer URL.
- Restarting the live `lighterbot` service needs explicit user authorization.

---

## Key design decisions (locked)

- **Privacy posture = "exact results, fuzzy fingerprint"** (HL-only): PnL $, %,
  win-rate, win/loss stay EXACT; price/size/notional/timestamps are fuzzed.
  Master switch `privacy_enabled` (false ⇒ show 100% real). Footnote
  "prices approx · PnL exact". Frozen anchor seeded at OPEN so a scaled-into
  position looks identical open→close.
- **One record per realization**: every REDUCE-batch flush AND every full CLOSE
  writes its own `closed_trades` row + card + stats entry. Fixes the old bug
  where a multi-fill close only logged the last fill. Realized PnL from HL
  `closedPnl` or `_lighter_realized()`. Fill-level dedup via
  `closed_trades.trade_id` + `fill_ids` (JSON) + `_recorded_realizations` set
  loaded at boot.
- **Stats computed on CLOSED trades only** (rows with non-None pnl).
- **Binance**: rotating `ProxyPool` (round-robin + cooldown + failover) to bypass
  the Azure-region geo-block; fail-safe so HL/Lighter keep running if Binance is
  down, with a `/health` page. Currently the Binance source is commented out.

---

## Stats display window (the current feature)

Goal: **do NOT import the full HL history yet.** Show only the coins already in
the dashboard, scoped to a configurable date window.

`settings:` knobs in `config.yaml` (all optional, safe defaults):
- `stats_start_date` — ISO date/datetime ("2026-05-01" or "...T00:00:00Z").
  Only trades on/after this show. `null` ⇒ no lower bound.
- `stats_end_date` — `null` ⇒ now (current date/time). A date-only value is
  treated as inclusive end-of-day.
- `stats_symbols` — whitelist of tickers. Empty ⇒ all coins currently in the DB
  (which today = only the existing dashboard coins, since no history import was
  done). Populate this list to lock the view after any future import.

Plumbing: `BotSettings` (src/sources.py) → `compute_stats` is fed a filtered +
date-sorted view via `stats.filter_trades(...)`. The same filtered view backs the
closed-trades grid in `snapshot_payload`. Frontend: equity-curve x-axis labelled
by date, PnL-per-trade bars labelled by ticker, grid sorted newest-first by date.

---

## Reconciliation (one-time HL correction sweep) — NOT applied

`scripts/reconcile_hl_pnl.py` can rebuild closed-trades PnL from HL fills.
A 365-day **dry-run** showed true realized PnL ≈ **+$75,695.67 over 6,482 fills /
37 coins**, vs **+$2,026.39 recorded (16 rows)**. **`--apply` was NEVER run** —
the user redirected to a scoped view instead of a full-history import.

Before any future `--apply`:
- Must add a `--cards-limit` (cap card regen to ~300; 6,482 cards is impractical).
- Scope the rebuild to existing coins + a from-date, not the full window.
- The script must call `bootstrap_markets()` first (a stable `_perp_universe`
  set), else the fill-parse collapses 37 coins → 1 (a blind apply would DELETE
  9 coins' history).
- FARTCOIN (+$60.76) is `exclude_symbols` on live display but appears in sweeps —
  decide whether to exclude from stats too.

---

## File map (the parts that matter)

- `config.yaml` — `settings:` block + `sources:`.
- `src/sources.py` — `BotSettings`, `load_settings()`, `Source`, `is_hyperliquid`.
- `src/stats.py` — `compute_stats()`, `filter_trades()`, `format_stats_summary()`.
- `src/dashboard.py` — aiohttp app, INDEX_HTML (Chart.js frontend),
  `record_realization()`, CLOSE handler, live silent-close backstop,
  `snapshot_payload()`, `/api/send_stats` (rate-limited 1/hr).
- `src/db.py` — `closed_trades` schema + helpers (save/load/delete/query,
  `load_recorded_fill_ids`, scoped delete by source+since).
- `src/hyperliquid_client.py` — `fetch_realizing_fills()`, `_parse_fill()`,
  `_perp_universe`.
- `scripts/reconcile_hl_pnl.py` + `scripts/hl_pnl_logic.py` — correction sweep.
- `src/display_transform.py` — privacy `disp_*`. `src/health.py`,
  `src/proxy_pool.py`, `src/types.py`, `src/pnl_card.py`.
