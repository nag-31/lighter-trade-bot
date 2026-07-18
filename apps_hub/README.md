# Lighter Apps Hub

The local control centre for the repo's independent dashboards. It contains the access page, launcher, and launcher notes while the underlying applications stay in their own packages.

Run on Windows PowerShell:

    & "D:\content\crypto scientist\lighter-trade-bot\apps_hub\run_all_apps.ps1"

Then open http://127.0.0.1:8800/.

The launcher starts only apps whose ports are not already responding. It writes local logs to D:\content\crypto scientist\lighter-trade-bot\data\app_logs\, which Git ignores.

When the sibling `D:\content\crypto scientist\hack-alert-bot` project is present, the Windows launcher also starts its monitor and dashboard at http://127.0.0.1:8788/. Port 8788 is a launcher-only override that avoids the PnL Analytics service on 8787.
