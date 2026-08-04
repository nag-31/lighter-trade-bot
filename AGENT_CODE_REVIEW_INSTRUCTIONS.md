# Agent Instructions: Bug Hunt, Testing, and Code Review

Use this file when asking an agent to review the Crypto Scientist codebase.

## Mission

Find real bugs, missing tests, unsafe behavior, regressions, and deployment
risks. Do not rewrite working code just to make it look different. Prefer small,
reproducible findings with a failing test or a precise reason.

## What this project is

Crypto Scientist is a group of separate trading applications. The main
production service is the Trade Tracker:

`exchange fills -> position tracker -> realization/PnL record -> Telegram alert`

Other services are deliberately separate:

- Trade Journal: lifecycle review, fills, notes, and live/realized PnL;
- Command Center: signals, hypotheses, and research outcomes;
- App Hub: links and health checks only;
- portfolio applications: account and asset views.

The production VM is authoritative. Local databases are snapshots, not a
second production state.

## Recent work to review

### PnL-card chart integration

- `src/pnl_card.py` creates the existing PnL card PNG.
- `src/execution_chart.py` adapts legacy close-card inputs into the isolated V2
  chart specification and renders an execution-only PNG.
- `src/dashboard.py` creates a Telegram media group containing the PnL card and
  execution chart for full closes.
- If chart creation or Telegram album delivery fails, the existing card/text
  fallback must still deliver the trade alert.
- The current chart uses real execution times and BUY/SELL markers. It does not
  yet use live market candles.
- Chart code must not change accounting, orders, balances, or stored PnL.

### V2 architecture

`architecture_v2/` is an isolated accounting foundation, not a replacement for
production accounting. It contains immutable executions, lifecycle projection,
realization-fill PnL, lifecycle-close reporting, additive storage, and a
deterministic Pillow chart renderer.

V2 must remain read-only/inert until an explicit cutover is approved. Do not add
production migrations or consumer activation during a review.

### Current isolated V2 boundary (2026-08-04)

The isolated V2 implementation now includes:

- `architecture_v2/infrastructure/account_ledger_store.py`: one append-only
  exchange-fact SQLite ledger per account;
- `architecture_v2/infrastructure/catalog_store.py`: central account identity,
  independent ingestion/alert/portfolio/historical flags, label history, and
  auditable account-state transitions;
- `architecture_v2/application/ingestion.py`: ledger-first write coordination,
  drift audit, and projection repair;
- `architecture_v2/domain/policy.py`: `ProjectionWindow` with the fixed
  `2026-06-01T00:00:00Z` report boundary and LIVE/BACKFILL/REPAIR/SHADOW policy;
- `architecture_v2/domain/projections.py` and `sqlite_store.py`: deterministic
  input/projection hashes, immutable versioned run manifests, and immutable
  persisted shadow evidence;
- `application/read_models.py`: read-only Dashboard and Journal snapshots;
- `infrastructure/verification.py` and `rollout.py`: backup/restore integrity
  checks and non-activating rollout gates.

Reviewers must verify that account labels never become identities, raw fills are
never rewritten, pre-cutoff context cannot become report PnL, and BACKFILL,
REPAIR, and SHADOW runs create zero notification events. The catalog currently
records `HL Swing Wallet` and keeps `My NK pool` historically visible but
excluded from ingestion, alerts, and portfolio totals.

Production remains authoritative. This V2 boundary is local/inert until an
explicit cutover approval; review work must not migrate production databases or
enable consumers.

## First files to inspect

1. `src/dashboard.py` — event handling, realization recording, Telegram
   delivery, retries, outbox, and fallback behavior.
2. `src/execution_chart.py` — legacy-to-V2 adapter, timestamps, side semantics,
   quantities, PnL, and execution identity.
3. `src/pnl_card.py` — existing card calculation and rendering.
4. `src/db.py` — event/realization persistence and notification idempotency.
5. `src/types.py` — Trade, Position, Event, and side meanings.
6. `architecture_v2/domain/charts.py` and
   `architecture_v2/tracker/static_chart.py` — chart contract and renderer.
7. `MASTER_AGENT_LOG.md` — exact deployment/test history.
8. `MASTER_STORYBOOK.md` — short plain-English product explanation.

## Required review questions

### Accounting correctness

- Can a partial close be counted twice when the final close arrives?
- Does the chart PnL agree with the PnL card and stored realization total?
- Are long and short sides correct? Long opens BUY/closes SELL; short opens
  SELL/closes BUY.
- Are scale-ins, scale-outs, reversals, and same-timestamp fills ordered safely?
- Can an unknown PnL value silently become a false zero?
- Can restart/reconnect/backfill create duplicate alerts or duplicate rows?
- Can a projection failure lose the raw fill, or can drift remain invisible?
- Can repair introduce an execution absent from the immutable account ledger?
- Can a retry overwrite a projection manifest or classified shadow comparison?
- Does every account-state change retain who changed it, why, and old/new values?

### Telegram and outbox correctness

- Is the media-group request valid for Telegram's multipart API?
- Does HTML escaping remain correct in captions?
- Does a failed album send exactly one safe card/text fallback?
- Can retries or concurrent senders create duplicates?
- Are notification states (`pending`, `sent`, `failed`) left recoverable?
- Are captions, filenames, logs, and errors free of secrets and private wallet
  addresses?

### Chart correctness

- Does every marker belong to the selected lifecycle only?
- Are marker side, action label, quantity, VWAP, and time accurate?
- Does the chart clearly say `execution-only` when candles are unavailable?
- Does missing or malformed legacy data fail safely without blocking the card?
- Is chart generation deterministic for identical inputs?
- Are large lifecycles bounded so chart generation cannot stall the event loop?

### Service and deployment safety

- Does the code compile on Linux/Python 3.12, the VM runtime?
- Do all four services remain active after restart?
- Do these endpoints return HTTP 200?
  - `http://127.0.0.1:8080/healthz`
  - `http://127.0.0.1:8810/health`
  - `http://127.0.0.1:8811/health`
  - `http://127.0.0.1:8800/api/status`
- Are database integrity checks still `ok`?
- Is every deployment backup-first and reversible?
- Are no migrations or V2 consumer cutovers introduced accidentally?

## Test commands

From the repository root on Windows:

```powershell
& .\.venv\Scripts\python.exe -m pytest -q `
  --basetemp data\pytest-tmp\agent-review
```

Focused chart/alert tests:

```powershell
& .\.venv\Scripts\python.exe -m pytest -q `
  tests\test_execution_chart.py `
  tests\test_tg_alerts.py `
  tests\test_dashboard_contract.py `
  architecture_v2\tests\test_static_chart_renderer.py `
  architecture_v2\tests\test_trade_chart_spec.py `
  --basetemp data\pytest-tmp\agent-review-focused
```

Also run a syntax check:

```powershell
& .\.venv\Scripts\python.exe -m py_compile `
  src\dashboard.py src\execution_chart.py
```

Do not use the system Python if it lacks pytest. Use the repository `.venv`.

## How to report findings

Report findings before general comments, ordered by severity:

```text
[P0/P1/P2/P3] Short bug title
File: path/to/file.py:line
Impact: what can go wrong in production
Evidence: test, trace, or exact code path
Reproduction: smallest reliable command or input
Suggested fix: concise direction, not a speculative rewrite
```

Then report:

- tests run and exact pass/fail counts;
- warnings or environment limitations;
- files changed, if the user asked for fixes;
- remaining risks and recommended next step.

## Boundaries

- Never expose Telegram tokens, API keys, wallet addresses, or private IDs.
- Never run a live trade, migration, destructive cleanup, or production deploy
  merely because you found a bug.
- Do not modify production while reviewing unless the user explicitly asks for
  a fix/deployment.
- Keep `MASTER_AGENT_LOG.md` updated after meaningful review or deployment work.
- Keep `MASTER_STORYBOOK.md` understandable to a non-technical reader.
