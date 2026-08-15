"""Local access page and health overview for the standalone applications."""

from __future__ import annotations

import argparse
import asyncio
import os
from dataclasses import dataclass

from aiohttp import ClientSession, ClientTimeout, web


@dataclass(frozen=True)
class AppLink:
    key: str
    name: str
    description: str
    url: str
    health_url: str
    category: str
    note: str


PUBLIC_SUFFIX = os.getenv("APP_HUB_PUBLIC_SUFFIX", "").strip().strip(".")
PUBLIC_SCHEME = os.getenv("APP_HUB_PUBLIC_SCHEME", "http").strip() or "http"


def _app_url(subdomain: str, local_port: int) -> str:
    if PUBLIC_SUFFIX:
        return f"{PUBLIC_SCHEME}://{subdomain}.{PUBLIC_SUFFIX}/"
    return f"http://127.0.0.1:{local_port}/"


APPS: tuple[AppLink, ...] = (
    AppLink("enkapital", "Naga Portfolio", "Professional trading-systems portfolio featuring Crypto Scientist, architecture evidence, selected work, and a downloadable pitch deck.", _app_url("enkapital", 8088), "http://127.0.0.1:8088/", "Website", "Public portfolio for hiring, collaboration, and product walkthroughs."),
    AppLink("command-center", "Signal Research", "Market signals, counterfactual outcomes, research queue, and weekly edge review.", _app_url("command", 8810), "http://127.0.0.1:8810/health", "Research", "Signal research only. Trading records live in the separate Trade Journal."),
    AppLink("trade-journal", "Trade Journal", "Position-level execution review with grouped fills, partial exits, reasons, notes, and live or realized PnL.", _app_url("journal", 8811), "http://127.0.0.1:8811/health", "Trading", "One lifecycle record follows the full position instead of treating its last fill as the trade."),
    AppLink("portfolio", "Portfolio Overview", "Guest portfolio workspace for checking multi-wallet balances across EVM chains, Lighter, Hyperliquid, LIT staking, and pool deposits.", _app_url("portfolio", 8790), "http://127.0.0.1:8790/api/config", "Portfolio", "Guest data stays in this browser and is not saved to the VM."),
    AppLink("private-portfolio", "Private Portfolio Tracker", "Your password-protected portfolio with saved wallets, labels, history, and private VM storage.", _app_url("private-portfolio", 8791), "http://127.0.0.1:8791/api/auth/status", "Portfolio", "Sign in to access your saved portfolio workspace."),
    AppLink("tracker", "Trade Tracker", "Standalone live monitor for accounts, positions, orders, trade events, aggregate PnL, and system health.", _app_url("dashboard", 8080), "http://127.0.0.1:8080/healthz", "Trading", "Remains its own application and reads the deployed account configuration."),
    AppLink("analytics", "PnL Analytics", "Replay fills, inspect round trips, equity curves, drawdown, and scenarios.", _app_url("analytics", 8787), "http://127.0.0.1:8787/api/summary", "Analytics", "Runs on the VM using selected inputs or bundled fixtures."),
    AppLink("pnl-dashboard", "Pro PnL Dashboard", "Timescale-backed exchange PnL dashboard with encrypted API-key storage and background sync.", _app_url("pnl", 3000), "http://127.0.0.1:3000/", "Analytics", "API keys are encrypted at rest; use only over HTTPS."),
    AppLink("importer", "Futures PnL Importer", "Upload exchange exports and analyze them entirely in your browser.", _app_url("importer", 5180), "http://127.0.0.1:5180/", "Analytics", "Uploaded files remain in your browser and are not sent to the VM."),
    AppLink("hack-alert", "TVL & Protocol Monitor", "TVL-drop monitoring, anomaly detection, incident correlation, and provider health across major lending markets.", _app_url("hack", 8788), "http://127.0.0.1:8788/health/live", "Risk", "Own service and database; its alerts are not copied into Signal Research or the Trade Journal."),
    AppLink("bot", "Full-Fledged Bot", "Replay-first alerts dashboard with event history and notification controls.", _app_url("bot", 18080), "http://127.0.0.1:18080/health", "Trading", "Runs as a separate service with its own health view."),
)

def _cards() -> str:
    cards = []
    for app in APPS:
        cards.append(
            f'''<article class="app-card" data-app="{app.key}">
              <div class="card-head"><span class="category">{app.category}</span><span class="status">Checking</span></div>
              <div><h2>{app.name}</h2><p>{app.description}</p></div>
              <p class="note">{app.note}</p>
              <div class="actions"><a class="open" href="{app.url}" target="_blank" rel="noreferrer">Open app <span aria-hidden="true">&#8599;</span></a><a class="secondary" href="{app.health_url}" target="_blank" rel="noreferrer">Health</a></div>
            </article>'''
        )
    return "\n".join(cards)


INDEX_HTML = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Crypto Scientist App Hubs</title>
<style>
:root {{ --ink:#e8edf5; --muted:#96a4b8; --bg:#090c12; --surface:#121823; --surface-2:#171f2c; --line:#283449; --mint:#58d6a7; --red:#fb7185; --amber:#f4bf54; }}
* {{ box-sizing:border-box; }} body {{ margin:0; min-height:100vh; font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:var(--bg); color:var(--ink); letter-spacing:0; }}
main {{ max-width:1160px; margin:auto; padding:48px 28px 40px; }} .top {{ display:flex; align-items:flex-start; justify-content:space-between; gap:20px; padding-bottom:28px; border-bottom:1px solid var(--line); }}
.eyebrow,.category {{ color:var(--mint); font-size:11px; font-weight:700; letter-spacing:.08em; text-transform:uppercase; }} h1 {{ font-size:32px; line-height:1.12; margin:7px 0 8px; }} .intro {{ margin:0; color:var(--muted); font-size:14px; max-width:610px; line-height:1.55; }}
button {{ border:1px solid var(--line); border-radius:6px; background:var(--surface-2); color:var(--ink); padding:10px 13px; font:inherit; font-size:13px; cursor:pointer; }} button:hover {{ border-color:var(--mint); }} button:disabled {{ opacity:.65; cursor:wait; }}
.summary {{ display:grid; grid-template-columns:repeat(3,1fr); gap:12px; margin:24px 0; }} .stat {{ min-height:91px; padding:15px; background:var(--surface); border:1px solid var(--line); border-radius:8px; }} .stat b {{ display:block; font-size:25px; margin-top:5px; }} .stat span {{ color:var(--muted); font-size:12px; }}
.grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px; }} .app-card {{ min-height:242px; display:flex; flex-direction:column; padding:17px; border:1px solid var(--line); border-radius:8px; background:var(--surface); }} .card-head {{ display:flex; align-items:center; justify-content:space-between; }} h2 {{ margin:20px 0 7px; font-size:18px; }} p {{ color:var(--muted); font-size:13px; line-height:1.5; }} .note {{ margin-top:auto; min-height:38px; font-size:12px; }}
.status {{ border:1px solid var(--line); border-radius:999px; padding:4px 8px; color:var(--muted); font-size:11px; font-weight:700; }} .status.online {{ color:var(--mint); border-color:#245a49; }} .status.error,.status.offline {{ color:var(--red); border-color:#643344; }} .status.degraded {{ color:var(--amber); border-color:#624f2b; }}
.actions {{ display:flex; gap:8px; margin-top:14px; }} a {{ min-height:35px; display:inline-flex; align-items:center; justify-content:center; padding:0 12px; border:1px solid var(--line); border-radius:6px; color:var(--ink); text-decoration:none; font-size:13px; }} a.open {{ border-color:var(--mint); background:var(--mint); color:#082319; font-weight:750; }} a.secondary {{ color:var(--muted); }} a:hover {{ filter:brightness(1.08); }}
.footer {{ color:var(--muted); font-size:12px; padding-top:26px; }} code {{ color:var(--mint); }} @media (max-width:700px) {{ main {{ padding:28px 16px; }} .top {{ flex-direction:column; }} h1 {{ font-size:27px; }} .summary,.grid {{ grid-template-columns:1fr; }} .app-card {{ min-height:220px; }} }}
</style></head><body><main><section class="top"><div><div class="eyebrow">Central control centre</div><h1>Crypto Scientist App Hubs</h1><p class="intro">Open and monitor every deployed dashboard from one page.</p></div><button id="refresh" type="button">Refresh status</button></section>
<section class="summary" aria-label="System summary"><div class="stat"><span>Apps online</span><b id="online">-</b></div><div class="stat"><span>Needs attention</span><b id="attention">-</b></div><div class="stat"><span>Last checked</span><b id="checked">-</b></div></section>
<section class="grid">{_cards()}</section><p class="footer">Each app runs independently against its own production boundary on the GCP VM. This directory only checks private health endpoints every 15 seconds.</p></main>
<script>const button=document.querySelector("#refresh");async function refresh(){{button.disabled=true;button.textContent="Checking...";try{{const r=await fetch("/api/status",{{cache:"no-store"}});const data=await r.json();let online=0,attention=0;for(const app of data.apps){{const card=document.querySelector('[data-app="'+app.key+'"]');const badge=card.querySelector(".status");badge.textContent=app.status;badge.className="status "+app.status.toLowerCase();if(app.status==="Online")online++;else attention++;}}document.querySelector("#online").textContent=online+" / "+data.apps.length;document.querySelector("#attention").textContent=attention;document.querySelector("#checked").textContent=new Date().toLocaleTimeString([],{{hour:"2-digit",minute:"2-digit",second:"2-digit"}});}}catch(e){{document.querySelector("#attention").textContent="?";}}finally{{button.disabled=false;button.textContent="Refresh status";}}}}button.addEventListener("click",refresh);refresh();setInterval(refresh,15000);</script></body></html>'''


async def _probe(session: ClientSession, app: AppLink) -> dict[str, str]:
    try:
        async with session.get(app.health_url) as response:
            if response.status < 300:
                status = "Online"
            elif response.status < 500:
                status = "Degraded"
            else:
                status = "Error"
    except (asyncio.TimeoutError, OSError):
        status = "Offline"
    return {"key": app.key, "status": status}


async def status(_request: web.Request) -> web.Response:
    timeout = ClientTimeout(total=2)
    async with ClientSession(timeout=timeout) as session:
        apps = await asyncio.gather(*(_probe(session, app) for app in APPS))
    return web.json_response({"apps": apps})


async def index(_request: web.Request) -> web.Response:
    return web.Response(text=INDEX_HTML, content_type="text/html")


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/status", status)
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local control centre for Lighter bot apps")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8800)
    args = parser.parse_args(argv)
    web.run_app(create_app(), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
