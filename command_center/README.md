# Crypto Scientist Signal Research

The standalone market-signal research and decision-intelligence application.
Trade lifecycle review belongs to Trade Journal, and TVL/protocol risk belongs
to the separate TVL Monitor.

It reads the market-research source databases without modifying them:

- `speculation-alert-bot/data/state.db`
- `speculation-alert-bot/data/candles.db`

It persists signals, research decisions, outcome snapshots, risk limits, and
sync history in `lighter-trade-bot/data/command_center.db`.

## What it does

- **Today** ranks market signals into a decision queue.
- **Freshness-aware queue** separates signals from the last seven days from
  historical research and scores urgency using severity, confidence, and age.
- **Analytics integrity** keeps sample/test alerts visible in a simulation
  workspace while excluding them from production precision, edge rankings,
  counterfactuals, and weekly discipline.
- **Decision journal** stores thesis, direction, entry, invalidation, target,
  confidence, risk, status, and linked trades.
- **Outcome tracker** measures directional returns after 1h, 6h, 24h, and 7d.
- **Counterfactual tracking** measures ignored and dismissed signals too.
- **Edge Lab** compares detector hit rate, average edge, MFE, MAE, and horizon.
- **Weekly review** summarizes discipline, P&L, best signals, and missed setups.
  It also identifies the noisiest detector and repeated losing-asset patterns.

## Run

Use the shared launcher:

```powershell
& "D:\content\crypto scientist\lighter-trade-bot\apps_hub\run_all_apps.ps1"
```

Then open <http://127.0.0.1:8810/>.

For the Command Center only:

```powershell
python -B -m command_center.app --host 127.0.0.1 --port 8810
```

No API keys are required. Page loads are read-only; scheduled background sync
and **Sync now** update research data. Trade and position ingestion is disabled
here and owned by Trade Tracker/Trade Journal.
