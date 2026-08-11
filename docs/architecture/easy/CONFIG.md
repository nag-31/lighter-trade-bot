# Configuration — the knobs you turn (no code)

Everything here is edited in **`config.yaml`** and/or **`.env`**. After a
config change you must restart the service (with your OK).

## `config.yaml` → `settings:`

| Setting | What it controls | Default |
| --- | --- | --- |
| `default_min_notional_usd` | Minimum notional (USD) before a fill triggers a Telegram alert. Applied to the *aggregated* window, not the standing position. | 1000 |
| `alert_on_open/close/size_change/reduce` | Toggle each alert type. The dashboard always shows all events. | all true |
| `aggregate_window_seconds` | How long add/reduce fills are batched before one alert fires. | 60 |
| `digest_window_seconds` | Combines multiple coins' add/reduce alerts into ONE message per source. `0` = off. | 20 |
| `daily_recap_enabled` | Posts yesterday's closed-trade stats at 00:01 UTC. | true |
| `daily_self_audit_enabled` | DMs the owner if DB PnL disagrees with the exchange (> $1) per coin. | true |
| `rest_poll_seconds` | REST safety-net poll interval. | 60 |
| `reconciler_interval_seconds` | Position truth re-sync interval (catches silent closes). | 30 |
| `tg_dedup_window_seconds` | Identical messages within this window are dropped. | 90 |
| `dashboard_port` | Dashboard HTTP port. | 8080 |
| `stats_full_history` | Compute stats over ALL closed trades, not just the last-N. | true |
| `stats_start_date` / `stats_end_date` | Scope which trades show in the dashboard window. | 2026-06-01 / null |
| `stats_symbols` | Whitelist of tickers for the window view. Empty = all coins in DB. | [] |
| `privacy_enabled` | Master switch for HL display blur. `false` = show 100% real. | true |
| `privacy_mag`, `entry_quantum_pct`, `size_sigfigs`, `notional_sigfigs`, `time_bucket`, `disclose_footnote` | Tune the blur. Don't change unless you know the model. | … |
| `open_orders_enabled` | Show the open-orders panel. | true |
| `binance_proxies` | Rotating proxies to bypass Binance geo-block (Binance source currently disabled). | list |

## `config.yaml` → `sources:`

Each entry is one watched account:

```yaml
sources:
  - type: lighter
    id: lighter-wallet
    name: "Lighter Wallet"
    address_env: LIGHTER_ADDRESS     # .env var, never inline
    account_slot: 0

  - type: hyperliquid
    id: hl-main
    name: "HL Swing Wallet"
    address_env: HL_ADDRESS          # .env var, never inline
    min_notional_usd: 1000
    exclude_symbols: ["FARTCOIN"]    # hidden everywhere
    footer_url: "https://enkapital.xyz"   # public site only, never a wallet URL
```

Rules enforced in code:

- Wallet addresses / API keys come from **env vars only**, never from config.
- `id` is stable — **never rename it after deployment** (it's the identity key).
- `exclude_symbols` hides a ticker from dashboard + alerts + stats.
- Binance requires `api_key_env`/`api_secret_env` and is currently commented out.

## `.env`

Secrets live here, never in git:

| Var | For |
| --- | --- |
| `HL_ADDRESS`, `HL_ADDRESS_2` | Hyperliquid wallet addresses |
| `LIGHTER_ADDRESS` | Lighter wallet address |
| `BINANCE_API_KEY`, `BINANCE_API_SECRET` | Binance (read-only) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`, `TELEGRAM_OWNER_USER_ID` | Telegram |
| `PRIVACY_SECRET_KEY` | Salt for the HL privacy blur (keep secret) |
| `SOCKS_PROXY_URL` | Optional proxy for Lighter/Binance traffic |
| `LIGHTER_WS_PROXY` | Optional proxy for Lighter's geo-blocked WebSocket only |

> Never paste these into chat, logs, or docs. The bot masks the token in logs.
