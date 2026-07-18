# Standalone PnL Analytics Bot

This shadow bot reconstructs closed round trips from raw fills and keeps its
own database, cards, reports, and Telegram alert state under
`standalone/pnl_analytics_bot/`. It does not import or write the live dashboard
recorder, live `data/events.db`, or live card workflow.

## Accounting Rules

- Raw fills are sorted oldest-first and deduped by `(source, account, fill_id)`.
- Positions are reconstructed per `(source, account, symbol)`.
- Win rate and analytics count only fully closed round trips.
- Breakevens are non-wins for win-rate math, but they are reported separately
  from true negative-PnL losses.
- Main card percent is `net_pnl / closed_cost_basis * 100`, labelled
  `Return on cost`.
- Hyperliquid uses exchange `closedPnl` when present, then subtracts explicit
  close fees and allocated opening fees. HIP-3 / stock-style perps are preserved
  as dex-prefixed symbols such as `RWA:SKHYNIX` and should be treated like any
  other perp for PnL math.
- Lighter standard accounts use zero trading fees; premium fees can be parsed
  from fill data when provided.
- Missing Lighter funding does not block PnL calculation, but marks the round
  trip funding status as `unknown`.

## Dry Run

```powershell
python -B -m standalone.pnl_analytics_bot.reports.cli --fixture acceptance --json-out standalone\pnl_analytics_bot\data\acceptance_report.json
```

This prints fill counts, duplicate counts, closed round trips, open positions,
net PnL, fees, funding, win rate, exchange mismatch count, and sample card
generation count.

## Dashboard

Run the standalone dashboard:

```powershell
python -B -m standalone.pnl_analytics_bot.dashboard.server --host 127.0.0.1 --port 8787
```

Open `http://127.0.0.1:8787`.

Endpoints:

- `/api/summary` returns summary, analytics, round trips, open positions,
  time-series data, mismatches, and scenario checks.
- `/api/time-series` returns equity curve, PnL bars, drawdown, and day/week/month
  aggregates.
- `/api/round-trips` returns closed round-trip records with realization details.
- `/api/scenarios` returns fixture checks for long/short wins, partial closes,
  scale-in, flips, breakeven, and open-position separation.

The default dashboard uses a scenario fixture set designed for human review of
the accounting behavior. Its Scenario Checks tab is enabled only for that
fixture, so acceptance or imported fill data will not show false failed checks
just because the special scenario symbols are absent. Pass `--fixture
acceptance` for the smaller acceptance fixture or `--input-json
path\to\fills.json` for imported fills.

The visible dashboard includes:

- round-trip totals with close-fill evidence for each realization
- a time-series audit table that maps chart index to closed time, symbol, PnL,
  equity, and drawdown
- separate wins, true losses, breakevens, and open-position counts

## Telegram Alerts

Alerts are sent only for closed round trips. Partial fills do not send PnL card
alerts. By default, a fresh standalone DB marks already-closed historical round
trips as seen without sending Telegram messages, so first startup does not
blast old trades. That bootstrap is persisted; later polls alert newly
discovered closes unless their round-trip ID was already marked sent. Alert IDs
are persisted in the standalone SQLite DB, so restarts do not resend the same
close alert.

Set:

```powershell
$env:PNL_TG_BOT_TOKEN="..."
$env:PNL_TG_CHAT_ID="..."
python -B -m standalone.pnl_analytics_bot.reports.cli --fixture acceptance --send-telegram --telegram-max 1 --persist
```

The CLI sends at most `--telegram-max` alerts in one run. The in-memory deduper
also limits duplicate sends and burst spam inside a process. Use
`--telegram-alert-after 2026-06-01T00:00:00+00:00` to choose the startup cutoff,
or `--telegram-backfill` only when you explicitly want to send unsent historical
closed-trade alerts.
