"""Dashboard + Telegram notifier.

Watches the Lighter pool via WebSocket, updates the local dashboard,
and posts OPEN / CLOSE / SIZE_CHANGE events to Telegram.

Run with:  python -m src.dashboard
Then open: http://localhost:8080/
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx
from aiohttp import WSMsgType, web
from dotenv import load_dotenv

from .filters import passes_min_notional
from .formatter import format_event
from .lighter_client import LighterClient
from .position_tracker import PositionTracker
from .types import Event, Position, Trade

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("dashboard")

REST_BASE = "https://mainnet.zklighter.elliot.ai/api/v1"
WS_URL = "wss://mainnet.zklighter.elliot.ai/stream"
REST_POLL_SECONDS = 60
MAX_RECENT_EVENTS = 200


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
<title>Lighter pool dashboard</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 24px; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; background: #0b0d10; color: #d8dbe0; }
  h1 { font-size: 18px; margin: 0 0 4px; color: #fff; }
  .meta { font-size: 12px; color: #6b7280; margin-bottom: 24px; }
  .meta .dot { display:inline-block; width:8px; height:8px; border-radius:50%; background:#444; margin-right:6px; vertical-align: 1px; }
  .meta .dot.on { background:#22c55e; }
  .meta .dot.off { background:#ef4444; }
  .grid { display: grid; grid-template-columns: 1fr 2fr; gap: 24px; }
  section { background:#13161b; border:1px solid #1f242c; border-radius:8px; padding:16px; }
  section h2 { font-size: 12px; text-transform: uppercase; letter-spacing: 1px; color:#9ca3af; margin: 0 0 12px; }
  table { width:100%; border-collapse: collapse; font-size: 12px; }
  th { text-align:left; color:#6b7280; font-weight:500; padding: 6px 8px; border-bottom: 1px solid #1f242c; }
  td { padding: 8px; border-bottom: 1px solid #11141a; }
  tr:last-child td { border-bottom: none; }
  .long { color: #22c55e; }
  .short { color: #ef4444; }
  .kind-OPEN { color: #60a5fa; }
  .kind-CLOSE { color: #f59e0b; }
  .kind-SIZE_CHANGE { color: #a78bfa; }
  .empty { color:#4b5563; font-style: italic; padding: 8px; }
  .num { text-align: right; font-variant-numeric: tabular-nums; }
  @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<h1>Lighter pool <span id="pool"></span></h1>
<div class="meta"><span id="status"><span class="dot off"></span>connecting</span> &middot; <span id="last">no events yet</span></div>
<div class="grid">
  <section>
    <h2>Open positions</h2>
    <table>
      <thead><tr><th>Market</th><th>Side</th><th class="num">Size</th><th class="num">Entry</th><th class="num">Notional</th></tr></thead>
      <tbody id="positions"></tbody>
    </table>
  </section>
  <section>
    <h2>Recent events</h2>
    <table>
      <thead><tr><th>Time UTC</th><th>Kind</th><th>Market</th><th>Side</th><th class="num">Size</th><th class="num">Price</th><th class="num">Notional</th></tr></thead>
      <tbody id="events"></tbody>
    </table>
  </section>
</div>
<script>
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
function renderPositions(positions) {
  const tb = document.getElementById("positions");
  if (!positions.length) { tb.innerHTML = '<tr><td colspan="5" class="empty">no open positions</td></tr>'; return; }
  tb.innerHTML = positions.map(p => `
    <tr>
      <td>${p.market_symbol}</td>
      <td class="${p.side}">${p.side.toUpperCase()}</td>
      <td class="num">${fmtSize(p.size)}</td>
      <td class="num">${fmtPrice(p.avg_entry_price)}</td>
      <td class="num">${fmtUsd(Number(p.size) * Number(p.avg_entry_price))}</td>
    </tr>`).join("");
}
function renderEvents(events) {
  const tb = document.getElementById("events");
  if (!events.length) { tb.innerHTML = '<tr><td colspan="7" class="empty">waiting for trades…</td></tr>'; return; }
  tb.innerHTML = events.map(e => {
    const t = e.trade;
    const time = new Date(t.timestamp).toISOString().slice(11, 19);
    const notional = Number(t.size) * Number(t.price);
    return `<tr>
      <td>${time}</td>
      <td class="kind-${e.kind}">${e.kind}</td>
      <td>${t.market_symbol}</td>
      <td class="${t.side}">${t.side.toUpperCase()}</td>
      <td class="num">${fmtSize(t.size)}</td>
      <td class="num">${fmtPrice(t.price)}</td>
      <td class="num">${fmtUsd(notional)}</td>
    </tr>`;
  }).join("");
}
function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = () => setStatus(true, "connected");
  ws.onclose = () => { setStatus(false, "disconnected — retrying"); setTimeout(connect, 2000); };
  ws.onerror = () => setStatus(false, "error");
  ws.onmessage = (msg) => {
    const data = JSON.parse(msg.data);
    if (data.type === "snapshot") {
      document.getElementById("pool").textContent = data.pool_id;
      renderPositions(data.positions);
      renderEvents(data.recent_events);
      if (data.recent_events.length) {
        document.getElementById("last").textContent = "last event " + data.recent_events[0].trade.timestamp;
      }
    } else if (data.type === "event") {
      renderPositions(data.positions);
      renderEvents(data.recent_events);
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
    pool_id = int(os.environ.get("LIGHTER_POOL_ID", "0"))
    if not pool_id:
        raise RuntimeError("LIGHTER_POOL_ID not set in .env")

    lighter = LighterClient(pool_id, REST_BASE, WS_URL)
    tracker = PositionTracker()
    hub = Hub()
    recent_events: list[Event] = []
    last_trade_id: int | None = None

    log.info("bootstrapping markets…")
    await lighter.bootstrap_markets()
    log.info("seeding positions…")
    tracker.seed(await lighter.current_positions())
    log.info("seeded with %d positions", len(tracker.snapshot()))

    # Anchor to most recent trade on first run so we don't replay everything.
    latest = await lighter.fetch_trades_since(since_trade_id=None, limit=1)
    if latest:
        last_trade_id = latest[-1].trade_id
        log.info("anchored last_trade_id=%d", last_trade_id)

    # --- Telegram ---
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_channel = os.environ.get("TELEGRAM_CHANNEL_ID", "")
    tg_owner = os.environ.get("TELEGRAM_OWNER_USER_ID", "")
    min_notional = Decimal("1000")
    tg_client = httpx.AsyncClient(timeout=15.0)
    pool_url = f"https://app.lighter.xyz/public-pools/{pool_id}"
    TG_COOLDOWN_SECONDS = 30
    _tg_last_fired: dict[int, float] = {}  # market_id -> monotonic time

    async def tg_send(chat_id: str, text: str) -> None:
        try:
            r = await tg_client.post(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                data={"chat_id": chat_id, "text": text},
            )
            if not r.json().get("ok"):
                log.warning("tg sendMessage failed: %s", r.text[:200])
        except Exception:
            log.exception("tg_send failed")

    queue: asyncio.Queue[Trade] = asyncio.Queue()

    def snapshot_payload(type_: str, extra: dict | None = None) -> dict:
        payload = {
            "type": type_,
            "pool_id": pool_id,
            "positions": list(tracker.snapshot().values()),
            "recent_events": recent_events[:MAX_RECENT_EVENTS],
        }
        if extra:
            payload.update(extra)
        return payload

    async def ws_producer() -> None:
        async for trade in lighter.stream_trades():
            await queue.put(trade)

    async def rest_safety_producer() -> None:
        nonlocal last_trade_id
        while True:
            await asyncio.sleep(REST_POLL_SECONDS)
            try:
                trades = await lighter.fetch_trades_since(last_trade_id)
                for t in trades:
                    await queue.put(t)
            except Exception:
                log.exception("safety poll failed")

    async def consumer() -> None:
        nonlocal last_trade_id
        while True:
            trade = await queue.get()
            if last_trade_id is not None and trade.trade_id <= last_trade_id:
                continue
            last_trade_id = trade.trade_id
            events = tracker.apply(trade)
            for ev in events:
                ev.leverage = await lighter.fetch_leverage(ev.trade.market_id)
                recent_events.insert(0, ev)
                del recent_events[MAX_RECENT_EVENTS:]
                await hub.broadcast(snapshot_payload("event", {"event": ev}))
                log.info("event %s %s %s @ %s size=%s", ev.kind, ev.trade.side,
                         ev.trade.market_symbol, ev.trade.price, ev.trade.size)
                if tg_token and tg_channel and passes_min_notional(ev, min_notional):
                    import time as _time
                    from .types import EventKind as _EK
                    now = _time.monotonic()
                    last = _tg_last_fired.get(ev.trade.market_id, 0)
                    on_cooldown = (now - last) < TG_COOLDOWN_SECONDS
                    # CLOSE always fires; everything else respects cooldown
                    if ev.kind == _EK.CLOSE or not on_cooldown:
                        await tg_send(tg_channel, format_event(ev, pool_url))
                        _tg_last_fired[ev.trade.market_id] = now
                    else:
                        log.info("cooldown suppressed %s %s (%.0fs remaining)",
                                 ev.kind, ev.trade.market_symbol,
                                 TG_COOLDOWN_SECONDS - (now - last))

    # --- HTTP routes ---
    async def index(_request: web.Request) -> web.Response:
        return web.Response(text=INDEX_HTML, content_type="text/html")

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

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/healthz", healthz)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    log.info("dashboard on http://localhost:8080/  (pool %d)", pool_id)

    await asyncio.gather(ws_producer(), rest_safety_producer(), consumer())


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
