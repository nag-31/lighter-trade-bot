# Checklist — what "healthy" looks like, and what to flag

This is the human's quick reference. It turns "is everything OK?" into a list
of checks you can run, plus the red flags worth reporting.

## 1. Services up

```powershell
# From the VM (or wherever the services run)
curl -s http://127.0.0.1:8080/healthz   # "ok"
curl -s http://127.0.0.1:8810/health    # Command Center
curl -s http://127.0.0.1:8811/health    # Trade Journal
curl -s http://127.0.0.1:8800/api/status
```

All four should return HTTP 200.

## 2. Database health

```powershell
& .\.venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('data/events.db'); print(c.execute('PRAGMA integrity_check').fetchall())"
```

You want `[(u'ok',)]`. Also check per-account ledgers in `data/accounts/`.

## 3. The dashboard looks right

Open the dashboard and check:

- **Positions** match what the exchange shows (reconciler runs every 30s).
- **Closed trades grid** — one tile per fully-closed trade. In-progress
  positions show as "IN PROGRESS" tiles, not closed tiles.
- **PnL card vs tile vs chart** all show the **same** round-trip total.
- **Events table** — for HL, prices/sizes look "rounded/odd" (that's privacy
  blur working). If you see clean exact prices for HL there, that's a **leak**.
- **Stats** use closed trades only; win rate and total PnL are exact.

## 4. Telegram looks right

- A full close sends **one album** = PnL card + execution chart. If the chart
  fails, you should still get the card (never silence).
- Reduce/scale-out alerts are batched and land in the **session digest**
  (one message per burst), not one message per fill.
- Caption shows `COIN SIDE · ±$PnL` so the channel is searchable by ticker.
- No wallet address, token, or API key ever appears in any message.

## 5. What to flag to me / the agent

If you see any of these, stop and report:

- 🔴 **HL real prices/sizes on a public surface** (dashboard events table,
  raw `/ws` payload, open orders panel). That's a privacy leak — P1.
- 🔴 **PnL double-counted** (same trade appears twice, stats jump after a
  restart, a scale-out's PnL added twice). P0/P1.
- 🔴 **Wrong-card alert**: a close card shows a *different* coin's card image.
- 🟠 **Duplicate Telegram alert** for the same fill after a reconnect.
- 🟠 **`/health` shows telegram or a source down/degraded** for a while.
- 🟠 **stats window shows PnL from before the cutoff date** (date filtering bug).
- 🟡 **Outbox rows stuck `pending`/`failed`** for old alerts — check
  `notification_outbox` in `data/events.db`.

## 6. Normal maintenance

- Config changes → edit `config.yaml` → `sudo systemctl restart lighterbot`
  (**with your OK**).
- Adding/removing a wallet → add/remove a `sources:` entry; env vars in `.env`.
- Reconcile script → `python scripts/reconcile_hl_pnl.py --days 10` for dry-run,
  `--apply` only after review and a backup.

## 7. Test suite (canary)

From the repo root (Windows):

```powershell
& .\.venv\Scripts\python.exe -m pytest -q --basetemp C:\Users\ADMIN\AppData\Local\Temp\opencode\pytest-canary
```

Green = the 1000+ tests pass. A red here usually means something got broken,
not that the tests are flaky.
