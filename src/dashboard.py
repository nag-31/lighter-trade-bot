"""Dashboard + Telegram notifier.

Watches every tracked pool/wallet listed in config.yaml (Lighter pools and
Hyperliquid wallets) via WebSocket, updates the local dashboard, and posts
OPEN / CLOSE / SIZE_CHANGE events to Telegram.

Run with:  python -m src.dashboard
Then open: http://localhost:8080/
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import time
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx
from aiohttp import WSMsgType, web
from dotenv import load_dotenv

from pathlib import Path

from .db import (
    backfill_closed_trades_from_events,
    init_db,
    load_closed_trades,
    load_recent_events,
    load_recorded_fill_ids,
    load_tg_alerts,
    save_closed_trade,
    save_event,
    save_tg_alert,
)
from .display_transform import PrivacyParams, disp_notional, disp_price, disp_size, disp_time, disp_view, footnote, price_factor
from .filters import passes_min_notional
from .formatter import format_aggregate, format_event, format_reduce_aggregate, format_sl_tp_set
from .health import HealthRegistry
from .pnl_card import calculate_pnl, generate_pnl_card, record_result
from .sources import BotSettings, Source, load_settings, load_sources
from .stats import aggregate_round_trips, compute_stats, filter_trades, format_stats_summary
from .stats_card import render_stats_card
from .types import Event, EventKind, OpenOrder, Position, Trade

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("dashboard")

DB_PATH = Path(__file__).parent.parent / "data" / "events.db"
PIDFILE = Path("/tmp/lighterbot.pid")


def _acquire_pid_lock() -> bool:
    """Return True if we successfully claimed the singleton lock, False if another instance is running."""
    if PIDFILE.exists():
        try:
            pid = int(PIDFILE.read_text().strip())
            os.kill(pid, 0)  # signal 0 = probe only; raises if process doesn't exist
            log.error("Another lighterbot instance is already running (PID %d). Exiting.", pid)
            return False
        except (ProcessLookupError, PermissionError, ValueError, OSError):
            pass  # stale PID file — previous instance died without cleanup
    PIDFILE.write_text(str(os.getpid()))
    return True


def _release_pid_lock() -> None:
    try:
        PIDFILE.unlink(missing_ok=True)
    except Exception:
        pass


def _to_jsonable(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, (Trade, Position, Event)):
        return _to_jsonable(asdict(obj))
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_jsonable(x) for x in obj]
    return obj


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Trade tracker</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 24px; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; background: #0b0d10; color: #d8dbe0; }
  h1 { font-size: 18px; margin: 0 0 4px; color: #fff; }
  .meta { font-size: 12px; color: #6b7280; margin-bottom: 24px; }
  .meta .dot { display:inline-block; width:8px; height:8px; border-radius:50%; background:#444; margin-right:6px; vertical-align: 1px; }
  .meta .dot.on { background:#22c55e; }
  .meta .dot.off { background:#ef4444; }
  .grid { display: grid; grid-template-columns: 1fr 2fr; gap: 24px; align-items: start; }
  .col-left { display: flex; flex-direction: column; gap: 24px; }
  section { background:#13161b; border:1px solid #1f242c; border-radius:8px; padding:16px; }
  section h2 { font-size: 12px; text-transform: uppercase; letter-spacing: 1px; color:#9ca3af; margin: 0 0 12px; }
  .col-left section { max-height: 340px; overflow-y: auto; }
  .col-right { background:#13161b; border:1px solid #1f242c; border-radius:8px; padding:16px; max-height: 720px; overflow-y: auto; }
  .col-right h2 { font-size: 12px; text-transform: uppercase; letter-spacing: 1px; color:#9ca3af; margin: 0 0 12px; }
  table { width:100%; border-collapse: collapse; font-size: 12px; }
  th { text-align:left; color:#6b7280; font-weight:500; padding: 6px 8px; border-bottom: 1px solid #1f242c; }
  td { padding: 8px; border-bottom: 1px solid #11141a; }
  tr:last-child td { border-bottom: none; }
  .long { color: #22c55e; }
  .short { color: #ef4444; }
  .kind-OPEN { color: #60a5fa; }
  .kind-CLOSE { color: #f59e0b; }
  .kind-SIZE_CHANGE { color: #a78bfa; }
  .kind-REDUCE { color: #fb923c; }
  .empty { color:#4b5563; font-style: italic; padding: 8px; }
  .num { text-align: right; font-variant-numeric: tabular-nums; }
  .alert-msg { white-space: pre-wrap; word-break: break-word; color:#cbd5e1; font-size:12px; line-height:1.5; }
  .badge { display:inline-block; padding:2px 7px; border-radius:4px; font-size:10px; font-weight:600; letter-spacing:.5px; }
  .badge-text { background:#1e293b; color:#93c5fd; }
  .badge-card { background:#3b2f1a; color:#fbbf24; }
  /* ---- history section ---- */
  .history-section { margin-top: 24px; }
  .history-section h2 { font-size: 12px; text-transform: uppercase; letter-spacing: 1px; color:#9ca3af; margin: 0 0 16px; }
  .history-grid { display: flex; flex-wrap: wrap; gap: 16px; }
  .trade-tile {
    background: #13161b; border: 1px solid #1f242c; border-radius: 10px;
    padding: 14px 16px; min-width: 260px; max-width: 320px; flex: 1 1 260px;
    transition: border-color 0.2s;
    position: relative;
  }
  .trade-tile:hover { border-color: #374151; }
  .trade-tile.win  { border-left: 3px solid #22c55e; }
  .trade-tile.loss { border-left: 3px solid #ef4444; }
  .tile-header { font-size: 11px; color: #6b7280; margin-bottom: 4px; }
  .tile-direction { font-size: 11px; font-weight: 600; margin-bottom: 6px; }
  .tile-pnl { font-size: 26px; font-weight: 700; line-height: 1.1; margin-bottom: 2px; }
  .tile-pct { font-size: 13px; font-weight: 600; margin-bottom: 10px; }
  .tile-details { display: grid; grid-template-columns: 1fr 1fr; gap: 4px 12px; font-size: 11px; margin-bottom: 8px; }
  .tile-detail-label { color: #6b7280; }
  .tile-detail-val { color: #d8dbe0; }
  .tile-footer { font-size: 10px; color: #4b5563; margin-top: 6px; display: flex; justify-content: space-between; align-items: center; }
  .tile-thumb-wrap { margin-top: 10px; }
  .card-thumb { width: 100%; border-radius: 6px; cursor: pointer; transition: opacity 0.15s; }
  .card-thumb:hover { opacity: 0.85; }
  .green { color: #22c55e; }
  .red   { color: #ef4444; }
  @media (max-width: 900px) {
    .grid { grid-template-columns: 1fr; }
    .col-left section, .col-right { max-height: none; overflow-y: visible; }
    .trade-tile { max-width: 100%; }
    .charts-grid { grid-template-columns: 1fr !important; }
  }
  /* ---- analytics section ---- */
  .analytics-section { margin-top: 24px; }
  .analytics-section-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; }
  .analytics-section-header h2 { font-size: 12px; text-transform: uppercase; letter-spacing: 1px; color:#9ca3af; margin: 0; }
  .btn-send-stats { background: #1f242c; border: 1px solid #374151; color: #d8dbe0; font-family: inherit; font-size: 11px; padding: 5px 12px; border-radius: 5px; cursor: pointer; transition: background 0.15s, border-color 0.15s; }
  .btn-send-stats:hover { background: #262c38; border-color: #4b5563; }
  .btn-send-stats:disabled { opacity: 0.55; cursor: default; }
  .kpi-row { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 20px; }
  .kpi-card { background: #13161b; border: 1px solid #1f242c; border-radius: 8px; padding: 12px 16px; min-width: 110px; flex: 1 1 110px; }
  .kpi-label { font-size: 10px; text-transform: uppercase; letter-spacing: .8px; color: #6b7280; margin-bottom: 5px; }
  .kpi-value { font-size: 18px; font-weight: 700; line-height: 1.15; }
  .kpi-sub { font-size: 10px; color: #6b7280; margin-top: 3px; }
  .charts-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .chart-card { background: #13161b; border: 1px solid #1f242c; border-radius: 8px; padding: 14px 16px; }
  .chart-card-title { font-size: 10px; text-transform: uppercase; letter-spacing: .8px; color: #6b7280; margin-bottom: 10px; }
  .chart-wrap { position: relative; height: 240px; }
  .no-trades-msg { color: #4b5563; font-style: italic; font-size: 12px; padding: 8px 0; }
  /* ---- source-down banner ---- */
  #health-banner { display:none; background:#3b0f0f; border:1px solid #7f1d1d; border-radius:6px; padding:10px 14px; margin-bottom:16px; font-size:12px; color:#fca5a5; }
  #health-banner a { color:#fca5a5; text-decoration:underline; margin-left:8px; }
  #health-banner .hb-issues { display:flex; flex-wrap:wrap; gap:6px 16px; margin-top:6px; }
  #health-banner .hb-issue  { color:#f87171; }
  /* ---- open orders panel ---- */
  .oo-header { display: flex; align-items: center; justify-content: space-between; margin: 0 0 12px; }
  .oo-header h2 { margin: 0; }
  .oo-toggle-wrap { display: flex; align-items: center; gap: 6px; font-size: 11px; color: #6b7280; }
  .oo-toggle { position: relative; display: inline-block; width: 32px; height: 17px; }
  .oo-toggle input { opacity: 0; width: 0; height: 0; }
  .oo-slider { position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0; background: #374151; border-radius: 17px; transition: background 0.2s; }
  .oo-slider:before { position: absolute; content: ""; height: 11px; width: 11px; left: 3px; bottom: 3px; background: #9ca3af; border-radius: 50%; transition: transform 0.2s, background 0.2s; }
  .oo-toggle input:checked + .oo-slider { background: #166534; }
  .oo-toggle input:checked + .oo-slider:before { transform: translateX(15px); background: #22c55e; }
  @keyframes ooFlash { 0%,100% { background: transparent; } 30% { background: rgba(34,197,94,0.25); } }
  .oo-flash { animation: ooFlash 1.2s ease-out; }
  @keyframes ooPulse { 0%,100% { box-shadow: none; } 40% { box-shadow: 0 0 0 3px rgba(34,197,94,0.35); } }
  .oo-pulse { animation: ooPulse 0.6s ease-out; }
</style>
</head>
<body>
<h1>Trade tracker</h1>
<div id="health-banner">
  <strong>&#x26A0; System issues detected</strong><a href="/health">details &rarr;</a>
  <div class="hb-issues" id="hb-issues"></div>
</div>
<div class="meta"><span id="status"><span class="dot off"></span>connecting</span> &middot; <span id="sources">no sources</span> &middot; <span id="last">no events yet</span></div>
<div class="grid">
  <div class="col-left">
    <section>
      <h2>Open positions</h2>
      <table>
        <thead><tr><th>Source</th><th>Market</th><th>Side</th><th class="num">Entry</th><th class="num">Notional</th><th class="num">Unreal. P&amp;L</th><th class="num">SL</th><th class="num">TP</th></tr></thead>
        <tbody id="positions"></tbody>
      </table>
    </section>
    <section id="open-orders-section">
      <div class="oo-header">
        <h2>Open orders</h2>
        <div class="oo-toggle-wrap">
          <span>Show</span>
          <label class="oo-toggle">
            <input type="checkbox" id="oo-enabled" checked>
            <span class="oo-slider"></span>
          </label>
        </div>
      </div>
      <div id="oo-body">
        <table>
          <thead><tr><th>Source</th><th>Market</th><th>Side</th><th>Type</th><th class="num">Price</th><th class="num">Trigger</th><th class="num">Notional</th></tr></thead>
          <tbody id="open-orders"></tbody>
        </table>
        <div id="oo-footnote" style="font-size:10px;color:#6b7280;margin-top:6px"></div>
      </div>
    </section>
    <section>
      <h2>Telegram alerts &mdash; exactly what the bot sent</h2>
      <table>
        <thead><tr><th style="width:90px">Time IST</th><th style="width:70px">Type</th><th>Message</th></tr></thead>
        <tbody id="alerts"></tbody>
      </table>
    </section>
  </div>
  <div class="col-right">
    <h2>Recent events</h2>
    <table>
      <thead><tr><th>Time IST</th><th>Source</th><th>Kind</th><th>Market</th><th>Side</th><th class="num">Price</th><th class="num">Notional</th></tr></thead>
      <tbody id="events"></tbody>
    </table>
  </div>
</div>
<section class="analytics-section">
  <div class="analytics-section-header">
    <h2>Trade analytics</h2>
    <button class="btn-send-stats" id="send-stats-btn">&#128228; Send stats to Telegram</button>
  </div>
  <div id="analytics-no-trades" class="no-trades-msg" style="display:none">no closed trades yet</div>
  <div id="analytics-content">
    <div class="kpi-row" id="kpi-row"></div>
    <div class="charts-grid">
      <div class="chart-card">
        <div class="chart-card-title">Equity curve</div>
        <div class="chart-wrap"><canvas id="chart-equity"></canvas></div>
      </div>
      <div class="chart-card">
        <div class="chart-card-title">PnL per trade</div>
        <div class="chart-wrap"><canvas id="chart-pnl-per-trade"></canvas></div>
      </div>
      <div class="chart-card">
        <div class="chart-card-title">PnL by symbol</div>
        <div class="chart-wrap"><canvas id="chart-by-symbol"></canvas></div>
      </div>
      <div class="chart-card">
        <div class="chart-card-title">Win / loss</div>
        <div class="chart-wrap"><canvas id="chart-donut"></canvas></div>
      </div>
    </div>
  </div>
</section>
<section class="history-section">
  <h2>Closed trades / PnL history</h2>
  <div id="history-grid" class="history-grid"></div>
</section>
<script>
const toIST = ts => {
  const d = new Date(new Date(ts).getTime() + 5.5 * 60 * 60 * 1000);
  return d.toISOString().slice(11, 19);
};
// Short calendar label ("Jun 3") for chart date axes; "" if unparseable.
const toDateLabel = ts => {
  if (!ts) return "";
  const d = new Date(ts);
  if (isNaN(d.getTime())) return "";
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
};
const fmtNum = (s, d=2) => {
  const n = Number(s); if (!isFinite(n)) return s;
  return n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
};
const fmtSize = s => fmtNum(s, 4);
const fmtUsd  = s => "$" + fmtNum(s, 0);
const fmtPrice = s => "$" + (Number(s) >= 1000 ? fmtNum(s, 2) : fmtNum(s, 4));
const setStatus = (ok, label) => {
  document.getElementById("status").innerHTML =
    `<span class="dot ${ok ? 'on' : 'off'}"></span>${label}`;
};
const esc = s => String(s == null ? "" : s).replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const fmtPnl = v => {
  if (v == null || v === "") return "—";
  const n = Number(v); if (!isFinite(n)) return "—";
  const sign = n >= 0 ? "+" : "";
  return `<span style="color:${n >= 0 ? '#22c55e' : '#ef4444'}">${sign}$${Math.abs(n).toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2})}</span>`;
};
function renderPositions(positions) {
  const tb = document.getElementById("positions");
  if (!positions.length) { tb.innerHTML = '<tr><td colspan="8" class="empty">no open positions</td></tr>'; return; }
  tb.innerHTML = positions.map(p => {
    const entry = p.avg_entry_price;
    const size  = p.size;
    const fn    = p.footnote ? `<br><span style="font-size:10px;color:#6b7280">${esc(p.footnote)}</span>` : "";
    return `<tr>
      <td>${esc(p.source)}</td>
      <td>${esc(p.market_symbol)}</td>
      <td class="${p.side}">${p.side.toUpperCase()}</td>
      <td class="num">${fmtPrice(entry)}${fn}</td>
      <td class="num">${fmtUsd(Number(size) * Number(entry))}</td>
      <td class="num">${fmtPnl(p.unrealized_pnl)}</td>
      <td class="num">${p.sl_price != null ? fmtPrice(p.sl_price) : "—"}</td>
      <td class="num">${p.tp_price != null ? fmtPrice(p.tp_price) : "—"}</td>
    </tr>`;
  }).join("");
}
function renderEvents(events) {
  const tb = document.getElementById("events");
  if (!events.length) { tb.innerHTML = '<tr><td colspan="7" class="empty">waiting for trades…</td></tr>'; return; }
  tb.innerHTML = events.map(e => {
    const t = e.trade;
    // Prefer display fields (present for HL events); fall back to real fields.
    const disp = e._disp || {};
    const time = disp.ts ?? toIST(t.timestamp);
    const price = disp.price ?? t.price;
    const size  = disp.size  ?? t.size;
    // For HL events prefer the pre-computed privacy notional; for Lighter compute from real fields.
    const notional = disp.notional ?? (Number(size) * Number(price));
    const fn = disp.footnote ? ` <span style="font-size:10px;color:#6b7280">${esc(disp.footnote)}</span>` : "";
    return `<tr>
      <td>${time}</td>
      <td>${esc(t.source)}</td>
      <td class="kind-${e.kind}">${e.kind}</td>
      <td>${esc(t.market_symbol)}</td>
      <td class="${t.side}">${t.side.toUpperCase()}</td>
      <td class="num">${fmtPrice(price)}${fn}</td>
      <td class="num">${fmtUsd(notional)}</td>
    </tr>`;
  }).join("");
}
function renderAlerts(alerts) {
  const tb = document.getElementById("alerts");
  const a = alerts || [];
  if (!a.length) { tb.innerHTML = '<tr><td colspan="3" class="empty">no alerts sent yet</td></tr>'; return; }
  tb.innerHTML = a.map(x => `
    <tr>
      <td class="num">${toIST(x.ts)}</td>
      <td><span class="badge badge-${x.kind === 'card' ? 'card' : 'text'}">${x.kind === 'card' ? 'CARD' : 'TEXT'}</span></td>
      <td class="alert-msg">${esc(x.text)}</td>
    </tr>`).join("");
}
function renderSources(sources) {
  const s = sources || [];
  document.getElementById("sources").textContent =
    s.length ? s.length + " source" + (s.length > 1 ? "s" : "") + ": " + s.join(", ") : "no sources";
}
function renderClosedTrades(trades) {
  const grid = document.getElementById("history-grid");
  const arr = trades || [];
  if (!arr.length) {
    grid.innerHTML = '<span style="color:#4b5563;font-style:italic;font-size:12px">no closed trades yet</span>';
    return;
  }
  grid.innerHTML = arr.map(tr => {
    const isWin = tr.is_win === 1 || tr.is_win === true;
    const pnlN  = tr.pnl  != null ? Number(tr.pnl)  : null;
    const pctN  = tr.pct  != null ? Number(tr.pct)   : null;
    const pnlColor = (pnlN == null ? '#9ca3af' : pnlN >= 0 ? '#22c55e' : '#ef4444');
    const pnlSign  = (pnlN == null ? '' : pnlN >= 0 ? '+' : '−');
    const pnlStr   = pnlN == null ? '—' : pnlSign + '$' + Math.abs(pnlN).toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2});
    const pctStr   = pctN == null ? '' : (pctN >= 0 ? '+' : '') + pctN.toFixed(2) + '%';
    const sideClass = (tr.side || '').toLowerCase() === 'long' ? 'green' : 'red';
    const _rk = (tr.realization_kind || '').toUpperCase();
    const _rkLabel = _rk === 'OPEN' ? ' · IN PROGRESS' : (_rk === 'PARTIAL' ? ' · PARTIAL CLOSE' : ' · CLOSED');
    const sideLabel = ((tr.side || '').toUpperCase()) + _rkLabel;
    const levStr  = tr.leverage ? tr.leverage + 'x' : '—';
    const wrStr   = (tr.wins != null && tr.total != null && tr.total > 0) ? tr.wins + '/' + tr.total : '—';
    // Prefer privacy-transformed display fields when present (HL trades).
    const timeStr = tr.ts_disp   ?? (tr.ts ? toIST(tr.ts) : '');
    const entryStr = (tr.entry_disp ?? tr.entry) ? fmtPrice(tr.entry_disp ?? tr.entry) : '—';
    const exitStr  = (tr.exit_disp  ?? tr.exit)  ? fmtPrice(tr.exit_disp  ?? tr.exit)  : '—';
    const notStr   = (tr.notional_disp ?? tr.notional) ? fmtUsd(tr.notional_disp ?? tr.notional) : '—';
    const tileFootnote = tr.footnote ? `<div style="font-size:10px;color:#6b7280;margin-top:4px">${esc(tr.footnote)}</div>` : '';
    let thumbHtml = '';
    if (tr.card_path) {
      thumbHtml = `<div class="tile-thumb-wrap"><img class="card-thumb" src="${esc(tr.card_path)}" loading="lazy" alt="PnL card" onclick="window.open('${esc(tr.card_path)}','_blank')"></div>`;
    }
    return `<div class="trade-tile ${isWin ? 'win' : 'loss'}">
  <div class="tile-header">${esc(tr.source || '')} &middot; ${esc(tr.market_symbol || '')}</div>
  <div class="tile-direction ${sideClass}">${sideLabel}</div>
  <div class="tile-pnl" style="color:${pnlColor}">${pnlStr}</div>
  ${pctStr ? `<div class="tile-pct" style="color:${pnlColor}">${pctStr}</div>` : ''}
  <div class="tile-details">
    <span class="tile-detail-label">ENTRY</span><span class="tile-detail-val">${entryStr}</span>
    <span class="tile-detail-label">EXIT</span><span class="tile-detail-val">${exitStr}</span>
    <span class="tile-detail-label">NOTIONAL</span><span class="tile-detail-val">${notStr}</span>
    <span class="tile-detail-label">LEVERAGE</span><span class="tile-detail-val">${levStr}</span>
    <span class="tile-detail-label">WIN RATE</span><span class="tile-detail-val">${wrStr}</span>
  </div>
  ${thumbHtml}
  ${tileFootnote}
  <div class="tile-footer"><span>${timeStr} IST</span></div>
</div>`;
  }).join("");
}
const fmtSignedUsd = n => {
  if (n == null || !isFinite(Number(n))) return "—";
  const v = Number(n);
  const sign = v >= 0 ? "+" : "−";
  return sign + "$" + Math.abs(v).toLocaleString("en-US", {minimumFractionDigits:2, maximumFractionDigits:2});
};
// Chart instance cache — reused/updated on each renderStats call to avoid leaks
let _chartEquity = null;
let _chartPnlBar = null;
let _chartBySymbol = null;
let _chartDonut = null;
const CHART_DEFAULTS = {
  color: "#9ca3af",
  borderColor: "#1f242c",
  font: { family: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace", size: 10 },
};
function _chartBaseOpts(extra) {
  return Object.assign({
    responsive: true,
    maintainAspectRatio: false,
    animation: { duration: 300 },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: "#1f242c",
        titleColor: "#9ca3af",
        bodyColor: "#d8dbe0",
        borderColor: "#374151",
        borderWidth: 1,
        titleFont: CHART_DEFAULTS.font,
        bodyFont: CHART_DEFAULTS.font,
      },
    },
    scales: {
      x: {
        ticks: { color: "#9ca3af", font: CHART_DEFAULTS.font, maxRotation: 0 },
        grid: { color: "#1f242c" },
        border: { color: "#1f242c" },
      },
      y: {
        ticks: { color: "#9ca3af", font: CHART_DEFAULTS.font },
        grid: { color: "#1f242c" },
        border: { color: "#1f242c" },
      },
    },
  }, extra || {});
}
function renderStats(stats) {
  if (!stats) return;
  const noTrades = !stats.n_trades;
  const noEl = document.getElementById("analytics-no-trades");
  const contEl = document.getElementById("analytics-content");
  if (noTrades) {
    noEl.style.display = "";
    contEl.style.display = "none";
    return;
  }
  noEl.style.display = "none";
  contEl.style.display = "";

  // --- KPI cards ---
  const GREEN = "#22c55e", RED = "#ef4444", MUTED = "#9ca3af";
  const kpis = [
    { label: "Net P&L",       value: fmtSignedUsd(stats.total_pnl),   color: stats.total_pnl >= 0 ? GREEN : RED },
    { label: "Win rate",      value: (Number(stats.win_rate)||0).toFixed(1) + "%", sub: stats.wins + "/" + stats.n_trades + " trades", color: stats.win_rate >= 50 ? GREEN : RED },
    { label: "Profit factor", value: stats.profit_factor == null ? (stats.wins > 0 ? "∞" : "—") : Number(stats.profit_factor).toFixed(2), color: stats.profit_factor == null ? (stats.wins > 0 ? GREEN : MUTED) : (stats.profit_factor >= 1 ? GREEN : RED) },
    { label: "# Trades",      value: String(stats.n_trades), color: MUTED },
    { label: "Avg win",       value: fmtSignedUsd(stats.avg_win),   color: GREEN },
    { label: "Avg loss",      value: fmtSignedUsd(stats.avg_loss),  color: RED  },
    { label: "Best",          value: fmtSignedUsd(stats.largest_win),  color: GREEN },
    { label: "Worst",         value: fmtSignedUsd(stats.largest_loss), color: RED  },
    { label: "Max drawdown",  value: fmtSignedUsd(stats.max_drawdown), color: RED  },
  ];
  const kpiRow = document.getElementById("kpi-row");
  kpiRow.innerHTML = kpis.map(k =>
    '<div class="kpi-card">' +
      '<div class="kpi-label">' + esc(k.label) + '</div>' +
      '<div class="kpi-value" style="color:' + k.color + '">' + esc(k.value) + '</div>' +
      (k.sub ? '<div class="kpi-sub">' + esc(k.sub) + '</div>' : '') +
    '</div>'
  ).join("");

  // --- Equity curve (x-axis = trade date) ---
  const eqData = (stats.equity_curve || []);
  const eqLabels = eqData.map(p => toDateLabel(p.ts));
  const eqValues = eqData.map(p => Number(p.cum_pnl));
  const finalPnl = eqValues.length ? eqValues[eqValues.length - 1] : 0;
  const eqColor = finalPnl >= 0 ? GREEN : RED;
  const eqFill = finalPnl >= 0 ? "rgba(34,197,94,0.12)" : "rgba(239,68,68,0.12)";
  const eqOpts = _chartBaseOpts();
  eqOpts.plugins.tooltip.callbacks = {
    title: items => (items.length ? toDateLabel(eqData[items[0].dataIndex].ts) : ""),
    label: item => "Cum P&L: " + fmtSignedUsd(item.parsed.y),
  };
  // Avoid an unreadable wall of date ticks when there are many trades.
  eqOpts.scales.x.ticks.autoSkip = true;
  eqOpts.scales.x.ticks.maxTicksLimit = 10;
  if (_chartEquity) {
    _chartEquity.data.labels = eqLabels;
    _chartEquity.data.datasets[0].data = eqValues;
    _chartEquity.data.datasets[0].borderColor = eqColor;
    _chartEquity.data.datasets[0].backgroundColor = eqFill;
    _chartEquity.options = eqOpts;
    _chartEquity.update();
  } else {
    const ctx = document.getElementById("chart-equity").getContext("2d");
    _chartEquity = new Chart(ctx, {
      type: "line",
      data: {
        labels: eqLabels,
        datasets: [{
          data: eqValues,
          borderColor: eqColor,
          backgroundColor: eqFill,
          borderWidth: 2,
          pointRadius: eqValues.length > 60 ? 0 : 3,
          pointHoverRadius: 4,
          fill: true,
          tension: 0.3,
        }],
      },
      options: eqOpts,
    });
  }

  // --- PnL per trade bar chart (x-axis = ticker) ---
  const pnlSeries = (stats.pnl_series || []);
  const pnlLabels = pnlSeries.map(p => p.symbol || "?");
  const pnlValues = pnlSeries.map(p => Number(p.pnl));
  const pnlColors = pnlSeries.map(p => (p.pnl >= 0 ? "rgba(34,197,94,0.75)" : "rgba(239,68,68,0.75)"));
  const pnlOpts = _chartBaseOpts();
  pnlOpts.plugins.tooltip.callbacks = {
    title: items => {
      if (!items.length) return "";
      const p = pnlSeries[items[0].dataIndex] || {};
      const d = toDateLabel(p.ts);
      return (p.symbol || "?") + (p.side ? " · " + String(p.side).toUpperCase() : "") + (d ? " · " + d : "");
    },
    label: item => "P&L: " + fmtSignedUsd(item.parsed.y),
  };
  pnlOpts.scales.x.ticks.autoSkip = true;
  pnlOpts.scales.x.ticks.maxTicksLimit = 24;
  if (_chartPnlBar) {
    _chartPnlBar.data.labels = pnlLabels;
    _chartPnlBar.data.datasets[0].data = pnlValues;
    _chartPnlBar.data.datasets[0].backgroundColor = pnlColors;
    _chartPnlBar.options = pnlOpts;
    _chartPnlBar.update();
  } else {
    const ctx2 = document.getElementById("chart-pnl-per-trade").getContext("2d");
    _chartPnlBar = new Chart(ctx2, {
      type: "bar",
      data: {
        labels: pnlLabels,
        datasets: [{
          data: pnlValues,
          backgroundColor: pnlColors,
          borderWidth: 0,
          borderRadius: 2,
        }],
      },
      options: pnlOpts,
    });
  }

  // --- PnL by symbol horizontal bar ---
  const bySymbol = (stats.by_symbol || []);
  const symLabels = bySymbol.map(s => s.symbol);
  const symValues = bySymbol.map(s => Number(s.pnl));
  const symColors = symValues.map(v => v >= 0 ? "rgba(34,197,94,0.75)" : "rgba(239,68,68,0.75)");
  const symOpts = _chartBaseOpts({
    indexAxis: "y",
    scales: {
      x: {
        ticks: { color: "#9ca3af", font: CHART_DEFAULTS.font },
        grid: { color: "#1f242c" },
        border: { color: "#1f242c" },
      },
      y: {
        ticks: { color: "#9ca3af", font: CHART_DEFAULTS.font },
        grid: { color: "#1f242c" },
        border: { color: "#1f242c" },
      },
    },
  });
  if (_chartBySymbol) {
    _chartBySymbol.data.labels = symLabels;
    _chartBySymbol.data.datasets[0].data = symValues;
    _chartBySymbol.data.datasets[0].backgroundColor = symColors;
    _chartBySymbol.update();
  } else {
    const ctx3 = document.getElementById("chart-by-symbol").getContext("2d");
    _chartBySymbol = new Chart(ctx3, {
      type: "bar",
      data: {
        labels: symLabels,
        datasets: [{
          data: symValues,
          backgroundColor: symColors,
          borderWidth: 0,
          borderRadius: 2,
        }],
      },
      options: symOpts,
    });
  }

  // --- Win/loss donut ---
  const donutData = [stats.wins || 0, stats.losses || 0];
  if (_chartDonut) {
    _chartDonut.data.datasets[0].data = donutData;
    _chartDonut.update();
  } else {
    const ctx4 = document.getElementById("chart-donut").getContext("2d");
    _chartDonut = new Chart(ctx4, {
      type: "doughnut",
      data: {
        labels: ["Wins", "Losses"],
        datasets: [{
          data: donutData,
          backgroundColor: ["rgba(34,197,94,0.8)", "rgba(239,68,68,0.8)"],
          borderColor: ["#22c55e", "#ef4444"],
          borderWidth: 1,
          hoverOffset: 6,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 300 },
        cutout: "60%",
        plugins: {
          legend: {
            display: true,
            position: "bottom",
            labels: { color: "#9ca3af", font: CHART_DEFAULTS.font, padding: 12, boxWidth: 12 },
          },
          tooltip: {
            backgroundColor: "#1f242c",
            titleColor: "#9ca3af",
            bodyColor: "#d8dbe0",
            borderColor: "#374151",
            borderWidth: 1,
            titleFont: CHART_DEFAULTS.font,
            bodyFont: CHART_DEFAULTS.font,
          },
        },
      },
    });
  }
}

// --- Open orders panel ---
const _ooSeenIds = new Set();
let _ooEnabled = localStorage.getItem("ooEnabled") !== "false";
const _ooToggle = document.getElementById("oo-enabled");
const _ooBody   = document.getElementById("oo-body");
const _ooSection = document.getElementById("open-orders-section");

// Apply initial toggle state from localStorage
_ooToggle.checked = _ooEnabled;
_ooBody.style.display = _ooEnabled ? "" : "none";

_ooToggle.addEventListener("change", function() {
  _ooEnabled = this.checked;
  localStorage.setItem("ooEnabled", _ooEnabled ? "true" : "false");
  if (_ooEnabled) {
    _ooBody.style.display = "";
    _ooSection.classList.remove("oo-pulse");
    // Force reflow to re-trigger animation
    void _ooSection.offsetWidth;
    _ooSection.classList.add("oo-pulse");
    _ooSection.addEventListener("animationend", () => _ooSection.classList.remove("oo-pulse"), { once: true });
  } else {
    _ooBody.style.display = "none";
  }
});

function renderOpenOrders(orders) {
  const tb = document.getElementById("open-orders");
  const fnEl = document.getElementById("oo-footnote");
  const arr = orders || [];
  if (!arr.length) {
    tb.innerHTML = '<tr><td colspan="7" class="empty">no resting orders</td></tr>';
    fnEl.textContent = "";
    return;
  }
  let footnoteTxt = "";
  tb.innerHTML = arr.map(o => {
    const isNew = o.order_id != null && !_ooSeenIds.has(String(o.order_id));
    if (o.order_id != null) _ooSeenIds.add(String(o.order_id));
    const flashClass = isNew ? " oo-flash" : "";
    const sideClass  = o.side === "long" ? "long" : "short";
    const price    = o.price      != null ? fmtPrice(o.price)      : "—";
    const trigger  = o.trigger_px != null ? fmtPrice(o.trigger_px) : "—";
    const notional = o.notional   != null ? fmtUsd(o.notional)     : "—";
    if (o.footnote) footnoteTxt = o.footnote;
    return `<tr class="${flashClass}">
      <td>${esc(o.source)}</td>
      <td>${esc(o.market_symbol)}</td>
      <td class="${sideClass}">${o.side.toUpperCase()}</td>
      <td>${esc(o.order_kind)}</td>
      <td class="num">${price}</td>
      <td class="num">${trigger}</td>
      <td class="num">${notional}</td>
    </tr>`;
  }).join("");
  fnEl.textContent = footnoteTxt;
}

// --- Send stats to Telegram button ---
document.getElementById("send-stats-btn").addEventListener("click", function() {
  const btn = this;
  btn.disabled = true;
  btn.textContent = "Sending…";
  fetch("/api/send_stats", { method: "POST" })
    .then(r => r.json())
    .then(d => {
      if (d.ok) {
        btn.textContent = "✓ Sent";
      } else {
        btn.textContent = d.error || "error";
      }
      setTimeout(() => { btn.disabled = false; btn.textContent = "📤 Send stats to Telegram"; }, 2000);
    })
    .catch(err => {
      btn.textContent = "network error";
      setTimeout(() => { btn.disabled = false; btn.textContent = "📤 Send stats to Telegram"; }, 2000);
    });
});

function renderHealth(health) {
  const banner = document.getElementById("health-banner");
  const issuesEl = document.getElementById("hb-issues");
  if (!health || health.ok) { banner.style.display = "none"; return; }
  const bad = (health.components || []).filter(c => c.status === "down" || c.status === "degraded");
  if (!bad.length) { banner.style.display = "none"; return; }
  issuesEl.innerHTML = bad.map(c => {
    const label = c.status === "down" ? "not working" : "degraded";
    return `<span class="hb-issue">&#x26A0; ${esc(c.component)} ${esc(label)}</span>`;
  }).join("");
  banner.style.display = "";
}
function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = () => setStatus(true, "connected");
  ws.onclose = () => { setStatus(false, "disconnected — retrying"); setTimeout(connect, 2000); };
  ws.onerror = () => setStatus(false, "error");
  ws.onmessage = (msg) => {
    const data = JSON.parse(msg.data);
    if (data.type === "snapshot") {
      renderSources(data.sources);
      renderPositions(data.positions);
      renderOpenOrders(data.open_orders || []);
      renderEvents(data.recent_events);
      renderAlerts(data.tg_alerts);
      renderClosedTrades(data.closed_trades);
      if (data.stats) renderStats(data.stats);
      if (data.health) renderHealth(data.health);
      if (data.recent_events.length) {
        document.getElementById("last").textContent = "last event " + data.recent_events[0].trade.timestamp;
      }
    } else if (data.type === "event") {
      renderSources(data.sources);
      renderPositions(data.positions);
      renderOpenOrders(data.open_orders || []);
      renderEvents(data.recent_events);
      renderAlerts(data.tg_alerts);
      renderClosedTrades(data.closed_trades);
      if (data.stats) renderStats(data.stats);
      if (data.health) renderHealth(data.health);
      document.getElementById("last").textContent = "last event " + data.event.trade.timestamp;
    }
  };
}
connect();
</script>
</body>
</html>
"""


class Hub:
    """Tracks connected dashboard websockets and broadcasts updates."""

    def __init__(self) -> None:
        self._clients: set[web.WebSocketResponse] = set()

    def add(self, ws: web.WebSocketResponse) -> None:
        self._clients.add(ws)

    def remove(self, ws: web.WebSocketResponse) -> None:
        self._clients.discard(ws)

    async def broadcast(self, payload: dict) -> None:
        if not self._clients:
            return
        msg = json.dumps(_to_jsonable(payload))
        dead: list[web.WebSocketResponse] = []
        for ws in self._clients:
            try:
                await ws.send_str(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.remove(ws)


async def run() -> None:
    load_dotenv()

    if not _acquire_pid_lock():
        return
    try:
        await _run()
    finally:
        _release_pid_lock()


async def _run() -> None:
    cfg = load_settings()
    hub = Hub()

    # ── Health / status registry ──────────────────────────────────────────────
    health = HealthRegistry(started_at_iso=datetime.now(timezone.utc).isoformat())

    sources: list[Source] = load_sources(settings=cfg)
    by_id: dict[str, Source] = {s.id: s for s in sources}

    # ── Privacy transform params (built once; ValueError on bad mag fails loudly) ──
    privacy = PrivacyParams(
        enabled=cfg.privacy_enabled,
        secret=cfg.privacy_secret_key,
        mag=cfg.privacy_mag,
        entry_quantum_pct=cfg.privacy_entry_quantum_pct,
        size_sigfigs=cfg.privacy_size_sigfigs,
        notional_sigfigs=cfg.privacy_notional_sigfigs,
        time_bucket=cfg.privacy_time_bucket,
        disclose_footnote=cfg.privacy_disclose_footnote,
    )

    log.info("initialising database…")
    await init_db(DB_PATH)
    health.mark_up("database")

    # Backfill closed_trades from events (idempotent — no-op if already populated)
    backfill_count = await backfill_closed_trades_from_events(DB_PATH)
    if backfill_count:
        log.info("backfilled %d closed trades from events table", backfill_count)

    # recent_events holds Event objects (new this session) and dicts (loaded from DB).
    # _to_jsonable handles both transparently.
    recent_events: list[Any] = list(await load_recent_events(DB_PATH, cfg.max_recent_events))
    log.info("loaded %d persisted events from db", len(recent_events))

    # Load full history for stats when cfg.stats_full_history; cap the UI payload slice.
    if cfg.stats_full_history:
        _all_closed_trades: list[dict] = list(await load_closed_trades(DB_PATH, None))
        closed_trades: list[dict] = _all_closed_trades  # keep full list; sliced in payload
        log.info("loaded %d closed trades (full history) from db", len(closed_trades))
    else:
        closed_trades: list[dict] = list(await load_closed_trades(DB_PATH, cfg.max_closed_trades))
        log.info("loaded %d closed trades from db", len(closed_trades))

    # Boot dedup set: union of all fill ids ever recorded — prevents re-recording
    # the same fill after a restart (e.g. reduce batch flushed before crash).
    _recorded_realizations: set[int] = set(await load_recorded_fill_ids(DB_PATH))

    # ── Re-derive privacy display fields for DB-loaded closed trades ──────────
    # Display fields (entry_disp, …) are in-memory only; after a restart the
    # history grid loads real DB rows and the JS "?? tr.entry" fallback would
    # leak REAL Hyperliquid prices on an internet-reachable port. Re-derive them
    # here so the public grid never shows real HL values across restarts.
    # DB rows store the source NAME; the privacy factor is seeded by source ID
    # (stable across restarts), so map name → id. Lighter/unknown rows are never
    # touched (no disp keys → JS keeps real values, which are intentionally
    # public for Lighter).
    _hl_name_to_id = {s.name: s.id for s in sources if s.is_hyperliquid}

    def _derive_hl_disp_fields(row: dict) -> None:
        """Populate HL privacy ``*_disp`` fields on a closed-trade row in place.

        The public grid uses ``tr.<field>_disp ?? tr.<field>`` — without the
        disp fields an internet-reachable port would leak REAL Hyperliquid
        prices. We seed the jitter from the row's own (real) entry so the
        displayed values are internally consistent across restarts. Lighter /
        unknown rows are left untouched (their real values are intentionally
        public). Re-derives even if disp fields exist, since aggregated rows
        carry stale per-fill disp that must be recomputed from the aggregate.
        """
        try:
            sid = _hl_name_to_id.get(row.get("source"))
            if sid is None:
                return  # Lighter/unknown — leave real values untouched.
            if not (privacy.enabled and row.get("entry")):
                return
            real_entry = Decimal(str(row["entry"]))
            dv = disp_view(
                privacy,
                True,
                sid,
                row.get("market_symbol", ""),
                row.get("side") or "",
                real_entry,
                entry=real_entry,
                exit=Decimal(str(row["exit"])) if row.get("exit") else None,
                size=Decimal(str(row["size"])) if row.get("size") else None,
                notional=Decimal(str(row["notional"])) if row.get("notional") else None,
                ts=row.get("ts"),
                now=datetime.now(timezone.utc),
            )
            if "entry" in dv:
                row["entry_disp"] = str(dv["entry"])
            if "exit" in dv:
                row["exit_disp"] = str(dv["exit"])
            if "size" in dv:
                row["size_disp"] = str(dv["size"])
            if "notional" in dv:
                row["notional_disp"] = str(dv["notional"])
            row["ts_disp"] = dv.get("ts")
            row["is_hl"] = True
            row["footnote"] = dv.get("footnote", "")
        except Exception:
            log.exception("failed to derive privacy disp fields for closed trade row")

    for row in closed_trades:
        # Newly-closed in-memory rows already carry frozen-anchor disp fields.
        if not row.get("entry_disp"):
            _derive_hl_disp_fields(row)

    # ── Stats display window + round-trip aggregation ────────────────────────
    # The recorder writes one row per realization (each scale-out + the final
    # close). For display we collapse each round-trip into ONE entry via
    # aggregate_round_trips (closed trade = total of all its scale-outs; an
    # in-progress position = its realized-so-far), then re-derive HL disp on the
    # aggregates, then apply the [start,end]+symbols window. Used by BOTH the
    # analytics and the history grid so they always agree.
    #   include_open=True  → grid (shows in-progress round-trips, flagged OPEN)
    #   include_open=False → stats (CLOSED-only, per the "closed trades only" rule)
    def display_trades(include_open: bool = True) -> list[dict]:
        agg = aggregate_round_trips(closed_trades)
        for row in agg:
            _derive_hl_disp_fields(row)
        if not include_open:
            agg = [r for r in agg if (r.get("realization_kind") or "").upper() == "FULL"]
        return filter_trades(
            agg,
            start_date=cfg.stats_start_date,
            end_date=cfg.stats_end_date,
            symbols=cfg.stats_symbols,
        )

    if cfg.stats_start_date or cfg.stats_end_date or cfg.stats_symbols:
        log.info(
            "stats window — start=%s  end=%s  symbols=%s",
            cfg.stats_start_date or "(none)",
            cfg.stats_end_date or "(now)",
            list(cfg.stats_symbols) or "(all)",
        )

    # Cached trade stats — recomputed after each close, served in snapshot payload.
    # Stored as a mutable dict so inner closures can update it in-place.
    stats_state: dict = compute_stats(display_trades(include_open=False))

    def refresh_stats() -> None:
        """Recompute trade stats from the CLOSED-only round-trip view."""
        stats_state.clear()
        stats_state.update(compute_stats(display_trades(include_open=False)))

    # ── Privacy anchor store: [source_id][market_id] → frozen Decimal entry price ──
    # Seeded at OPEN; cleared (after reading) at CLOSE.
    # Ensures the jitter factor stays constant across scale-ins and the close card.
    _privacy_anchor: dict[str, dict[int, Decimal]] = {}

    # Directory for PnL card PNG files — must exist before static route is added.
    cards_dir = DB_PATH.parent / "cards"
    cards_dir.mkdir(parents=True, exist_ok=True)

    # Dashboard-level position view: API truth, updated by the reconciler.
    # Kept separate from src.tracker (fill-based classifier) so the reconciler
    # doesn't corrupt event classification when it seeds positions before fills arrive.
    _dash_positions: dict[str, dict[int, Position]] = {}

    # Positions for which the reconciler sent an OPEN alert before the fill arrived.
    # The fill-based OPEN/SIZE_CHANGE handler skips alerting for these keys so the
    # user never sees a duplicate "Opened" message.
    _reconciler_alerted_opens: set[tuple[str, int]] = set()

    # Positions for which a one-time "SL/TP set" TG alert has already been sent
    # (or that existed at startup, pre-armed below). Declared before bootstrap
    # because the bootstrap loop arms it.
    _sl_tp_alerted: set[tuple[str, int]] = set()

    # Bootstrap every source: markets, seed positions, anchor last_trade_id so
    # we don't replay history.
    #
    # Each source is wrapped in an individual try/except so a Binance failure
    # (e.g. all proxies dead) never prevents HL/Lighter from starting.
    # Failed sources are marked down in health and omitted from the active loop.
    _active_sources: list[Source] = []
    for s in sources:
        try:
            # Binance: quick ping through the proxy pool before full bootstrap.
            # If the pool has no working proxy we skip the source immediately
            # rather than letting every subsequent REST call time out.
            if hasattr(s.client, "ping") and hasattr(s.client, "_proxy_pool"):
                pool = s.client._proxy_pool
                if pool is not None:
                    # Update proxy-pool health status
                    n_alive = pool.n_alive
                    n_total = pool.n_total
                    if n_total:
                        pool_status = "up" if n_alive > 0 else "down"
                        health.set(
                            "binance_proxies",
                            pool_status,
                            detail=f"{n_alive}/{n_total} alive",
                        )
                    reachable = await s.client.ping()
                    if not reachable:
                        # Re-check after ping attempt
                        n_alive = pool.n_alive
                        health.set(
                            "binance_proxies",
                            "down" if n_alive == 0 else "degraded",
                            detail=f"{n_alive}/{n_total} alive after ping",
                        )
                        health.mark_down(
                            f"source:{s.name}",
                            "no working proxy / Binance unreachable at bootstrap",
                        )
                        log.warning(
                            "[%s] Binance unreachable at bootstrap (all proxies dead?) — "
                            "skipping source; HL/Lighter unaffected",
                            s.name,
                        )
                        continue

            log.info("[%s] bootstrapping markets…", s.name)
            await s.client.bootstrap_markets()
            init_pos = await s.client.current_positions()
            init_pos = {mid: p for mid, p in init_pos.items() if not s.is_excluded(p.market_symbol)}
            s.tracker.seed(init_pos)
            _dash_positions[s.id] = init_pos
            log.info("[%s] seeded with %d positions", s.name, len(init_pos))
            # Arm every already-open position so the reconciler never fires a SL/TP alert
            # for positions that existed before this bot session started.
            for mid in init_pos:
                _sl_tp_alerted.add((s.id, mid))
            # Seed privacy anchors for pre-existing HL positions using current avg_entry_price
            if s.is_hyperliquid and init_pos:
                _privacy_anchor.setdefault(s.id, {})
                for mid, pos in init_pos.items():
                    if mid not in _privacy_anchor[s.id]:
                        _privacy_anchor[s.id][mid] = pos.avg_entry_price
            latest = await s.client.fetch_trades_since(since_trade_id=None, limit=1)
            if latest:
                s.last_trade_id = latest[-1].trade_id
                log.info("[%s] anchored last_trade_id=%d", s.name, s.last_trade_id)
            # Tell HL client the anchor so WS snapshot is filtered correctly
            if hasattr(s.client, "set_anchor"):
                s.client.set_anchor(s.last_trade_id)
            health.mark_up(f"source:{s.name}")
            _active_sources.append(s)

        except Exception as _bootstrap_exc:
            log.error(
                "[%s] bootstrap failed — source skipped; bot continues with remaining sources. "
                "Error: %s",
                s.name,
                _bootstrap_exc,
                exc_info=True,
            )
            health.mark_down(f"source:{s.name}", str(_bootstrap_exc))

    # Replace the sources list with only those that bootstrapped successfully.
    # HL/Lighter sources that already appended to _active_sources are unaffected.
    sources = _active_sources
    by_id   = {s.id: s for s in sources}

    def _anchor(src: "Source", market_id: int, fallback_entry: Decimal) -> Decimal:
        """Return the frozen open-time anchor entry for (src, market_id).

        Falls back to fallback_entry (the live avg_entry_price) when the anchor
        has not been set yet — this is safe because it only happens on the very
        first OPEN message before the anchor is written.
        """
        return _privacy_anchor.get(src.id, {}).get(market_id) or fallback_entry

    # --- Telegram ---
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_channel = os.environ.get("TELEGRAM_CHANNEL_ID", "")
    tg_client = httpx.AsyncClient(timeout=15.0)
    AGGREGATE_WINDOW = cfg.aggregate_window_seconds

    # SIZE_CHANGE aggregate buffer
    # (source_id, market_id) -> {net_added, n_fills, leverage, position, task}
    _pending: dict[tuple[str, int], dict] = {}

    # REDUCE aggregate buffer — same structure, separate dict
    # (source_id, market_id) -> {net_reduced, n_fills, total_pnl, leverage, position, task}
    _pending_reduces: dict[tuple[str, int], dict] = {}

    # SL/TP cache for dashboard display
    # (source_id, market_id) -> (sl_price, tp_price)  — both Optional[Decimal]
    # Populated by the reconciler and on OPEN events; purged on CLOSE.
    _sl_tp_cache: dict[tuple[str, int], tuple] = {}

    # Open orders cache: source_id -> list of jsonable order dicts
    # Populated by the reconciler when cfg.open_orders_enabled is True.
    _open_orders: dict[str, list[dict]] = {}

    # NOTE: _sl_tp_alerted is declared earlier (before the bootstrap loop, which
    # arms it for pre-existing positions).

    # Dedup guard: MD5(alert text) -> monotonic timestamp of last send
    _tg_sent: dict[str, float] = {}

    # Rolling log of alerts actually delivered to Telegram, surfaced on the
    # dashboard so the bot's output can be compared against positions/events at
    # a glance. Persisted to DB and loaded on restart; newest first.
    TG_ALERTS_MAX = 100
    _tg_alerts: list[dict] = list(await load_tg_alerts(DB_PATH, TG_ALERTS_MAX))

    async def _record_tg_alert(kind: str, text: str) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": kind,   # "text" | "card"
            "text": text,
        }
        _tg_alerts.insert(0, record)
        del _tg_alerts[TG_ALERTS_MAX:]
        try:
            await save_tg_alert(DB_PATH, record["ts"], record["kind"], record["text"])
        except Exception:
            log.exception("failed to persist tg alert")

    async def tg_send(text: str) -> None:
        h = hashlib.md5(text.encode()).hexdigest()
        now = time.monotonic()
        dedup_window = cfg.tg_dedup_window_seconds
        # Evict expired entries to prevent unbounded growth
        expired = [k for k, t in _tg_sent.items() if now - t > dedup_window]
        for k in expired:
            del _tg_sent[k]
        if h in _tg_sent:
            log.warning("tg_send: suppressed duplicate alert (%.0fs since last send, window=%ds)",
                        now - _tg_sent[h], dedup_window)
            return
        _tg_sent[h] = now
        await _record_tg_alert("text", text)
        try:
            r = await tg_client.post(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                data={"chat_id": tg_channel, "text": text},
            )
            if not r.json().get("ok"):
                log.warning("tg sendMessage failed: %s", r.text[:200])
                health.mark_down("telegram", error=f"sendMessage API error: {r.text[:120]}")
            else:
                health.mark_up("telegram")
        except Exception as _tg_exc:
            log.exception("tg_send failed")
            health.mark_down("telegram", error=str(_tg_exc))
        # Push the new alert to dashboard clients live (alerts can fire from the
        # aggregate flush / reconciler at times with no other broadcast).
        await hub.broadcast(snapshot_payload("snapshot"))

    async def tg_send_photo(image_bytes: bytes, caption: str = "", log_text: str = "") -> None:
        """Send a PNG image to Telegram. Falls back to plain text on error.

        log_text is the human-readable line recorded in the dashboard alert log
        (the image itself can't be shown there); falls back to the caption.
        """
        await _record_tg_alert("card", log_text or caption or "PnL card")
        try:
            r = await tg_client.post(
                f"https://api.telegram.org/bot{tg_token}/sendPhoto",
                data={"chat_id": tg_channel, "caption": caption},
                files={"photo": ("card.png", image_bytes, "image/png")},
            )
            if not r.json().get("ok"):
                log.warning("tg sendPhoto failed: %s", r.text[:200])
                if caption:
                    await tg_send(caption)
        except Exception:
            log.exception("tg_send_photo failed")
            if caption:
                await tg_send(caption)
        await hub.broadcast(snapshot_payload("snapshot"))

    async def _get_sl_tp(src: Source, market_id: int):
        """Fetch SL/TP from client; returns (None, None) silently on any error."""
        try:
            return await src.client.fetch_sl_tp(market_id)
        except Exception:
            return None, None

    # ── Lighter realized-PnL helper ──────────────────────────────────────────
    def _lighter_realized(side: str, avg_entry: Decimal, fill_price: Decimal, reduced_size: Decimal) -> Decimal:
        """Compute realized PnL for a Lighter fill (no per-fill closedPnl from exchange)."""
        if side == "long":
            return (fill_price - avg_entry) * reduced_size
        return (avg_entry - fill_price) * reduced_size

    def _roundtrip_partial_pnl(source_name: str, market_symbol: str) -> Decimal:
        """Sum the realized PnL of the CURRENT (not-yet-closed) round-trip's
        PARTIAL rows for this (source, coin).

        closed_trades is newest-first; walk from the front collecting PARTIAL
        rows until the previous FULL (a round-trip boundary). Used to make the
        FULL close card show the whole trade's total. Restart-safe: the partials
        were persisted, so they're back in closed_trades after a reboot.
        """
        total = Decimal(0)
        for row in closed_trades:
            if row.get("source") != source_name or row.get("market_symbol") != market_symbol:
                continue
            if (row.get("realization_kind") or "").upper() == "FULL":
                break  # boundary of the previous round-trip
            p = row.get("pnl")
            if p is not None:
                try:
                    total += Decimal(str(p))
                except Exception:
                    pass
        return total

    # ── Shared realization recorder ───────────────────────────────────────────
    async def record_realization(
        *,
        src: "Source",
        kind: str,  # "PARTIAL" | "FULL"
        trade: "Trade",
        position_before: "Position",
        realized_pnl: "Decimal | None",
        reduced_size: Decimal,
        fill_price: Decimal,
        avg_entry: Decimal,
        leverage: "float | None",
        fill_ids: "list[int]",
        anchor_entry: Decimal,
        card_pnl_override: "Decimal | None" = None,
    ) -> None:
        """Record a single realization event (partial close or full close).

        Writes a closed_trades DB row, a PnL card PNG, and broadcasts a snapshot.
        Deduplicated by fill_ids — if every id in fill_ids is already in
        _recorded_realizations, the call is a no-op.

        ``card_pnl_override`` (FULL close only) sets the $ figure shown ON THE
        CARD IMAGE to the whole round-trip's total (every scale-out + this
        close), while the stored row keeps this fill's own realized_pnl. The
        display layer (aggregate_round_trips) sums the per-fill rows to the same
        total, so card, grid tile, chart bar and stats all agree — with no
        double counting.
        """
        # ── Dedup guard ────────────────────────────────────────────────────────
        if fill_ids:
            if all(fid in _recorded_realizations for fid in fill_ids):
                log.info(
                    "[%s] record_realization: all fill_ids already recorded, skipping %s %s",
                    src.name, kind, trade.market_symbol,
                )
                return
            # Register all ids (and the trade's own id) so future calls are no-ops
            for fid in fill_ids:
                _recorded_realizations.add(fid)
        if trade.trade_id is not None:
            _recorded_realizations.add(trade.trade_id)

        # ── Build a synthetic Event-like for generate_pnl_card ────────────────
        # We need an Event whose position_before is the pre-reduce/pre-close
        # position and whose trade carries the fill price and size.
        from .types import Event as _Event, EventKind as _EventKind
        from dataclasses import replace as _replace
        # Use a minimal Trade-like object so the card can read price / size / side.
        # We reuse the real trade object; position_before is what we pass in.
        synth_trade = _replace(
            trade,
            price=fill_price,
            size=reduced_size,
        )
        synth_ev = _Event(
            kind=_EventKind.CLOSE,
            trade=synth_trade,
            position_before=position_before,
            position_after=None,
        )
        synth_ev_with_leverage = synth_ev
        # Event is a frozen dataclass so we can't set leverage directly;
        # generate_pnl_card reads event.leverage for the pill.
        # We work around by passing a modified object attribute via object.__setattr__.
        import copy as _copy
        synth_ev_with_leverage = _copy.copy(synth_ev)
        object.__setattr__(synth_ev_with_leverage, "leverage", leverage)

        # ── PnL / pct ─────────────────────────────────────────────────────────
        is_win = realized_pnl is not None and realized_pnl > 0
        wins, total = record_result(is_win)

        pct: "Decimal | None" = None
        if avg_entry and avg_entry != 0:
            if position_before.side == "long":
                pct = (fill_price - avg_entry) / avg_entry * 100
            else:
                pct = (avg_entry - fill_price) / avg_entry * 100

        # ── PnL card ──────────────────────────────────────────────────────────
        card_bytes = generate_pnl_card(
            synth_ev_with_leverage,
            src.name,
            wins,
            total,
            pnl_override=(card_pnl_override if card_pnl_override is not None else realized_pnl),
            is_partial=(kind == "PARTIAL"),
            privacy=privacy,
            is_hl=src.is_hyperliquid,
            anchor_entry=anchor_entry,
            source_id=src.id,
        )

        # Write PNG to disk
        card_path = None
        if card_bytes:
            ts_str = trade.timestamp.isoformat().replace(":", "-").replace("+", "p")
            kind_suffix = "_partial" if kind == "PARTIAL" else ""
            raw_name = f"{ts_str}_{src.name}_{trade.market_symbol}{kind_suffix}"
            safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", raw_name) + ".png"
            card_file = cards_dir / safe_name
            await asyncio.to_thread(card_file.write_bytes, card_bytes)
            card_path = f"/cards/{safe_name}"

        # ── Compute HL display fields (in-memory only, not persisted) ─────────
        entry_disp = exit_disp = size_disp = notional_disp = ts_disp = None
        close_footnote = ""
        if src.is_hyperliquid and position_before is not None:
            dv = disp_view(
                privacy, True,
                src.id, position_before.market_symbol, position_before.side, anchor_entry,
                entry=avg_entry,
                exit=fill_price,
                size=reduced_size,
                notional=reduced_size * fill_price,
                ts=trade.timestamp.isoformat(),
                now=datetime.now(timezone.utc),
            )
            entry_disp    = str(dv.get("entry",    avg_entry))
            exit_disp     = str(dv.get("exit",     fill_price))
            size_disp     = str(dv.get("size",     reduced_size))
            notional_disp = str(dv.get("notional", reduced_size * fill_price))
            ts_disp       = dv.get("ts", trade.timestamp.isoformat())
            close_footnote = dv.get("footnote", "")

        # ── Build DB record (REAL values only) ────────────────────────────────
        record: dict = {
            "ts": trade.timestamp.isoformat(),
            "source": src.name,
            "market_symbol": trade.market_symbol,
            "side": position_before.side if position_before else None,
            "entry": str(avg_entry),
            "exit": str(fill_price),
            "size": str(reduced_size),
            "notional": str(reduced_size * fill_price),
            "pnl": str(realized_pnl) if realized_pnl is not None else None,
            "pct": str(pct) if pct is not None else None,
            "is_win": 1 if is_win else 0,
            "leverage": str(leverage) if leverage is not None else None,
            "wins": wins,
            "total": total,
            "card_path": card_path,
            # Realization metadata
            "trade_id": trade.trade_id,
            "fill_ids": json.dumps(fill_ids),
            "realization_kind": kind,
        }
        await save_closed_trade(DB_PATH, record)

        # ── Add in-memory display fields (HL only, never persisted) ───────────
        if src.is_hyperliquid:
            record["entry_disp"]    = entry_disp
            record["exit_disp"]     = exit_disp
            record["size_disp"]     = size_disp
            record["notional_disp"] = notional_disp
            record["ts_disp"]       = ts_disp
            record["is_hl"]         = True
            record["footnote"]      = close_footnote
        record["realization_kind"] = kind  # always present in-memory for JS tile label

        # Newest-first; only trim when NOT keeping full history for stats
        closed_trades.insert(0, record)
        if not cfg.stats_full_history:
            del closed_trades[cfg.max_closed_trades:]
        refresh_stats()
        await hub.broadcast(snapshot_payload("snapshot"))

    async def flush_aggregate(key: tuple[str, int]) -> None:
        buf = _pending.pop(key, None)
        if buf is None:
            return
        source_id, market_id = key
        src = by_id.get(source_id)
        if src is None:
            return
        # Use the position snapshot captured at fill time; fall back to live tracker
        # state only if somehow missing. This prevents reconciler interference from
        # altering the position shown in the alert during the 30s accumulation window.
        pos = buf.get("position") or src.tracker.snapshot().get(market_id)
        if pos is None:
            log.info("[%s] aggregate flush: market %d already closed, skipping",
                     src.name, market_id)
            return
        if pos.notional_usd < src.min_notional:
            log.info("[%s] aggregate flush: %s notional $%.0f below min, skipping",
                     src.name, pos.market_symbol, pos.notional_usd)
            return
        sl, tp = await _get_sl_tp(src, market_id)
        ae = _anchor(src, market_id, pos.avg_entry_price)
        text = format_aggregate(
            position=pos,
            net_added_usd=buf["net_added"],
            n_fills=buf["n_fills"],
            leverage=buf["leverage"],
            pool_url=src.url,
            source_name=src.name,
            sl=sl,
            tp=tp,
            privacy=privacy,
            is_hl=src.is_hyperliquid,
            anchor_entry=ae,
            source_id=src.id,
        )
        log.info("[%s] aggregate alert: %s +$%.0f across %d fills → $%.0f",
                 src.name, pos.market_symbol, buf["net_added"], buf["n_fills"],
                 pos.notional_usd)
        await tg_send(text)

    async def flush_reduce_aggregate(key: tuple[str, int]) -> None:
        buf = _pending_reduces.pop(key, None)
        if buf is None:
            return
        source_id, market_id = key
        src = by_id.get(source_id)
        if src is None:
            return
        pos = buf.get("position") or src.tracker.snapshot().get(market_id)
        if pos is None:
            log.info("[%s] reduce aggregate flush: market %d already closed, skipping",
                     src.name, market_id)
            return
        sl, tp = await _get_sl_tp(src, market_id)
        ae = _anchor(src, market_id, pos.avg_entry_price)
        text = format_reduce_aggregate(
            position=pos,
            net_reduced_usd=buf["net_reduced"],
            n_fills=buf["n_fills"],
            realized_pnl=buf["total_pnl"],
            leverage=buf["leverage"],
            pool_url=src.url,
            source_name=src.name,
            sl=sl,
            tp=tp,
            privacy=privacy,
            is_hl=src.is_hyperliquid,
            anchor_entry=ae,
            source_id=src.id,
        )
        log.info("[%s] reduce aggregate alert: %s −$%.0f across %d fills → remaining $%.0f",
                 src.name, pos.market_symbol, buf["net_reduced"], buf["n_fills"],
                 pos.notional_usd)
        await tg_send(text)

        # ── Record this partial close as its own realization row ───────────────
        last_trade = buf.get("last_trade")
        pos_before = buf.get("position_before") or pos  # best-effort pre-reduce snapshot
        if last_trade is not None and pos_before is not None:
            reduced_size = buf.get("reduced_size", buf["net_reduced"] / last_trade.price if last_trade.price else Decimal(0))
            await record_realization(
                src=src,
                kind="PARTIAL",
                trade=last_trade,
                position_before=pos_before,
                realized_pnl=buf["total_pnl"],
                reduced_size=reduced_size,
                fill_price=last_trade.price,
                avg_entry=pos_before.avg_entry_price,
                leverage=buf["leverage"],
                fill_ids=buf.get("fill_ids", []),
                anchor_entry=ae,
            )

    def _accumulate_reduce(source_id: str, ev: Event) -> None:
        key = (source_id, ev.trade.market_id)
        fill_notional = ev.trade.size * ev.trade.price
        pnl = ev.trade.realized_pnl
        # For Lighter (no per-fill closedPnl), compute PnL from prices.
        # Use ev.position_before which holds the pre-fill position state.
        pos_for_pnl = ev.position_before
        if pnl is None and pos_for_pnl is not None:
            pnl = _lighter_realized(
                pos_for_pnl.side,
                pos_for_pnl.avg_entry_price,
                ev.trade.price,
                ev.trade.size,
            )
        current_pos = by_id[source_id].tracker.snapshot().get(ev.trade.market_id)
        tid = ev.trade.trade_id
        if key in _pending_reduces:
            _pending_reduces[key]["net_reduced"] += fill_notional
            _pending_reduces[key]["n_fills"] += 1
            _pending_reduces[key]["reduced_size"] += ev.trade.size
            if pnl is not None:
                prev = _pending_reduces[key]["total_pnl"]
                _pending_reduces[key]["total_pnl"] = (prev or Decimal(0)) + pnl
            if ev.leverage is not None:
                _pending_reduces[key]["leverage"] = ev.leverage
            if current_pos is not None:
                _pending_reduces[key]["position"] = current_pos
            # Track representative last fill and fill_ids for record_realization
            _pending_reduces[key]["last_trade"] = ev.trade
            if tid is not None:
                _pending_reduces[key]["fill_ids"].append(tid)
            # Debounce: reset timer on every new fill (same reason as SIZE_CHANGE)
            _pending_reduces[key]["task"].cancel()
            task = asyncio.get_running_loop().create_task(
                _delayed_flush_reduce(key, AGGREGATE_WINDOW)
            )
            _pending_reduces[key]["task"] = task
        else:
            task = asyncio.get_running_loop().create_task(
                _delayed_flush_reduce(key, AGGREGATE_WINDOW)
            )
            _pending_reduces[key] = {
                "net_reduced": fill_notional,
                "n_fills": 1,
                "total_pnl": pnl,
                "leverage": ev.leverage,
                "position": current_pos,
                # New fields for record_realization
                "reduced_size": ev.trade.size,
                "last_trade": ev.trade,
                "fill_ids": [tid] if tid is not None else [],
                # Keep pre-reduce position snapshot for card context
                "position_before": ev.position_before,
                "task": task,
            }

    async def _delayed_flush_reduce(key: tuple[str, int], delay: float) -> None:
        await asyncio.sleep(delay)
        await flush_reduce_aggregate(key)

    def _accumulate_size_change(source_id: str, ev: Event) -> None:
        key = (source_id, ev.trade.market_id)
        fill_notional = ev.trade.size * ev.trade.price
        # Always refresh the position snapshot — tracker just applied this fill,
        # so we capture the most up-to-date post-fill state before the reconciler
        # can overwrite it during the accumulation window.
        current_pos = by_id[source_id].tracker.snapshot().get(ev.trade.market_id)
        if key in _pending:
            _pending[key]["net_added"] += fill_notional
            _pending[key]["n_fills"] += 1
            if ev.leverage is not None:
                _pending[key]["leverage"] = ev.leverage
            if current_pos is not None:
                _pending[key]["position"] = current_pos
            # Debounce: reset the timer on every new fill so that fills
            # arriving across REST poll boundaries (or a WS burst after a
            # reconnect) are all batched into one alert rather than spilling
            # into a second window and firing a duplicate alert.
            _pending[key]["task"].cancel()
            task = asyncio.get_running_loop().create_task(
                _delayed_flush(key, AGGREGATE_WINDOW)
            )
            _pending[key]["task"] = task
        else:
            task = asyncio.get_running_loop().create_task(
                _delayed_flush(key, AGGREGATE_WINDOW)
            )
            _pending[key] = {
                "net_added": fill_notional,
                "n_fills": 1,
                "leverage": ev.leverage,
                "position": current_pos,
                "task": task,
            }

    def _cancel_pending(key: tuple[str, int]) -> None:
        buf = _pending.pop(key, None)
        if buf is not None:
            buf["task"].cancel()

    async def _delayed_flush(key: tuple[str, int], delay: float) -> None:
        await asyncio.sleep(delay)
        await flush_aggregate(key)

    queue: asyncio.Queue[tuple[str, Trade]] = asyncio.Queue()

    def all_positions() -> list[dict]:
        """Return all open positions as dicts, augmented with cached SL/TP prices.

        Uses _dash_positions (API truth from reconciler) rather than the fill-based
        tracker so the dashboard always reflects blockchain reality, independent of
        whether fills have been processed yet.

        For HL sources, display fields (avg_entry_price, size, notional, sl, tp) are
        overwritten with privacy-transformed values; uPnL stays EXACT.
        """
        out: list[dict] = []
        for s in sources:
            for market_id, pos in _dash_positions.get(s.id, {}).items():
                d = asdict(pos)
                sl, tp = _sl_tp_cache.get((s.id, market_id), (None, None))
                d["sl_price"] = sl
                d["tp_price"] = tp
                d["is_hl"] = s.is_hyperliquid
                if s.is_hyperliquid:
                    ae = _anchor(s, market_id, pos.avg_entry_price)
                    dv = disp_view(
                        privacy, True,
                        s.id, pos.market_symbol, pos.side, ae,
                        entry=pos.avg_entry_price,
                        size=pos.size,
                        notional=pos.notional_usd,
                        sl=sl,
                        tp=tp,
                    )
                    d["avg_entry_price"] = dv.get("entry", pos.avg_entry_price)
                    d["size"]            = dv.get("size",  pos.size)
                    d["notional_usd"]    = dv.get("notional", pos.notional_usd)
                    if sl is not None:
                        d["sl_price"] = dv.get("sl", sl)
                    if tp is not None:
                        d["tp_price"] = dv.get("tp", tp)
                    d["footnote"] = dv.get("footnote", "")
                out.append(d)
        return out

    def _transform_open_order_row(s: "Source", o: dict) -> dict:
        """Apply the same privacy factor to an open order row's price/trigger_px
        when the owning source is HL.  Lighter rows pass through unchanged.

        Always computes `notional` (USD value) from the RAW price and size BEFORE
        price is overwritten, so the column shows the correct order notional.
        """
        # --- Compute notional from raw values before any transform ---
        try:
            raw_price_for_notional = o.get("price") or o.get("trigger_px")
            raw_size = o.get("size")
            if raw_price_for_notional is not None and raw_size is not None:
                real_notional = Decimal(str(raw_price_for_notional)) * Decimal(str(raw_size))
                o["notional"] = str(disp_notional(privacy, s.is_hyperliquid, real_notional))
            else:
                o["notional"] = None
        except Exception:
            o["notional"] = None

        if not s.is_hyperliquid:
            o["is_hl"] = False
            return o
        o["is_hl"] = True
        try:
            symbol = o.get("market_symbol", "")
            side   = o.get("side", "long")
            mid    = o.get("market_id", 0)
            # Prefer the position anchor; fall back to the order's own price.
            price_raw  = o.get("price")
            trig_raw   = o.get("trigger_px")
            anchor_raw = price_raw or trig_raw
            if anchor_raw is None:
                o["footnote"] = footnote(privacy, True)
                return o
            anchor = Decimal(str(anchor_raw))
            ae = _anchor(s, mid, anchor)
            f = price_factor(privacy, s.id, symbol, side, ae)
            if price_raw is not None:
                o["price"] = str(disp_price(privacy, True, f, Decimal(str(price_raw))))
            if trig_raw is not None:
                o["trigger_px"] = str(disp_price(privacy, True, f, Decimal(str(trig_raw))))
        except Exception:
            # Fail CLOSED: never leave a real HL price in the payload if the
            # transform errored (the dashboard port is internet-reachable).
            log.debug("[%s] failed to transform open order for privacy", s.name)
            if privacy.enabled:
                o["price"] = None
                o["trigger_px"] = None
        o["footnote"] = footnote(privacy, True)
        return o

    def all_open_orders() -> list[dict]:
        """Return all cached open orders, with HL price transforms applied."""
        out: list[dict] = []
        for s in sources:
            for o in _open_orders.get(s.id, []):
                row = dict(o)  # shallow copy so we don't mutate the cache
                out.append(_transform_open_order_row(s, row))
        return out

    def snapshot_payload(type_: str, extra: dict | None = None) -> dict:
        payload = {
            "type": type_,
            "sources": [s.name for s in sources],
            "positions": all_positions(),
            "open_orders": all_open_orders(),
            "recent_events": recent_events[:cfg.max_recent_events],
            "tg_alerts": _tg_alerts[:TG_ALERTS_MAX],
            # UI payload is always capped; the full list is kept in-memory for stats.
            # Aggregated into one entry per round-trip (in-progress positions
            # included, flagged OPEN) + filtered to the display window, so the grid
            # matches the analytics (one card per trade, only existing coins/dates).
            "closed_trades": display_trades(include_open=True)[:cfg.max_closed_trades],
            "stats": stats_state,
            "health": health.snapshot(),
        }
        if extra:
            payload.update(extra)
        return payload

    async def position_reconciler(src: Source) -> None:
        """Every N seconds: pull ground-truth positions from the exchange API.

        Uses two separate comparisons:
          1. _dash_positions[src.id] (previous API snapshot) vs actual (current API)
             → detect truly new / silently-closed positions and send TG alerts.
          2. src.tracker (fill-based classifier) is seeded with API truth so its
             internal state doesn't drift — EXCEPT for positions that the reconciler
             just alerted about (fill not yet arrived); those are left to the fill-
             based path so the OPEN classification fires correctly.

        This separation prevents the phantom SIZE_CHANGE bug: reconciler seeds
        position → fill arrives → tracker classifies as SIZE_CHANGE (not OPEN)
        → alert is cancelled on close → user never sees an open notification.
        """
        while True:
            await asyncio.sleep(cfg.reconciler_interval_seconds)
            try:
                actual = await src.client.current_positions()
                actual = {mid: p for mid, p in actual.items() if not src.is_excluded(p.market_symbol)}
                prev_dash = _dash_positions.get(src.id, {})
                tracked    = src.tracker.snapshot()

                # ── 1. Detect positions that appeared since last reconciler run ──────
                for market_id, pos in actual.items():
                    if market_id not in prev_dash:
                        # New position: appeared in API since the last reconcile tick.
                        if market_id not in tracked:
                            # Fill hasn't arrived yet — the tracker doesn't know about
                            # this position. Alert from reconciler so the user isn't
                            # left waiting for a fill that may arrive later.
                            log.info(
                                "[%s] reconcile: new %s %s (notional $%.0f) — "
                                "fill pending, alerting now",
                                src.name, pos.side, pos.market_symbol, pos.notional_usd,
                            )
                            if tg_token and tg_channel and pos.notional_usd >= src.min_notional:
                                sl, tp = await _get_sl_tp(src, market_id)
                                _sl_tp_cache[(src.id, market_id)] = (sl, tp)
                                lev = await src.client.fetch_leverage(market_id)
                                direction = "🟢 LONG" if pos.side == "long" else "🔴 SHORT"
                                lev_str  = f"  |  {lev:g}x" if lev is not None else ""

                                # Seed privacy anchor for this newly-observed position
                                if src.is_hyperliquid:
                                    _privacy_anchor.setdefault(src.id, {})
                                    if market_id not in _privacy_anchor[src.id]:
                                        _privacy_anchor[src.id][market_id] = pos.avg_entry_price

                                # Transform displayed values for HL
                                disp_entry_val = pos.avg_entry_price
                                disp_not_val   = pos.notional_usd
                                disp_sl        = sl
                                disp_tp        = tp
                                fn_text        = ""
                                if src.is_hyperliquid:
                                    ae = _anchor(src, market_id, pos.avg_entry_price)
                                    dv = disp_view(
                                        privacy, True,
                                        src.id, pos.market_symbol, pos.side, ae,
                                        entry=pos.avg_entry_price,
                                        notional=pos.notional_usd,
                                        sl=sl,
                                        tp=tp,
                                    )
                                    disp_entry_val = dv.get("entry", pos.avg_entry_price)
                                    disp_not_val   = dv.get("notional", pos.notional_usd)
                                    disp_sl        = dv.get("sl", sl)
                                    disp_tp        = dv.get("tp", tp)
                                    fn_text        = dv.get("footnote", "")

                                sl_parts = []
                                if disp_sl is not None:
                                    sl_parts.append(f"SL: ${disp_sl:,.4f}")
                                if disp_tp is not None:
                                    sl_parts.append(f"TP: ${disp_tp:,.4f}")
                                sl_tp_str = ("\n" + "  |  ".join(sl_parts)) if sl_parts else ""
                                footer    = f"\n{src.url}" if src.url else ""
                                fn_line   = f"\n{fn_text}" if fn_text else ""
                                msg = (
                                    f"📍 {src.name}\n"
                                    f"Opened {direction} {pos.market_symbol}\n"
                                    f"Entry: ${disp_entry_val:,.4f}  |  "
                                    f"Notional: ${disp_not_val:,.0f}"
                                    f"{lev_str}{sl_tp_str}{fn_line}{footer}"
                                )
                                await tg_send(msg)
                                # If OPEN alert already showed SL/TP, pre-arm so the
                                # reconciler's SL/TP section doesn't double-alert.
                                if sl is not None or tp is not None:
                                    _sl_tp_alerted.add((src.id, market_id))
                            # Mark so the fill-based OPEN handler doesn't double-alert.
                            _reconciler_alerted_opens.add((src.id, market_id))
                        else:
                            # Tracker already has this position from a fill that arrived
                            # before the reconciler ran — fill-based path handled it.
                            log.debug(
                                "[%s] reconcile: new position %s already in tracker",
                                src.name, pos.market_symbol,
                            )

                # ── 2. Detect silently-closed positions ──────────────────────────────
                # Compare against the PREVIOUS API snapshot (prev_dash), not the
                # fill-based tracker, so we catch closes the tracker also missed.
                for market_id, pos in prev_dash.items():
                    if market_id not in actual:
                        log.warning(
                            "[%s] reconcile: %s %s closed while unobserved",
                            src.name, pos.side, pos.market_symbol,
                        )
                        _reconciler_alerted_opens.discard((src.id, market_id))
                        _sl_tp_alerted.discard((src.id, market_id))

                        # ── Read anchor BEFORE clearing it (needed for both TG msg
                        #    and record_realization below).
                        ae = _anchor(src, market_id, pos.avg_entry_price)

                        # ── HL-only: fetch realizing fills and record PnL ──────────
                        if src.is_hyperliquid:
                            try:
                                # Use last-2000 user_fills (start_time_ms=None) and
                                # filter to this market_id — simple and avoids
                                # needing a reliable last-known timestamp.
                                _sc_fills = await src.client.fetch_realizing_fills(
                                    market_id=market_id,
                                    start_time_ms=None,
                                )
                                # Only the fills we haven't booked yet (oldest-first).
                                _sc_new = [
                                    f for f in _sc_fills
                                    if f.trade_id not in _recorded_realizations
                                ]
                                # Partials already booked for this round-trip BEFORE
                                # this loop inserts any rows (capture once to avoid
                                # double-counting the rows we add below).
                                _sc_prior = _roundtrip_partial_pnl(src.name, pos.market_symbol)
                                # This unobserved close may span several fills. Mark
                                # all but the last as PARTIAL and the last as FULL so
                                # round-trip aggregation collapses them into ONE trade
                                # (instead of N). The FULL card shows the total.
                                _sc_total = Decimal(0)
                                for _i, _sc_fill in enumerate(_sc_new):
                                    _sc_pnl = (
                                        _sc_fill.realized_pnl
                                        if _sc_fill.realized_pnl is not None
                                        else _lighter_realized(
                                            pos.side,
                                            pos.avg_entry_price,
                                            _sc_fill.price,
                                            _sc_fill.size,
                                        )
                                    )
                                    if _sc_pnl is not None:
                                        _sc_total += _sc_pnl
                                    _is_last = _i == len(_sc_new) - 1
                                    # FULL card total = pre-existing partials + every
                                    # fill in this backstop batch.
                                    _card_total = None
                                    if _is_last and (_sc_prior != 0 or len(_sc_new) > 1):
                                        _card_total = _sc_prior + _sc_total
                                    await record_realization(
                                        src=src,
                                        kind="FULL" if _is_last else "PARTIAL",
                                        trade=_sc_fill,
                                        position_before=pos,
                                        realized_pnl=_sc_pnl,
                                        reduced_size=_sc_fill.size,
                                        fill_price=_sc_fill.price,
                                        avg_entry=pos.avg_entry_price,
                                        leverage=None,
                                        fill_ids=[_sc_fill.trade_id],
                                        anchor_entry=ae,
                                        card_pnl_override=_card_total,
                                    )
                            except Exception:
                                log.exception(
                                    "[%s] reconcile silent-close backstop failed for %s",
                                    src.name, pos.market_symbol,
                                )

                        if tg_token and tg_channel:
                            direction = "🟢 LONG" if pos.side == "long" else "🔴 SHORT"
                            footer    = f"\n{src.url}" if src.url else ""

                            # Transform displayed values for HL (close price unavailable)
                            disp_entry_val = pos.avg_entry_price
                            disp_not_val   = pos.notional_usd
                            fn_text        = ""
                            if src.is_hyperliquid:
                                dv = disp_view(
                                    privacy, True,
                                    src.id, pos.market_symbol, pos.side, ae,
                                    entry=pos.avg_entry_price,
                                    notional=pos.notional_usd,
                                )
                                disp_entry_val = dv.get("entry",    pos.avg_entry_price)
                                disp_not_val   = dv.get("notional", pos.notional_usd)
                                fn_text        = dv.get("footnote", "")
                                # Clear anchor now that position is gone
                                _privacy_anchor.get(src.id, {}).pop(market_id, None)

                            fn_line = f"\n{fn_text}" if fn_text else ""
                            msg = (
                                f"📍 {src.name}\n"
                                f"Closed {direction} {pos.market_symbol}\n"
                                f"Entry: ${disp_entry_val:,.2f}  |  Notional: ${disp_not_val:,.0f}  (close price unavailable)"
                                f"{fn_line}{footer}"
                            )
                            await tg_send(msg)

                # ── 3. Sync fill-based tracker with API truth ────────────────────────
                # Exclude positions awaiting their OPEN fill so the tracker classifies
                # that fill as OPEN (not SIZE_CHANGE) when it eventually arrives.
                positions_for_tracker = {
                    mid: p for mid, p in actual.items()
                    if (src.id, mid) not in _reconciler_alerted_opens
                }
                src.tracker.seed(positions_for_tracker)

                # ── 4. Update SL/TP cache + fire one-time TG alert when first known ──
                for market_id in actual:
                    sl, tp = await _get_sl_tp(src, market_id)
                    _sl_tp_cache[(src.id, market_id)] = (sl, tp)
                    key = (src.id, market_id)
                    if (sl is not None or tp is not None) and key not in _sl_tp_alerted:
                        pos = actual[market_id]
                        ae = _anchor(src, market_id, pos.avg_entry_price)
                        alert_text = format_sl_tp_set(
                            source_name=src.name,
                            side=pos.side,
                            market_symbol=pos.market_symbol,
                            sl=sl,
                            tp=tp,
                            pool_url=src.url,
                            privacy=privacy,
                            is_hl=src.is_hyperliquid,
                            anchor_entry=ae,
                            source_id=src.id,
                        )
                        if alert_text and tg_token and tg_channel:
                            await tg_send(alert_text)
                        _sl_tp_alerted.add(key)
                for cache_key in list(_sl_tp_cache.keys()):
                    c_src_id, c_market_id = cache_key
                    if c_src_id == src.id and c_market_id not in actual:
                        del _sl_tp_cache[cache_key]
                        _sl_tp_alerted.discard(cache_key)

                # ── 5. Fetch resting/pending open orders (if enabled) ────────────────
                if cfg.open_orders_enabled:
                    try:
                        raw_orders = await src.client.fetch_open_orders()
                        # Filter excluded symbols
                        raw_orders = [
                            o for o in raw_orders
                            if not src.is_excluded(o.market_symbol)
                        ]
                        _open_orders[src.id] = [
                            _to_jsonable(asdict(o)) for o in raw_orders
                        ]
                    except Exception:
                        log.debug("[%s] fetch_open_orders failed — keeping stale cache", src.name)

                # ── 6. Advance dashboard snapshot ────────────────────────────────────
                _dash_positions[src.id] = actual
                health.mark_up(f"source:{src.name}", ok_ts=datetime.now(timezone.utc).isoformat())
                await hub.broadcast(snapshot_payload("snapshot"))

            except Exception as _rec_exc:
                log.exception("[%s] position reconciler failed", src.name)
                health.mark_degraded(f"source:{src.name}", error=str(_rec_exc))

    async def ws_producer(src: Source) -> None:
        async for trade in src.client.stream_trades():
            await queue.put((src.id, trade))

    async def rest_safety_producer(src: Source) -> None:
        while True:
            await asyncio.sleep(cfg.rest_poll_seconds)
            try:
                trades = await src.client.fetch_trades_since(src.last_trade_id)
                for t in trades:
                    await queue.put((src.id, t))
                health.mark_up(f"source:{src.name}", ok_ts=datetime.now(timezone.utc).isoformat())
            except Exception as _poll_exc:
                log.exception("[%s] safety poll failed", src.name)
                health.mark_degraded(f"source:{src.name}", error=str(_poll_exc))

    async def consumer() -> None:
        while True:
            source_id, trade = await queue.get()
            src = by_id.get(source_id)
            if src is None:
                continue
            # Set-based dedup catches WS replay and REST/WS overlap regardless of order.
            if trade.trade_id in src.seen_tids:
                continue
            src.seen_tids.add(trade.trade_id)
            src.last_trade_id = max(src.last_trade_id or 0, trade.trade_id)
            events = src.tracker.apply(trade)
            for ev in events:
                if src.is_excluded(ev.trade.market_symbol):
                    continue
                ev.leverage = await src.client.fetch_leverage(ev.trade.market_id)
                recent_events.insert(0, ev)
                del recent_events[cfg.max_recent_events:]
                # Build a display block for HL events so the JS renderer uses
                # privacy-fuzzed values without doing any math itself.
                _ev_extra: dict = {"event": ev}
                if src.is_hyperliquid:
                    _ev_ae = _anchor(src, ev.trade.market_id, ev.trade.price)
                    _ev_dv = disp_view(
                        privacy, True,
                        src.id, ev.trade.market_symbol, ev.trade.side, _ev_ae,
                        entry=ev.trade.price,
                        size=ev.trade.size,
                        notional=ev.trade.size * ev.trade.price,
                        ts=ev.trade.timestamp.isoformat(),
                        now=datetime.now(timezone.utc),
                    )
                    # Attach as _disp on the event object in recent_events so
                    # snapshot broadcasts also carry it for newly-added events.
                    # Since Event is a frozen dataclass we attach to the broadcast
                    # dict directly rather than mutating the object.
                    _ev_extra["_disp"] = {
                        "price": str(_ev_dv.get("entry", ev.trade.price)),
                        "size":  str(_ev_dv.get("size",  ev.trade.size)),
                        "notional": str(_ev_dv.get("notional", ev.trade.size * ev.trade.price)),
                        "ts": _ev_dv.get("ts", ev.trade.timestamp.isoformat()),
                        "footnote": _ev_dv.get("footnote", ""),
                    }
                await hub.broadcast(snapshot_payload("event", _ev_extra))
                await save_event(
                    DB_PATH,
                    ev.trade.timestamp.isoformat(),
                    json.dumps(_to_jsonable(ev)),
                )
                log.info("[%s] event %s %s %s @ %s size=%s", src.name, ev.kind,
                         ev.trade.side, ev.trade.market_symbol, ev.trade.price,
                         ev.trade.size)

                if not (tg_token and tg_channel):
                    continue

                key = (source_id, ev.trade.market_id)

                if ev.kind == EventKind.OPEN:
                    # Cancel any pending aggregate for this market (position flipped)
                    _cancel_pending(key)
                    # Always fetch and cache SL/TP on open so the dashboard shows it
                    # immediately without waiting for the next reconciliation cycle.
                    sl, tp = await _get_sl_tp(src, ev.trade.market_id)
                    _sl_tp_cache[key] = (sl, tp)
                    # Freeze the privacy anchor at open time (only if not already set)
                    if src.is_hyperliquid:
                        _privacy_anchor.setdefault(src.id, {})
                        if ev.trade.market_id not in _privacy_anchor[src.id]:
                            _privacy_anchor[src.id][ev.trade.market_id] = ev.trade.price
                    if key in _reconciler_alerted_opens:
                        # Reconciler already sent the OPEN alert (fill arrived after
                        # the reconciler seeded the position). Suppress this one to
                        # prevent a duplicate notification.
                        _reconciler_alerted_opens.discard(key)
                        log.info(
                            "[%s] fill-based OPEN for %s suppressed — reconciler already alerted",
                            src.name, ev.trade.market_symbol,
                        )
                    elif cfg.alert_on_open and passes_min_notional(ev, src.min_notional):
                        # NOTE: we deliberately do NOT consult _dash_positions here.
                        # On Lighter pool, /trades is typically fresher than /account
                        # (ZK rollup settlement lag on the account endpoint), so the
                        # fill is the most reliable signal that a position opened.
                        # Suppressing this alert based on a possibly-stale snapshot
                        # caused legitimate OPENs to be silently dropped.
                        ae = _anchor(src, ev.trade.market_id, ev.trade.price)
                        await tg_send(format_event(
                            ev, src.url, src.name, sl=sl, tp=tp,
                            privacy=privacy, is_hl=src.is_hyperliquid, anchor_entry=ae,
                            source_id=src.id,
                        ))
                        # Pre-arm so the reconciler doesn't double-alert SL/TP when
                        # the OPEN alert already showed them.
                        if sl is not None or tp is not None:
                            _sl_tp_alerted.add(key)

                elif ev.kind == EventKind.CLOSE:
                    # Cancel any pending SIZE_CHANGE aggregate — position gone
                    _cancel_pending(key)

                    # If a reduce batch is still pending for this key, flush it NOW
                    # so its PnL is recorded as its own PARTIAL row before we record
                    # the CLOSE.  This replaces the old "accumulated_pnl merge" approach.
                    if key in _pending_reduces:
                        _pending_reduces[key]["task"].cancel()
                        await flush_reduce_aggregate(key)

                    # Position is gone — remove from SL/TP cache and re-arm for next open
                    _sl_tp_cache.pop(key, None)
                    _sl_tp_alerted.discard(key)

                    # Read the frozen anchor BEFORE clearing it — needed for the card
                    # and the close record's display fields.
                    pos_b = ev.position_before
                    _close_anchor = _anchor(
                        src, ev.trade.market_id,
                        pos_b.avg_entry_price if pos_b else ev.trade.price,
                    )
                    # Now clear the anchor (position is gone)
                    _privacy_anchor.get(src.id, {}).pop(ev.trade.market_id, None)

                    # Compute this close fill's OWN realized PnL (no merging with reduces)
                    realized = (
                        ev.trade.realized_pnl
                        if ev.trade.realized_pnl is not None
                        else (
                            _lighter_realized(
                                pos_b.side,
                                pos_b.avg_entry_price,
                                ev.trade.price,
                                pos_b.size,
                            )
                            if pos_b is not None
                            else None
                        )
                    )

                    # Round-trip total for the card image = realized PnL already
                    # booked on this trade's scale-outs + this close fill. The
                    # pending reduce batch was flushed just above, so those PARTIAL
                    # rows are already in closed_trades.
                    _rt_partials = _roundtrip_partial_pnl(src.name, ev.trade.market_symbol)
                    _card_total = (
                        (_rt_partials + realized)
                        if (realized is not None and _rt_partials != 0)
                        else None  # no scale-outs → card shows this fill's pnl as usual
                    )

                    # Record the FULL close realization (card + DB + broadcast)
                    await record_realization(
                        src=src,
                        kind="FULL",
                        trade=ev.trade,
                        position_before=pos_b if pos_b is not None else ev.position_before,
                        realized_pnl=realized,
                        reduced_size=pos_b.size if pos_b else ev.trade.size,
                        fill_price=ev.trade.price,
                        avg_entry=pos_b.avg_entry_price if pos_b else ev.trade.price,
                        leverage=ev.leverage,
                        fill_ids=[ev.trade.trade_id] if ev.trade.trade_id is not None else [],
                        anchor_entry=_close_anchor,
                        card_pnl_override=_card_total,
                    )

                    if cfg.alert_on_close:
                        # Find the record just inserted (newest-first, index 0)
                        _close_record = closed_trades[0] if closed_trades else {}
                        _close_card_path = _close_record.get("card_path")
                        if _close_card_path:
                            # Re-read the bytes from disk for Telegram send
                            # card_path is "/cards/<filename>" — strip the web prefix
                            _card_filename = _close_card_path.removeprefix("/cards/")
                            _close_card_file = cards_dir / _card_filename
                            try:
                                card_bytes = await asyncio.to_thread(_close_card_file.read_bytes)
                            except Exception:
                                card_bytes = None
                        else:
                            card_bytes = None

                        if card_bytes:
                            side_txt = (pos_b.side.upper() if pos_b else ev.trade.side.upper())
                            pnl_for_log = realized
                            if pnl_for_log is not None:
                                sign = "+" if pnl_for_log >= 0 else "−"
                                pnl_txt = f"{sign}${abs(pnl_for_log):,.2f}"
                            else:
                                pnl_txt = "—"
                            log_text = (
                                f"🖼 PnL card · CLOSE {side_txt} "
                                f"{ev.trade.market_symbol} · {pnl_txt}  [{src.name}]"
                            )
                            await tg_send_photo(card_bytes, caption=src.url, log_text=log_text)
                        else:
                            ae = _close_anchor
                            await tg_send(format_event(
                                ev, src.url, src.name,
                                privacy=privacy, is_hl=src.is_hyperliquid, anchor_entry=ae,
                                source_id=src.id,
                            ))

                elif ev.kind == EventKind.REDUCE:
                    # Batch partial-close fills — avoids spam on incremental closes.
                    if cfg.alert_on_reduce and passes_min_notional(ev, src.min_notional):
                        _accumulate_reduce(source_id, ev)

                elif ev.kind == EventKind.SIZE_CHANGE:
                    # If the reconciler sent an OPEN alert for this position (fill
                    # arrived after the reconciler seeded it), this SIZE_CHANGE might
                    # be the phantom "doubling" fill (tracker classified the opening
                    # fill as SIZE_CHANGE because the position was already seeded).
                    # Clear the flag on the first SIZE_CHANGE so subsequent real adds
                    # alert normally.
                    if key in _reconciler_alerted_opens:
                        _reconciler_alerted_opens.discard(key)
                        log.info(
                            "[%s] SIZE_CHANGE for %s treated as opening fill — suppressed",
                            src.name, ev.trade.market_symbol,
                        )
                    elif cfg.alert_on_size_change:
                        # Batch same-side adds — avoids spam for rapid scaling in.
                        _accumulate_size_change(source_id, ev)

    # --- HTTP routes ---
    async def index(_request: web.Request) -> web.Response:
        # no-cache so a browser always revalidates and picks up new frontend JS
        # after a deploy (the HTML is tiny; revalidation is cheap).
        return web.Response(
            text=INDEX_HTML,
            content_type="text/html",
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

    async def ws_handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        hub.add(ws)
        await ws.send_str(json.dumps(_to_jsonable(snapshot_payload("snapshot"))))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            hub.remove(ws)
        return ws

    async def healthz(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def health_json(_request: web.Request) -> web.Response:
        """GET /health.json — machine-readable health snapshot."""
        return web.json_response(health.snapshot())

    async def health_page(_request: web.Request) -> web.Response:
        """GET /health — human-readable component status page (dark theme)."""
        snap = health.snapshot()
        started = snap["started_at"]
        now_ts = snap["now"]
        ok = snap["ok"]
        components = snap["components"]

        # Compute uptime string
        try:
            from datetime import timedelta
            dt_started = datetime.fromisoformat(started)
            dt_now = datetime.fromisoformat(now_ts)
            uptime_sec = int((dt_now - dt_started).total_seconds())
            hours, rem = divmod(uptime_sec, 3600)
            minutes, seconds = divmod(rem, 60)
            uptime_str = f"{hours}h {minutes}m {seconds}s"
        except Exception:
            uptime_str = "unknown"

        banner_color = "#166534" if ok else "#7f1d1d"
        banner_text_color = "#86efac" if ok else "#fca5a5"
        banner_msg = "All systems operational" if ok else f"Degraded &mdash; {sum(1 for c in components if c['status'] in ('down','degraded'))} issue(s)"

        status_dot = {
            "up": '<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#22c55e;margin-right:8px;vertical-align:-1px"></span>',
            "degraded": '<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#f59e0b;margin-right:8px;vertical-align:-1px"></span>',
            "down": '<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#ef4444;margin-right:8px;vertical-align:-1px"></span>',
            "disabled": '<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#6b7280;margin-right:8px;vertical-align:-1px"></span>',
        }

        rows_html = ""
        for c in components:
            dot = status_dot.get(c["status"], status_dot["disabled"])
            detail_html = f'<span style="color:#9ca3af;margin-left:8px">{c["detail"]}</span>' if c.get("detail") else ""
            error_html = f'<div style="color:#ef4444;font-size:11px;margin-top:3px">&#x26A0; {c["error"]}</div>' if c.get("error") else ""
            last_ok_html = f'<div style="color:#6b7280;font-size:11px;margin-top:2px">last ok: {c["last_ok"]}</div>' if c.get("last_ok") else ""
            rows_html += f"""
<tr>
  <td style="padding:10px 8px">{dot}<strong>{c["component"]}</strong>{detail_html}</td>
  <td style="padding:10px 8px;text-transform:uppercase;font-size:11px;letter-spacing:.5px;color:{'#22c55e' if c['status']=='up' else '#f59e0b' if c['status']=='degraded' else '#ef4444' if c['status']=='down' else '#6b7280'}">{c["status"]}</td>
  <td style="padding:10px 8px;font-size:11px">{error_html}{last_ok_html}</td>
</tr>"""

        html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="10">
<title>Health &mdash; Trade tracker</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 24px; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; background: #0b0d10; color: #d8dbe0; }}
  h1 {{ font-size: 18px; margin: 0 0 4px; color: #fff; }}
  a {{ color: #60a5fa; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .banner {{ border-radius:6px; padding:12px 16px; margin-bottom:20px; font-size:14px; font-weight:600; }}
  table {{ width:100%; border-collapse: collapse; font-size: 12px; background:#13161b; border:1px solid #1f242c; border-radius:8px; }}
  th {{ text-align:left; color:#6b7280; font-weight:500; padding: 8px; border-bottom: 1px solid #1f242c; font-size:11px; text-transform:uppercase; letter-spacing:.8px; }}
  tr:last-child td {{ border-bottom: none; }}
  td {{ border-bottom: 1px solid #11141a; }}
  .meta {{ font-size:11px; color:#6b7280; margin-bottom:16px; }}
</style>
</head>
<body>
<h1><a href="/">&#x2190; Trade tracker</a> &mdash; Health</h1>
<div class="meta">Uptime: {uptime_str} &nbsp;&middot;&nbsp; Started: {started} &nbsp;&middot;&nbsp; <a href="/health.json">/health.json</a></div>
<div class="banner" style="background:{banner_color};color:{banner_text_color}">{banner_msg}</div>
<table>
  <thead><tr><th>Component</th><th>Status</th><th>Detail / Last OK</th></tr></thead>
  <tbody>{rows_html}</tbody>
</table>
</body>
</html>"""
        return web.Response(text=html, content_type="text/html")

    # Global rate-limit for the public, unauthenticated /api/send_stats endpoint:
    # at most ONE send per hour total (the dashboard is public, so this protects
    # the Telegram channel from being flooded by anyone who finds the URL).
    # A 1-element list so the closure can mutate it without `nonlocal`.
    _stats_send_gate = [0.0]
    STATS_SEND_INTERVAL = 3600.0  # seconds (1 hour)

    async def send_stats(_request: web.Request) -> web.Response:
        """POST /api/send_stats — relay current trade stats to Telegram.

        Public + unauthenticated, so it is globally rate-limited to 1/hour.
        """
        if not (tg_token and tg_channel):
            return web.json_response({"ok": False, "error": "telegram not configured"})
        now = time.monotonic()
        elapsed = now - _stats_send_gate[0]
        if _stats_send_gate[0] and elapsed < STATS_SEND_INTERVAL:
            wait_min = int((STATS_SEND_INTERVAL - elapsed) // 60) + 1
            return web.json_response(
                {"ok": False, "error": f"rate limited — try again in ~{wait_min} min"}
            )
        # Reserve the slot BEFORE sending so a burst of concurrent calls can't
        # all slip through; a rare failed send simply costs this hour's slot.
        _stats_send_gate[0] = now
        try:
            text = format_stats_summary(stats_state, pool_url="")
            await tg_send(text)
            card = render_stats_card(stats_state)
            if card:
                await tg_send_photo(card, log_text="\U0001f4ca Trade stats")
            return web.json_response({"ok": True})
        except Exception as exc:
            log.exception("send_stats failed")
            return web.json_response({"ok": False, "error": str(exc)})

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/health", health_page)
    app.router.add_get("/health.json", health_json)
    app.router.add_post("/api/send_stats", send_stats)
    app.router.add_static("/cards/", path=str(cards_dir), show_index=False)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", cfg.dashboard_port)
    await site.start()
    log.info("dashboard on http://localhost:%d/  (%d source(s))", cfg.dashboard_port, len(sources))

    tasks = [consumer()]
    for s in sources:
        tasks.append(ws_producer(s))
        tasks.append(rest_safety_producer(s))
        tasks.append(position_reconciler(s))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
