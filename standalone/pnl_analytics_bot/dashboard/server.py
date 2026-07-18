from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Callable

from aiohttp import web

from ..core.engine import PnlReconstructor
from ..core.serialization import result_payload
from ..reports.cli import load_input
from ..reports.fixtures import acceptance_fills, scenario_fills, validate_expected_scenarios


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Standalone PnL Analytics</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0f141c;
      --panel: #171d26;
      --panel2: #1d2530;
      --text: #e6edf7;
      --muted: #9aa6b5;
      --line: #2c3542;
      --green: #22c55e;
      --red: #f05252;
      --amber: #f59e0b;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Arial, Helvetica, sans-serif;
      letter-spacing: 0;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      padding: 18px 22px;
      border-bottom: 1px solid var(--line);
      background: #111821;
      position: sticky;
      top: 0;
      z-index: 2;
    }
    h1 { font-size: 20px; margin: 0; }
    .tabs { display: flex; gap: 8px; }
    button {
      border: 1px solid var(--line);
      background: var(--panel2);
      color: var(--text);
      height: 34px;
      padding: 0 12px;
      cursor: pointer;
    }
    button.active { border-color: var(--green); color: var(--green); }
    main { padding: 20px; max-width: 1440px; margin: 0 auto; }
    .metrics {
      display: grid;
      grid-template-columns: repeat(6, minmax(130px, 1fr));
      gap: 10px;
      margin-bottom: 18px;
    }
    .metric, .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    .metric { padding: 12px; min-height: 78px; }
    .label { color: var(--muted); font-size: 12px; text-transform: uppercase; }
    .value { font-size: 24px; margin-top: 8px; white-space: nowrap; }
    .green { color: var(--green); }
    .red { color: var(--red); }
    .amber { color: var(--amber); }
    .grid { display: grid; grid-template-columns: 1.35fr .9fr; gap: 14px; align-items: start; }
    .panel { padding: 14px; overflow: hidden; }
    .panel h2 { font-size: 16px; margin: 0 0 12px; }
    canvas { width: 100%; height: 260px; display: block; background: #111821; border: 1px solid var(--line); }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { padding: 9px 8px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
    th { color: var(--muted); font-weight: normal; }
    td.num, th.num { text-align: right; }
    .pill { display: inline-block; padding: 3px 7px; border: 1px solid var(--line); border-radius: 999px; color: var(--muted); }
    .hidden { display: none; }
    @media (max-width: 900px) {
      header { align-items: flex-start; flex-direction: column; }
      .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .grid { grid-template-columns: 1fr; }
      .value { font-size: 20px; }
    }
  </style>
</head>
<body>
<header>
  <h1>Standalone PnL Analytics</h1>
  <div class="tabs">
    <button class="active" data-tab="overview">Overview</button>
    <button data-tab="trades">Trades</button>
    <button data-tab="checks">Scenario Checks</button>
  </div>
</header>
<main>
  <section id="overview">
    <div class="metrics" id="metrics"></div>
    <div class="grid">
      <div class="panel">
        <h2>Equity Curve</h2>
        <canvas id="equity" width="900" height="300"></canvas>
      </div>
      <div class="panel">
        <h2>PnL Per Closed Round Trip</h2>
        <canvas id="bars" width="520" height="300"></canvas>
      </div>
      <div class="panel">
        <h2>Drawdown</h2>
        <canvas id="drawdown" width="900" height="300"></canvas>
      </div>
      <div class="panel">
        <h2>Time-Series Audit</h2>
        <table><thead><tr><th>#</th><th>Closed</th><th>Symbol</th><th class="num">Net PnL</th><th class="num">Equity</th><th class="num">Drawdown</th></tr></thead><tbody id="timeSeriesRows"></tbody></table>
      </div>
      <div class="panel">
        <h2>Open Positions</h2>
        <table><thead><tr><th>Symbol</th><th>Side</th><th class="num">Qty</th><th class="num">Avg Entry</th></tr></thead><tbody id="openPositions"></tbody></table>
      </div>
    </div>
  </section>
  <section id="trades" class="hidden">
    <div class="panel">
      <h2>Closed Round Trips</h2>
      <table><thead><tr><th>Closed</th><th>Symbol</th><th>Side</th><th class="num">Net PnL</th><th class="num">Return</th><th class="num">Cost</th><th>Close Evidence</th><th>Fills</th></tr></thead><tbody id="roundTrips"></tbody></table>
    </div>
  </section>
  <section id="checks" class="hidden">
    <div class="panel">
      <h2>Scenario Accuracy</h2>
      <table><thead><tr><th>Scenario</th><th>Side</th><th class="num">Expected PnL</th><th class="num">Actual PnL</th><th class="num">Expected Return</th><th class="num">Actual Return</th><th>Status</th></tr></thead><tbody id="checksTable"></tbody></table>
    </div>
  </section>
</main>
<script>
const fmtMoney = v => {
  const n = Number(v || 0);
  const s = n >= 0 ? '+' : '-';
  return `${s}$${Math.abs(n).toFixed(2)}`;
};
const cls = v => Number(v || 0) >= 0 ? 'green' : 'red';
const fmtPct = v => `${Number(v || 0).toFixed(2)}%`;
const fmtQty = v => Number(v || 0).toFixed(4).replace(/\\.?0+$/, '');
function metric(label, value, klass='') {
  return `<div class="metric"><div class="label">${label}</div><div class="value ${klass}">${value}</div></div>`;
}
function realizationEvidence(t) {
  if (!t.realizations || !t.realizations.length) return '';
  return t.realizations.map((r, i) => {
    const close = r.fill_id ? `#${r.fill_id}` : `#${i + 1}`;
    return `<div>${close} ${fmtQty(r.closed_qty)} @ $${Number(r.exit_price).toFixed(4)} -> <span class="${cls(r.net_pnl)}">${fmtMoney(r.net_pnl)}</span></div>`;
  }).join('');
}
function drawLine(canvas, points, key, color) {
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0,0,canvas.width,canvas.height);
  ctx.strokeStyle = '#2c3542'; ctx.lineWidth = 1;
  for (let i=0;i<5;i++){ const y=20+i*(canvas.height-40)/4; ctx.beginPath(); ctx.moveTo(32,y); ctx.lineTo(canvas.width-12,y); ctx.stroke(); }
  if (!points.length) return;
  const values = points.map(p => Number(p[key] || 0));
  const min = Math.min(0, ...values), max = Math.max(0, ...values);
  const span = max - min || 1;
  const x = i => 36 + i * (canvas.width - 58) / Math.max(points.length - 1, 1);
  const y = v => canvas.height - 24 - ((v - min) / span) * (canvas.height - 48);
  ctx.strokeStyle = color; ctx.lineWidth = 3; ctx.beginPath();
  values.forEach((v,i) => i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v)));
  ctx.stroke();
}
function drawBars(canvas, bars) {
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0,0,canvas.width,canvas.height);
  if (!bars.length) return;
  const vals = bars.map(b => Number(b.net_pnl || 0));
  const maxAbs = Math.max(1, ...vals.map(Math.abs));
  const zero = canvas.height / 2;
  ctx.strokeStyle = '#2c3542'; ctx.beginPath(); ctx.moveTo(20, zero); ctx.lineTo(canvas.width-10, zero); ctx.stroke();
  const gap = 6, w = Math.max(8, (canvas.width - 40 - gap * (bars.length-1)) / bars.length);
  vals.forEach((v,i) => {
    const h = Math.abs(v) / maxAbs * (canvas.height/2 - 24);
    const x = 22 + i * (w + gap);
    ctx.fillStyle = v >= 0 ? '#22c55e' : '#f05252';
    ctx.fillRect(x, v >= 0 ? zero - h : zero, w, h);
  });
}
function render(data) {
  const a = data.analytics;
  document.getElementById('metrics').innerHTML = [
    metric('Closed Trades', a.closed_trades),
    metric('Wins', a.wins, 'green'),
    metric('Losses', a.losses, 'red'),
    metric('Breakevens', a.breakevens || 0, 'amber'),
    metric('Win Rate', fmtPct(a.win_rate), 'green'),
    metric('Net PnL', fmtMoney(a.net_pnl), cls(a.net_pnl)),
    metric('Profit Factor', a.profit_factor || 'n/a'),
    metric('Max Drawdown', fmtMoney(a.max_drawdown), 'red'),
    metric('Open Positions', a.open_positions),
  ].join('');
  drawLine(document.getElementById('equity'), data.time_series.equity_curve, 'equity', '#22c55e');
  drawLine(document.getElementById('drawdown'), data.time_series.drawdown, 'drawdown', '#f05252');
  drawBars(document.getElementById('bars'), data.time_series.pnl_bars);
  document.getElementById('timeSeriesRows').innerHTML = data.time_series.equity_curve.length ? data.time_series.equity_curve.map(p => `<tr><td>${p.index}</td><td>${new Date(p.closed_at).toLocaleString()}</td><td>${p.symbol}</td><td class="num ${cls(p.net_pnl)}">${fmtMoney(p.net_pnl)}</td><td class="num">${fmtMoney(p.equity)}</td><td class="num ${cls(p.drawdown)}">${fmtMoney(p.drawdown)}</td></tr>`).join('') : '<tr><td colspan="6">No closed round trips</td></tr>';
  document.getElementById('openPositions').innerHTML = data.open_positions.length ? data.open_positions.map(p => `<tr><td>${p.symbol}</td><td>${p.direction}</td><td class="num">${p.qty}</td><td class="num">$${Number(p.avg_entry).toFixed(4)}</td></tr>`).join('') : '<tr><td colspan="4">No open positions</td></tr>';
  document.getElementById('roundTrips').innerHTML = data.round_trips.length ? data.round_trips.map(t => `<tr><td>${new Date(t.closed_at).toLocaleString()}</td><td>${t.symbol}</td><td>${t.direction}</td><td class="num ${cls(t.net_pnl)}">${fmtMoney(t.net_pnl)}</td><td class="num">${fmtPct(t.return_on_cost)}</td><td class="num">$${Number(t.cost_basis).toFixed(2)}</td><td>${realizationEvidence(t)}</td><td><span class="pill">${t.n_realizations} close fill${t.n_realizations === 1 ? '' : 's'}</span></td></tr>`).join('') : '<tr><td colspan="8">No closed round trips</td></tr>';
  document.getElementById('checksTable').innerHTML = data.scenario_checks.length ? data.scenario_checks.map(c => `<tr><td>${c.symbol}</td><td>${c.direction}</td><td class="num">${fmtMoney(c.expected_net_pnl)}</td><td class="num ${cls(c.actual_net_pnl)}">${fmtMoney(c.actual_net_pnl)}</td><td class="num">${fmtPct(c.expected_return_on_cost)}</td><td class="num">${fmtPct(c.actual_return_on_cost)}</td><td class="${c.passed ? 'green' : 'red'}">${c.passed ? 'PASS' : 'FAIL'}</td></tr>`).join('') : '<tr><td colspan="7">Scenario checks are disabled for this input</td></tr>';
}
document.querySelectorAll('button[data-tab]').forEach(btn => btn.onclick = () => {
  document.querySelectorAll('button[data-tab]').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('main > section').forEach(s => s.classList.add('hidden'));
  document.getElementById(btn.dataset.tab).classList.remove('hidden');
});
fetch('/api/summary').then(r => r.json()).then(render);
</script>
</body>
</html>
"""


def build_payload(fills, *, validate_scenarios: bool = False) -> dict:
    result = PnlReconstructor().reconstruct(fills)
    checks = validate_expected_scenarios(result.round_trips) if validate_scenarios else []
    return result_payload(result, scenario_checks=checks)


def create_app(payload_factory: Callable[[], dict] | None = None) -> web.Application:
    payload_factory = payload_factory or (lambda: build_payload(scenario_fills(), validate_scenarios=True))
    app = web.Application()

    async def index(_request: web.Request) -> web.Response:
        return web.Response(text=INDEX_HTML, content_type="text/html", headers={"Cache-Control": "no-cache"})

    async def summary(_request: web.Request) -> web.Response:
        return web.json_response(payload_factory())

    async def round_trips(_request: web.Request) -> web.Response:
        return web.json_response({"round_trips": payload_factory()["round_trips"]})

    async def time_series(_request: web.Request) -> web.Response:
        return web.json_response(payload_factory()["time_series"])

    async def scenarios(_request: web.Request) -> web.Response:
        return web.json_response({"scenario_checks": payload_factory()["scenario_checks"]})

    app.router.add_get("/", index)
    app.router.add_get("/api/summary", summary)
    app.router.add_get("/api/round-trips", round_trips)
    app.router.add_get("/api/time-series", time_series)
    app.router.add_get("/api/scenarios", scenarios)
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Standalone PnL analytics dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--fixture", choices=["scenario", "acceptance"], default="scenario")
    parser.add_argument("--input-json", type=Path)
    args = parser.parse_args(argv)

    if args.input_json:
        fills = load_input(args.input_json)
    elif args.fixture == "acceptance":
        fills = acceptance_fills()
    else:
        fills = scenario_fills()
    validate_scenarios = args.fixture == "scenario" and not args.input_json
    app = create_app(lambda: build_payload(fills, validate_scenarios=validate_scenarios))
    web.run_app(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
