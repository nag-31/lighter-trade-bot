# V2 status — isolated, inert, gated

## What V2 is

`architecture_v2/` is an isolated **accounting foundation** that will eventually
replace the legacy realization logic. It contains:

- immutable executions + lifecycle projection (`domain/`)
- realization-fill PnL + lifecycle-close reporting (`domain/reports.py`)
- additive SQLite storage (`infrastructure/sqlite_store.py`,
  `account_ledger_store.py`, `catalog_store.py`)
- ledger-first ingestion + projection repair (`application/ingestion.py`)
- a deterministic Pillow chart renderer (`tracker/static_chart.py`) and an
  interactive Plotly renderer (`tracker/plotly_chart.py`)
- a runtime adapter from the current `Trade` shape (`adapters/runtime_trade.py`)

## The hard boundary (do not cross)

> **V2 must remain read-only/inert until an explicit cutover is approved.**

- V2 never writes to production databases during development or shadow runs.
- Production snapshots are opened read-only and copied into temp DBs.
- `architecture_v2/` modules do NOT import `src.dashboard`.
- Existing production modules do NOT import V2 until cutover.
- No deployment without a fresh VM backup + rollback package.
- No migration or consumer activation during a code review.

## What is already live from V2 (safe)

The **execution chart** in `src/execution_chart.py` adapts legacy close-card
inputs into a V2 `TradeChartSpec` and renders a PNG. This is a read-only
presentation plugin — it never touches accounting, orders, balances, or stored
PnL. If chart creation or Telegram album delivery fails, the PnL card/text
fallback still delivers the alert.

## Chart contract (V2 domain)

- `build_trade_chart_spec(lifecycle, projection, candles, interval, provenance)`
  → `TradeChartSpec` (versioned).
- Markers grouped by action + side + candle bucket; batched within
  `batch_threshold_seconds` (120s).
- `Candle` validates OHLC consistency; interval auto-selected to ~180 candles.
- Deterministic renderer: same inputs → same PNG bytes.
- Large lifecycles are bounded (`max_candles` cap, `to_thread` in the adapter).

## Migration roadmap (from `ARCHITECTURE_BLUEPRINT.md`)

1. Complete locally: domain contracts + invariants ✅
2. Additive normalized storage ✅ (isolated)
3. Shadow projection — NOT started
4. Consumer cutover (dashboard → Telegram → recap → journal) — NOT started
5. Journal decoupling — NOT started
6. Cleanup + deployment — NOT started

## Verification commands

```powershell
& .\.venv\Scripts\python.exe -m pytest -q architecture_v2\tests `
  --basetemp C:\Users\ADMIN\AppData\Local\Temp\opencode\pytest-v2
```
