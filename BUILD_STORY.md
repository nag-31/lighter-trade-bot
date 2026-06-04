# The Lighter Trade Bot — A Build Story

*How a "just post my trades to Telegram" idea turned into a privacy-preserving,
self-persisting, public trade-analytics dashboard — and every wall we hit on the way.*

---

## Chapter 0 — How it started

The whole thing began with one simple wish:

> *"When I open or close a position in my Lighter pool, I want it posted to my
> Telegram channel automatically — so my followers see the signal without me
> manually typing it out."*

That was it. A watcher on a public Lighter.xyz pool → a Telegram message on every
fill. Add reasoning later as a reply. Transparency for a public pool, minus the
manual labor.

The first architecture was modest:
- **WebSocket** on the pool's trade stream as the primary feed.
- A **REST safety-net poll every 60s** to catch anything the socket missed.
- A single consumer that serializes everything and de-dupes against the last seen
  trade id.

Simple enough. Then reality — and a growing appetite for features — showed up.

---

## Chapter 1 — The trade schema fights back

Lighter doesn't hand you clean trades. The very first surprises:

- Trades have **no native `side`** — you derive long/short by comparing the pool id
  to the `ask_account_id` / `bid_account_id` on each fill.
- **No `leverage`** on the trade — you fetch it separately from the positions snapshot.
- **No `reduce_only`** flag at all — it simply isn't exposed.

So "just read the trade" became "reconstruct the trade from three different places."
This is the kind of thing nobody warns you about until you're staring at a fill that
refuses to tell you whether it opened or closed a position.

---

## Chapter 2 — The ghost alert: "opened" *after* I closed

Then the bug that started one of the longest investigations:

> *"I just checked — the OPEN alert came in **after** I already closed the position."*

It looked like a simple ordering bug. It wasn't. The root cause was subtle and very
Lighter-specific: the reconciler's `current_positions()` reads the `/account`
endpoint, which **lags the `/trades` feed by 30–90 seconds** because of ZK-rollup
settlement. An earlier "optimization" had used that lagging `/account` snapshot to
*suppress* alerts — so it was throwing away **legitimate** OPEN alerts whose fills
had already arrived on the faster `/trades` feed.

The lesson, in the user's words:

> *"The code we did 2 days ago was fine and better than the current one. Don't code
> before actually analyzing the code and the edge cases, then make a plan."*

Fix: **trust the fill.** The `/trades` feed is the fresher truth for OPEN detection;
never suppress an OPEN based on the slow `/account` snapshot. Sometimes the best fix
is deleting the clever thing you added.

---

## Chapter 3 — Small power-ups

A run of quality-of-life additions:

- **Hide tickers you don't want public.** Per-source `exclude_symbols` in config —
  no dashboard row, no alert, nothing. First use: hide `FARTCOIN` from the HL feed.
  Matching is normalized (case-insensitive, strips USDT/USDC/USD/-PERP).
- **A hard security line.** *"Don't expose the wallet — last 4 digits only if you
  absolutely must."* So the HL address lives in `.env` only, logs mask it as
  `0x…4c74`, the source id uses a SHA-256 hash instead of the raw address, and card
  filenames use the source *name*, never the address.

This security instinct turned out to be foreshadowing.

---

## Chapter 4 — From "alerts" to a real dashboard

The ask grew:

> *"I want SL/TP in the alerts. And make the dashboard reactive — left side: positions
> on top, Telegram alerts on the bottom; right side: the event feed. And a tracker
> that shows my previous PnL cards and the last N trades with all their details."*

So the dashboard became a live, WebSocket-driven cockpit:
- Left column: open positions (top) + the exact Telegram alerts the bot sent (bottom).
- Right column: the raw event feed.
- A history grid of closed trades, each as a PnL card with entry/exit/notional/etc.

SL/TP had its own wrinkle: stops are placed *seconds after* the position opens, so
they aren't known at fill time. The bot arms a one-time "🛡 SL/TP set" alert that
fires when they first appear — without flooding you for positions that already had them.

---

## Chapter 5 — Analytics, and a peek at the future

> *"Use a variable for the number of trades. Add nice charts on the trades taken.
> Research how dashboards show trade insights and build it in. And make sure I can
> relay it to Telegram. In the future I want a bot anyone in a group can use with
> commands."*

This produced a deliberately reusable design:
- **`stats.py`** — a channel-agnostic `compute_stats()` pure function. The dashboard,
  the Telegram relay, and the (future) group-command bot all call the *same* builder.
- A **Chart.js analytics section**: KPI cards, equity curve, PnL-per-trade bars,
  PnL-by-symbol bars, a win/loss donut — all re-rendering on every WS update.
- A **"Send stats to Telegram"** button → `POST /api/send_stats`.
- A documented (not-yet-built) **group-command bot** vision (`/stats`, `/pnl`, …),
  with the architecture already shaped so it drops in later.

The key call: make the stats engine channel-agnostic *now*, so the future is cheap.

---

## Chapter 6 — Make it remember

> *"Keep a persistent DB for all data — TG alerts, trades, everything. Across sessions
> and restarts. It's important."*

Trades and events already persisted to SQLite; the gap was the **Telegram alert log**,
which was in-memory only. So a `tg_alerts` table joined `events` and `closed_trades` —
now the whole picture survives a restart. (SQLite calls wrapped in `asyncio.to_thread`
so they never stall the event loop.)

---

## Chapter 7 — The privacy saga (the big one)

Then the most interesting request of the whole build:

> *"For the HL trades I want to add a little rounding error. Think about how I can do
> it **without being dishonest**. I don't want people tracking back to my wallet —
> it's public and on-chain. Maybe add ~0.1% randomness to prices. But PnL should
> still match. Research, plan, review the plan, and if it achieves the goal, do it."*

This was run as a proper **research → plan → adversarial-review workflow** before a
single line was written. And the adversary earned its keep — it found the trap:

> Price jitter alone is **cosmetic**. If you keep notional and realized PnL exact,
> an on-chain observer just matches on **exact notional + exact PnL + timing** and
> finds your wallet anyway. The ±0.1% on price is decorative against that.

That reframed everything. The honest, *actually-private* answer wasn't "fuzz the
price" — it was a clean separation:

- **Be exact about results** — PnL $, %, win-rate, win/loss stay 100% real and honest.
- **Be approximate about the fingerprint** — price (±0.3% HMAC-seeded jitter), size and
  notional (sig-fig rounded), and timestamps (coarsened). These are what dox you.

Chosen posture: **"exact results, fuzzy fingerprint."** Plus:
- A **master switch** (`privacy_enabled`) for instant rollback to real values.
- A **secret salt** (`PRIVACY_SECRET_KEY`, env-only) — because the fuzzing code is
  open-source on GitHub, the secret is the *only* thing that makes the jitter
  irreversible (same principle as encryption: public cipher, private key).
- HL-only — Lighter is never touched.

### The three bugs the review (and follow-up checks) caught

1. **Seed inconsistency.** The Telegram formatter computed its jitter factor with
   `source_id=""` while the dashboard used the real `src.id`. Same position →
   *different* fuzzed price in the alert vs. the dashboard. Deterministic seeding only
   works if *every* surface uses the *same* seed. Fixed + locked with a regression test.
2. **The restart leak.** Display values were computed in-memory at close time only.
   After a restart, the history grid loaded the real DB rows and the JS fallback would
   render **real HL prices** — publicly. Fixed by re-deriving the fuzz on load.
3. **Drifting anchor.** The factor was seeded off the *live average entry*, which moves
   as you scale into a position — so a trade could look different at open vs. close.
   Fixed by **freezing the factor at open.**

Throughout: **internal logic always uses real values** (reconciliation, dedup, SL/TP
matching, the DB columns, stats). The fuzz is applied only at the very last inch
before a human sees it.

---

## Chapter 8 — Open orders, and "notional everywhere"

> *"Show open orders in the dashboard, with a toggle to enable/disable it. And always
> show notional position everywhere — not size in coins."*

- A new **Open orders** panel: resting/limit orders per source, a localStorage toggle
  to show/hide, a green **flash** when a new order appears, a pulse when re-enabled.
  HL order prices run through the same privacy factor (and **fail closed** — null price
  on any transform error, never leak).
- **Notional everywhere.** Coin size is *itself* a fingerprint, so it was removed from
  every surface — events, open orders, history tiles, PnL cards, Telegram text — and
  replaced with rounded USD notional. PnL and % stay exact.

---

## Chapter 9 — "It's public on purpose"

A security note surfaced: server logs showed internet scanners probing the dashboard
(`/actuator/env`, `/dump.sql` — generic vulnerability scans, all harmless 404s). They
proved port 8080 is open to the world. The recommendation was to firewall it — but:

> *"I want it visible by anyone — it's a public dashboard."*

Which is fine, and actually *validates* the privacy work (a public board is exactly why
fuzzing the wallet's prices matters). But it surfaced one genuine hole: `/api/send_stats`
was an **unauthenticated POST** — anyone could spam the Telegram channel by hitting it.

> *"Rate limit it to 1 per 1 hour."*

Done — a global (not per-IP, since attackers rotate IPs) 1/hour gate that reserves the
slot *before* sending, so a burst of concurrent calls can't all slip through.

---

## The war stories (issues faced along the way)

Every build has its scars. This one's:

- **`UnboundLocalError` on deploy.** A state set was *declared* after the bootstrap loop
  that *used* it. The unit tests passed because they never execute the real startup path
  — it only blew up at runtime on the VM. Lesson: mirror-logic tests don't catch
  ordering bugs in the actual boot sequence.
- **The "Already up to date" lie.** A deploy reported success but ran old code — because
  the merge happened against a *stale* ref. Fix: **always `git fetch` before
  `merge --ff-only`.** Now every deploy verifies the new code is physically on the box
  before restarting.
- **Divergent branches on the VM.** The VM had local config edits and no pull strategy.
  Resolved by making the **repo the single source of truth** — the VM discards local
  edits on every deploy (`git checkout -- .` then fast-forward).
- **Hitting a session/rate limit mid-build.** A coding agent died before it ran. Adapted
  by tiering work to model complexity — trivial config edits done inline, real logic
  delegated to Sonnet agents (often in parallel on disjoint files), verification run
  directly.
- **Lighter WebSocket `HTTP 400`.** A pre-existing, Lighter-side handshake rejection that
  predates this work. The bot degrades gracefully — the 60s REST poll still catches every
  Lighter trade, just a touch slower. HL streams in real time.
- **Binance: blocked from Azure.** Binance returns **HTTP 451 (geo-block)** from the
  Azure region the VM lives in, so that source stays disabled until it's routed through
  a SOCKS5 proxy in an allowed jurisdiction (the code already supports `SOCKS_PROXY_URL`).

---

## Where it stands now

What began as "post my trades to Telegram" is now:

- A live, public, WebSocket-driven **trade dashboard** (positions, events, alerts,
  open orders, full analytics).
- **Privacy-preserving** HL display — honest about performance, opaque about the
  wallet fingerprint, reversible only with a secret you alone hold, and rollback-able
  with one switch.
- **Fully persistent** — trades, events, alerts, closed-trade history and stats all
  survive restarts, with one-time backfill from history.
- **Hardened** for public exposure — masked addresses, no secrets served, a
  rate-limited action endpoint.
- Backed by **500+ passing tests** and a repo-is-source-of-truth deploy.

And a clear runway for the next chapter: the group-command Telegram bot, already
designed into the architecture, waiting for its turn.

*Built incrementally, one honest fix at a time.*
