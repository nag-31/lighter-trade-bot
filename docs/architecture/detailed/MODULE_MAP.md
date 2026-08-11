# Module map — every file that matters

File-level responsibilities and the contracts between them. This is the
agent's primary navigation aid. Line references are approximate anchors.

## Trade Tracker (`src/`)

### Entry & orchestration

| File | Role | Notes |
| --- | --- | --- |
| `dashboard.py` | The whole live service (aiohttp + WS + reconciler + Telegram + dashboard HTML). ~5200 lines. | Single `_run()` coroutine owns all state. Entry point `python -m src.dashboard`. |
| `main.py` | Older lighter-only bot (`python -m src.main`). | Separate from dashboard; **not** the production service. |
| `supervisor.py` | `supervise()` — crash-isolate one source task with bounded backoff. | Restarts a task; never lets one source kill siblings. |
| `healthz.py` | `Healthz` — tiny `/healthz` app, 503 if main loop stale. | 200 only if ticked recently. |
| `health.py` | `HealthRegistry` — component up/down/degraded/disabled registry. | `ok` = no down/degraded. |
| `state.py` | `State` — paused flag, twitter daily counter, atomic JSON save. | Crash-safe writes via temp file + `os.replace`. |
| `recap.py` | `Recap` dataclass + formatter for daily/weekly recap text. | |
| `filters.py` | `passes_min_notional()` — event size gate. | |
| `result.py` | `FetchResult` — authoritative vs stale vs failure wrappers. | |
| `source_runtime.py` | `SourceRuntime` — per-source last-good positions, bootstrap-once, staleness. | Converts legacy client errors into non-authoritative results. |

### Sources & clients

| File | Role | Notes |
| --- | --- | --- |
| `sources.py` | `BotSettings`, `load_settings()`, `Source`, `SourceLoadReport`, config validation. | Wallet addresses/API keys loaded from env, never config. Source ids hashed, not raw addresses. |
| `lighter_client.py` | `LighterClient` — REST (account, trades, positions) + WS stream + account-index resolution. | WS geo-block detection (HTTP 400) → warn once + 300s backoff; REST covers all trades. |
| `hyperliquid_client.py` | `HyperliquidClient` — `user_fills`, `fetch_realizing_fills`, WS `userFills` stream, `_parse_fill`. | `closedPnl` → `Trade.realized_pnl`; per-DEX tid anchors (HIP-3); snapshot frames never yielded as events. |
| `binance_client.py` | `BinanceClient` — futures REST + WS via rotating proxy pool. | Source currently disabled (geo-block). |
| `proxy_pool.py` | `ProxyPool` — round-robin proxies, 5-min cooldown on failure, failover. | |
| `candle_provider.py` | `CandleProvider` — fetch public OHLC candles for lifecycle charts (best-effort). | Failure → `execution-only` provenance, never blocks alerts. |
| `chart_fetcher.py` | Legacy chart fetcher (older code path). | |

### Domain & accounting

| File | Role | Notes |
| --- | --- | --- |
| `types.py` | `Trade`, `Position`, `Event`, `EventKind`, `OpenOrder`, `PositionSide`. | `Trade.realized_pnl` (HL closedPnl), `start_position` (signed size before fill). |
| `position_tracker.py` | `PositionTracker` — classify a fill into OPEN / SIZE_CHANGE / REDUCE / CLOSE; weighted avg entry; flips. | Keyed by `market_id`. One tracker per source. |
| `stats.py` | `compute_stats`, `filter_trades`, `aggregate_round_trips`, `_drop_legacy_duplicate_rows`. | Fills filtered by window FIRST, then round-trips built. |
| `canonical_pnl.py` | Canonical per-account ledger, portfolio membership, projections, retractions. | Append-only `canonical_ledger_entries`; retraction rows for repairs. |
| `account_ledger.py` | Per-account immutable ledgers (`data/accounts/*.db`): `exchange_fills`, `fill_observations`, `pnl_realizations`. | `append_trade` immutable; `replace_realizations` rebuilds only derived rows. |
| `display_transform.py` | `PrivacyParams`, `price_factor`, `disp_price/size/notional/time`, `disp_view`, `footnote`. | Pure HMAC-based jitter; PnL untouched. |
| `pnl_card.py` | `generate_pnl_card()` — Pillow PNG card, win-rate bar, quotes, privacy pills. | Optional Pillow; returns None if unavailable. |
| `execution_chart.py` | `render_legacy_execution_chart()` — legacy close-card → V2 chart spec adapter → PNG. | Read-only; chart never affects accounting. |
| `stats_card.py` | `render_stats_card()` — stats summary PNG. | |

### Delivery (Telegram + web)

| File | Role | Notes |
| --- | --- | --- |
| `formatter.py` | `format_event`, `format_aggregate`, `format_reduce_aggregate`, `format_sl_tp_set`. | HTML-escaped symbols/sources; privacy factor computed once per message. |
| `telegram_poster.py` | `TelegramPoster` — low-level sendMessage/sendPhoto. | (Older bot path; dashboard has its own sender.) |
| `telegram_commands.py` | Command parsing + formatting: `/positions`, `/trades`, `/pnl`, `/stats`, `/coin`, `/leaderboard`, `/health`… | HTML escaping on all values. |
| `twitter_poster.py` | `TwitterPoster` — optional Twitter posting + daily soft cap. | |
| `notifier.py` | `Notifier` — alert wrapper. | |

### Persistence helpers

| File | Role | Notes |
| --- | --- | --- |
| `db.py` | SQLite schema + helpers: events, closed_trades, tg_alerts, source_cursors, notification_outbox, canonical tables. | All writes via `asyncio.to_thread`. |

## Reconciliation tooling (`scripts/`)

| File | Role | Notes |
| --- | --- | --- |
| `reconcile_hl_pnl.py` | One-time HL PnL rebuild from `closedPnl` fills. | Dry-run default; `--apply` backs up then scoped-deletes then re-inserts. |
| `hl_pnl_logic.py` | Pure reconstruction logic used by the reconcile script. | |
| `validate_tracker_config.py`, `merge_env.py`, `migrate_*.py`, `rebuild_*.py` | Ops helpers. | |

## Isolated V2 (`architecture_v2/`)

**Must stay inert** — no production import/write path.

| Path | Role |
| --- | --- |
| `domain/` | Pure accounting engine: `models.py`, `accounting.py`, `charts.py`, `identity.py`, `reports.py`. |
| `application/` | Use cases: `ingestion.py`, `queries.py`, `evaluations.py`, `portfolio.py`. |
| `infrastructure/` | `sqlite_store.py`, `account_ledger_store.py`, `catalog_store.py`, `rollout.py`, `verification.py`. |
| `tracker/` | `static_chart.py` (deterministic Pillow), `plotly_chart.py`. |
| `adapters/` | `runtime_trade.py` — adapts current `Trade` shape to V2 `Execution`. |
| `migrations/` | Additive SQL migrations. |
| `docs/`, `tests/` | Decisions, rollout evidence, tests. |

## Other apps

| App | Files | Port |
| --- | --- | --- |
| Trade Journal | `trade_journal/app.py`, `trade_journal/v2_consumer.py` | 8811 |
| Command Center | `command_center/app.py`, `ingest.py`, `evals.py`, `marks.py`, `store.py` | 8810 |
| App Hub | `apps_hub/access_page.py` | 8800 |
| Portfolio | `src/portfolio_app.py`, `portfolio_fetcher.py`, `portfolio_db.py`, `portfolio_defi.py` | (own) |

## Key cross-cutting contracts

1. `Trade` is the normalized fill across every exchange (always has `trade_id`,
   `market_id`, `market_symbol`, `side`, `size`, `price`).
2. `event_uid = f"{source_id}|{market_id}|{position_side}|{native_id}"` is the
   idempotency key used across `events`, `closed_trades`, and account ledgers.
3. `closed_trades` rows carry `realization_kind` = `PARTIAL` | `FULL` (or legacy
   `None` = treated as a close).
4. The dashboard **display layer** never mutates DB rows; it aggregates a
   filtered copy via `display_trades()` → `filter_trades()` →
   `aggregate_round_trips()`.
