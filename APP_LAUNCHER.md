# Crypto Scientist App Hubs

This repo contains several standalone dashboards. They stay separate, but can be started together and accessed from one local page.

## Run All Apps

Windows PowerShell:

    & "D:\content\crypto scientist\lighter-trade-bot\apps_hub\run_all_apps.ps1"

The former command at D:\content\crypto scientist\lighter-trade-bot\scripts\run_all_apps.ps1 remains available and forwards to the hub launcher.

Open:

    http://127.0.0.1:8800/

In PowerShell, press Enter in the launcher terminal to stop the apps it started.

## Apps Started

| App | URL |
| --- | --- |
| Signal Research | http://127.0.0.1:8810/ |
| Trade Journal | http://127.0.0.1:8811/ |
| Portfolio Overview | http://127.0.0.1:8790/ |
| Trade Tracker Dashboard | http://127.0.0.1:8080/ |
| Standalone PnL Analytics | http://127.0.0.1:8787/ |
| DeFi Hack Alert | http://127.0.0.1:8788/ |
| Full-Fledged Bot | http://127.0.0.1:18080/ |
| Apps Hub | http://127.0.0.1:8800/ |

Logs are written to D:\content\crypto scientist\lighter-trade-bot\data\app_logs\ and remain ignored by Git, as do local wallet database files.

The DeFi Hack Alert entry is optional and is started by the Windows launcher when the sibling `D:\content\crypto scientist\hack-alert-bot` project exists. Its normal standalone port remains unchanged; the shared launcher overrides it to 8788 to avoid the PnL Analytics service on 8787.
