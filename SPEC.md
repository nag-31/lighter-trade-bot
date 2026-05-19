# Lighter Trade Auto-Poster — Spec v1

## Goal
Watch a Lighter.xyz public pool. When a qualifying trade event happens, post identical messages to a public Telegram channel and a Twitter account. Owner adds reasoning manually as a Telegram reply.

## Triggers
- Position opened
- Position size changed (add / partial reduce)
- Position closed

## Message content (same on both channels)
- Asset + direction (long/short)
- Entry price + size
- Leverage
- Link back to Lighter pool
- Chart image (source TBD — see open questions)

## Filters
- Skip trades with notional < $1,000

## Latency
- WebSocket primary (`account_all_trades/<pool>` channel — push, no auth for public pools).
- REST safety-net poll every 60s as backstop.
- Single consumer goroutine serializes both streams against `state.last_trade_id` for dedup.

## Channels
- Telegram: one public channel
- Twitter: main account, Free tier (~17 posts/day)
- Manual reasoning: owner replies in Telegram; Twitter post untouched

## Reliability
- Auto-restart on crash (systemd / platform equivalent)
- External watchdog DMs owner on Telegram if bot dies / loses Lighter connection
- On restart: replay qualifying trades missed during downtime (last-seen trade ID on disk)
- Manual kill-switch: `/pause` and `/resume` DM commands to bot

## Recaps
- Daily P&L summary (end of UTC day)
- Weekly P&L summary (Sunday evening UTC)
- Both posted to Telegram + Twitter

## Volume planning
- Variable. Design queue + spacing for Twitter Free tier (~17/day cap).

## Hosting
- TBD post-build (likely Railway or $5 VPS).

## Credentials needed
- Lighter API key + public pool ID
- Telegram bot token + channel ID + owner user ID (for DM alerts)
- Twitter Free-tier app credentials (4 values)
- Chart source credential (depends on chart-API decision)

## Lighter API notes (verified)
- Pip package: `lighter-sdk` (import `lighter`). We use raw httpx + websockets directly; SDK is optional drop-in.
- Public-pool reads need NO auth.
- REST base: `https://mainnet.zklighter.elliot.ai/api/v1`. WS: `wss://mainnet.zklighter.elliot.ai/stream`.
- Trade record has no native `side`, `leverage`, or `reduce_only` field.
  - Side = compare pool_id to `ask_account_id` / `bid_account_id`.
  - Leverage = fetched from account positions snapshot at event time.
  - Reduce_only = not surfaced.

## Chart source (resolved)
- Tree of Alpha: dead end (no public chart-snapshot API).
- chart-img.com: BASIC tier free with watermark; pricing for higher tiers behind login.
- Symbol map: Lighter "BTC" → chart-img "BINANCE:BTCUSDT" via configurable template + per-market overrides.

## Open risks
1. Lighter REST positions schema field names — extraction is defensive (.get with multiple keys + fallbacks). May need a one-line fix on first real run.
2. Twitter Free tier cap (~17/day) hits hard with active trading + recap posts. Soft cap of 15/day enforced; bot logs + skips beyond that. Upgrade to Basic ($100/mo) if it bites.
3. Watchdog must be external (UptimeRobot → /healthz on :8080).
