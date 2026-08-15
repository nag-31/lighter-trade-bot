# Crypto Scientist App Hubs

The local control centre for the repo's independent dashboards. It contains the access page, launcher, and launcher notes while the underlying applications stay in their own packages.

Run on Windows PowerShell:

    & "D:\content\crypto scientist\lighter-trade-bot\apps_hub\run_all_apps.ps1"

Then open http://127.0.0.1:8800/.

The applications have explicit boundaries:

- **Naga Portfolio** at http://127.0.0.1:8088/ is the public professional
  portfolio, Crypto Scientist case study, and downloadable pitch deck.
- **Signal Research** at http://127.0.0.1:8810/ owns market signals,
  hypotheses, forward outcomes, and weekly edge review.
- **Trade Journal** at http://127.0.0.1:8811/ owns lifecycle review, reasons,
  notes, and journal editing in `data/trading_journal.db`.
- **Trade Tracker** at http://127.0.0.1:8080/ owns live exchange ingestion,
  positions, alerts, and trade accounting.
- **TVL & Protocol Monitor** at http://127.0.0.1:8788/ is an optional sibling
  service with its own database.

The App Hub only links to these applications and checks their health. It does
not merge their runtimes or databases.

The launcher starts only apps whose ports are not already responding. It writes local logs to D:\content\crypto scientist\lighter-trade-bot\data\app_logs\, which Git ignores.

When the sibling `D:\content\crypto scientist\hack-alert-bot` project is present, the Windows launcher also starts its monitor and dashboard at http://127.0.0.1:8788/. Port 8788 is a launcher-only override that avoids the PnL Analytics service on 8787.
