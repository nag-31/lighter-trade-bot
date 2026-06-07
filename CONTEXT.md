# CONTEXT — lighter-trade-bot working memory

> Living context file. Survives chat compaction. Update it when decisions change.
> Narrative history lives in [BUILD_STORY.md](BUILD_STORY.md); this file is the
> "where we are right now + the rules that must never be broken" snapshot.

Last updated: 2026-06-07

---

## Issues raised & how they were resolved (chronological)

Every problem reported during the build, newest at the bottom. Status: ✅ fixed,
🟡 mitigated/by-design, 🔲 open.

1. ✅ **Binance blocked from Azure (HTTP 451 geo-block).** → Rotating `ProxyPool`
   (round-robin + 5-min cooldown + failover), fail-safe so HL/Lighter keep
   running if Binance is down, `/health` page. (Binance source currently
   commented out in config.)
2. ✅ **HL wallet must never leak.** → Address in `.env` only, masked `0x…4c74`,
   source id = sha256 hash. (See SECURITY CONSTRAINTS.)
3. ✅ **Want price privacy without lying about PnL.** → "Exact results, fuzzy
   fingerprint": PnL/%/win-rate exact, price/size/notional/time fuzzed (HL-only),
   master rollback switch `privacy_enabled`, disclosure footnote.
4. ✅ **Multi-fill close only logged the LAST fill — scale-out PnL lost.** →
   One record per realization (every reduce-batch + close writes its own row);
   later collapsed for display into one card per round-trip.
5. ✅ **Reconciliation parse bug: 37 coins collapsed to 1** (`_parse_fill` used a
   map mutated mid-iteration). → stable `_perp_universe` set; script must
   `bootstrap_markets()` first. Also fixed `limit=2000` truncation.
6. ✅ **Stats should use closed trades only** (not open/unrealized). → enforced.
7. ✅ **Date window: show only from a cutoff (e.g. last 4 days → June 1); end
   = now.** → `stats_start_date` / `stats_end_date` config.
8. ✅ **Don't import full history; show only existing dashboard coins.** →
   scoped view + `stats_symbols` whitelist; `--apply` never run.
9. ✅ **Sort trades by date; equity x-axis by date; PnL-per-trade show ticker.**
10. ✅ **One PnL card per fully-closed trade (not per scale-out).** →
    display-layer round-trip aggregation; in-progress positions shown as tiles.
11. ✅ **Ticker labels not showing on the chart.** → was browser cache; added
    `Cache-Control: no-cache` so deploys are picked up.
12. ✅ **PnL showed $820 instead of ~$2k.** → legacy `None`-kind rows were
    treated as never-closing and dropped from stats; now any non-`PARTIAL` row
    closes a round-trip.
13. ✅ **FARTCOIN must be fully excluded from PnL.** → dropped at the display
    layer too (`filter_trades(exclude_symbols=…)`), not just at record time.
14. ✅ **Closed-then-reopened ticker shouldn't show the old one as in-progress.**
    → a full close = a distinct trade; the reopen is a new round-trip.
15. ✅ **Put the dashboard on a real domain, not a random link.** →
    `https://dashboard.enkapital.xyz` via Caddy + Let's Encrypt; GoDaddy A record;
    Azure NSG 80/443 opened via CLI.
16. ✅ **Domain loaded but stuck "connecting · no data".** → frontend opened
    `ws://` from an HTTPS page (blocked as mixed content); now picks `wss://`
    by page scheme.
17. 🟡 **Lighter WebSocket HTTP 400 reconnect loop.** → it's a Lighter
    *geo-block* on the `/stream` WS endpoint (REST stays open). Made it warn once
    + back off 300s; REST poll covers all trades (≤60s delay). Real-time needs a
    `LIGHTER_WS_PROXY` in an allowed region (not yet provided).
18. ✅ **Remove the privacy footnote text.** → `privacy_disclose_footnote: false`.
19. ✅ **"All fucked up" — PnL showed −$393; old trades' PnL bleeding in.** →
    date filter ran AFTER aggregation, so a trade that closed in-window but opened
    pre-cutoff dragged its old fills in. Now fills are filtered by the cutoff
    FIRST, then round-trips are built. Verified live: **+$2,080.91 / 23 closed
    trades / 56.5% win rate** for June 1+.
20. ✅ **TON duplicate — and 5 more like it.** Audit confirmed the TON +$42 was
    one of **8 legacy `None`-kind rows** (ids 1–8, written in the 06-03/06-04
    migration window, `trade_id`/`fill_ids` NULL). 6 of them (SOL +397.71,
    TON +42, FET +148.85, LIT +369.11, WLD +115.09, LIT +236.42) restate a
    real PARTIAL/FULL fill sequence at the same instant → **+$1,448 double-count**.
    The dashboard's June-1+ figure was inflated from a true ~+$496 to +$1,944.
    → `stats.aggregate_round_trips()` now calls `_drop_legacy_duplicate_rows()`:
    a legacy NULL row is dropped when a fill-based row for the same
    (source, symbol) exists within `_LEGACY_DEDUP_WINDOW_SEC` (1h). Pure
    display-layer (no DB writes). ENA (+165.01) and SPX (−25.93) have **no**
    fill counterpart → kept as genuine standalone legacy round-trips
    (**flagged for manual eyeball**). Also noted: stale baseline — DB now holds
    564 rows, all-history grand total is actually **−$1,885.64** (driven by a
    real **ZEC −$1,267** loss); no `fill_id` is reused across rows; `is_win`
    vs `pnl` sign is clean.
21. ✅ **Telegram alerts too spammy (~79/day, peak 134/day, all from HL).**
    Audit: 56% of HL alerts were small scale-in/scale-out (`added`/`reduced`).
    Root cause: the $900 gate was on the *standing position* size, so a small
    add to a large position still alerted. → Raised min size to **$1000** and
    moved the gate onto the **aggregated fill notional after the 60s window**:
    an add/reduce alert now fires only if the net added/reduced across the
    window ≥ $1000 (`flush_aggregate` gates on `net_added`, `flush_reduce_aggregate`
    on `net_reduced`). Sub-$1000 reduces are **still recorded** (PnL stays
    exact) — only the Telegram message is suppressed.

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
- **One card per fully-closed trade (display-layer aggregation)**: the recorder
  still writes per-fill rows (above), but the dashboard collapses each
  *round-trip* — a coin's PARTIAL rows + its final FULL — into ONE grid tile +
  chart bar + stats entry via `stats.aggregate_round_trips()`. A completed trade
  shows the TOTAL of every scale-out + the close; a still-open scaled-out
  position shows as an `OPEN` (· IN PROGRESS) tile with realized-so-far and is
  EXCLUDED from closed-only stats. No schema change, no double counting (each
  source row keeps its own-fill pnl; aggregation sums once). The FULL close card
  IMAGE shows the round-trip total via `record_realization(card_pnl_override=…)`
  (`_roundtrip_partial_pnl()` sums the round-trip's partials; restart-safe).
  Silent-close backstop marks intermediate fills PARTIAL + last FULL so it
  collapses to one trade. `display_trades(include_open)` is the single view
  feeding both grid and stats.
  - A round-trip ends on ANY non-`PARTIAL` row (FULL, legacy `None`, unknown) —
    only an explicit `PARTIAL` keeps it open. (Bug fixed 2026-06-04: treating
    `None`-kind legacy rows as non-terminating fused closed trades + reopens
    into one perpetual "in progress" blob and dropped ~$1.4k from stats.)
  - Excluded symbols (e.g. FARTCOIN via `exclude_symbols`) are dropped from the
    grid AND stats at the display layer (`filter_trades(exclude_symbols=…)`),
    not just at record time.
  - **ORDER: filter fills by the cutoff window FIRST, then aggregate** (bug fixed
    2026-06-04). Aggregating first and date-filtering after dragged a trade's
    pre-cutoff fills into the window whenever its close landed after the cutoff
    (bled old May PnL into June — showed −$393 instead of +$2,081). Now
    `display_trades` calls `filter_trades(closed_trades, …)` then
    `aggregate_round_trips(rows)`, so everything before the cutoff is literally
    disregarded fill-by-fill. A complete close = position flat = a distinct
    trade; a reopen of the same ticker is a brand-new round-trip (never summed). Dashboard index sends `Cache-Control: no-cache`
  so browsers pick up new frontend JS after a deploy.
- **Stats computed on CLOSED trades only** (rows with non-None pnl).
- **Binance**: rotating `ProxyPool` (round-robin + cooldown + failover) to bypass
  the Azure-region geo-block; fail-safe so HL/Lighter keep running if Binance is
  down, with a `/health` page. Currently the Binance source is commented out.
- **Lighter WS geo-block**: Lighter's `/stream` WS endpoint is geo-blocked from
  the Azure region (HTTP 400, code 20558 "restricted jurisdiction") while REST
  stays open — so the 60s REST poll covers all pool trades (no data lost), just
  not real-time. `stream_trades` detects the block, warns ONCE, and backs off
  `_WS_GEO_BACKOFF=300s` instead of spamming. Set `LIGHTER_WS_PROXY` in `.env`
  (a SOCKS5/HTTP proxy in an allowed region) to route ONLY the WS through it and
  restore real-time — REST stays direct.
- **Dashboard domain**: served at `https://dashboard.enkapital.xyz` via Caddy
  (auto Let's Encrypt TLS) reverse-proxying localhost:8080 on the VM. GoDaddy A
  record `dashboard → 20.29.250.158`; Azure NSG `lighter-bot-linux-nsg` opens
  80/443/8080/22. Raw `:8080` still public (can be locked down later).

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
