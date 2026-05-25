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

from .db import init_db, load_recent_events, save_event
from .filters import passes_min_notional
from .formatter import format_aggregate, format_event, format_reduce_aggregate
from .pnl_card import calculate_pnl, generate_pnl_card, record_result
from .sources import Source, load_sources
from .types import Event, EventKind, Position, Trade

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("dashboard")

REST_POLL_SECONDS = 60
MAX_RECENT_EVENTS = 200
DB_PATH = Path(__file__).parent.parent / "data" / "events.db"
PIDFILE = Path("/tmp/lighterbot.pid")
TG_DEDUP_WINDOW = 90.0  # suppress identical TG messages within this many seconds


def _acquire_pid_lock() -> bool:
    """Return True if we successfully claimed the singleton lock, False if another instance is running."""
    if PIDFILE.exists():
        try:
            pid = int(PIDFILE.read_text().strip())
            os.kill(pid, 0)  # signal 0 = probe only; raises if process doesn't exist
            log.error("Another lighterbot instance is already running (PID %d). Exiting.", pid)
            return False
        except (ProcessLookupError, PermissionError, ValueError):
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
  .kind-REDUCE { color: #fb923c; }
  .empty { color:#4b5563; font-style: italic; padding: 8px; }
  .num { text-align: right; font-variant-numeric: tabular-nums; }
  @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<h1>Trade tracker</h1>
<div class="meta"><span id="status"><span class="dot off"></span>connecting</span> &middot; <span id="sources">no sources</span> &middot; <span id="last">no events yet</span></div>
<div class="grid">
  <section>
    <h2>Open positions</h2>
    <table>
      <thead><tr><th>Source</th><th>Market</th><th>Side</th><th class="num">Size</th><th class="num">Entry</th><th class="num">Notional</th><th class="num">Unreal. P&amp;L</th><th class="num">Liq. Price</th></tr></thead>
      <tbody id="positions"></tbody>
    </table>
  </section>
  <section>
    <h2>Recent events</h2>
    <table>
      <thead><tr><th>Time IST</th><th>Source</th><th>Kind</th><th>Market</th><th>Side</th><th class="num">Size</th><th class="num">Price</th><th class="num">Notional</th></tr></thead>
      <tbody id="events"></tbody>
    </table>
  </section>
</div>
<script>
const toIST = ts => {
  const d = new Date(new Date(ts).getTime() + 5.5 * 60 * 60 * 1000);
  return d.toISOString().slice(11, 19);
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
  tb.innerHTML = positions.map(p => `
    <tr>
      <td>${esc(p.source)}</td>
      <td>${esc(p.market_symbol)}</td>
      <td class="${p.side}">${p.side.toUpperCase()}</td>
      <td class="num">${fmtSize(p.size)}</td>
      <td class="num">${fmtPrice(p.avg_entry_price)}</td>
      <td class="num">${fmtUsd(Number(p.size) * Number(p.avg_entry_price))}</td>
      <td class="num">${fmtPnl(p.unrealized_pnl)}</td>
      <td class="num">${p.liquidation_px != null ? fmtPrice(p.liquidation_px) : "—"}</td>
    </tr>`).join("");
}
function renderEvents(events) {
  const tb = document.getElementById("events");
  if (!events.length) { tb.innerHTML = '<tr><td colspan="8" class="empty">waiting for trades…</td></tr>'; return; }
  tb.innerHTML = events.map(e => {
    const t = e.trade;
    const time = toIST(t.timestamp);
    const notional = Number(t.size) * Number(t.price);
    return `<tr>
      <td>${time}</td>
      <td>${esc(t.source)}</td>
      <td class="kind-${e.kind}">${e.kind}</td>
      <td>${esc(t.market_symbol)}</td>
      <td class="${t.side}">${t.side.toUpperCase()}</td>
      <td class="num">${fmtSize(t.size)}</td>
      <td class="num">${fmtPrice(t.price)}</td>
      <td class="num">${fmtUsd(notional)}</td>
    </tr>`;
  }).join("");
}
function renderSources(sources) {
  const s = sources || [];
  document.getElementById("sources").textContent =
    s.length ? s.length + " source" + (s.length > 1 ? "s" : "") + ": " + s.join(", ") : "no sources";
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
      renderEvents(data.recent_events);
      if (data.recent_events.length) {
        document.getElementById("last").textContent = "last event " + data.recent_events[0].trade.timestamp;
      }
    } else if (data.type === "event") {
      renderSources(data.sources);
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

    if not _acquire_pid_lock():
        return
    try:
        await _run()
    finally:
        _release_pid_lock()


async def _run() -> None:
    hub = Hub()
    sources: list[Source] = load_sources()
    by_id: dict[str, Source] = {s.id: s for s in sources}

    log.info("initialising database…")
    await init_db(DB_PATH)
    # recent_events holds Event objects (new this session) and dicts (loaded from DB).
    # _to_jsonable handles both transparently.
    recent_events: list[Any] = list(await load_recent_events(DB_PATH, MAX_RECENT_EVENTS))
    log.info("loaded %d persisted events from db", len(recent_events))

    # Bootstrap every source: markets, seed positions, anchor last_trade_id so
    # we don't replay history.
    for s in sources:
        log.info("[%s] bootstrapping markets…", s.name)
        await s.client.bootstrap_markets()
        s.tracker.seed(await s.client.current_positions())
        log.info("[%s] seeded with %d positions", s.name, len(s.tracker.snapshot()))
        latest = await s.client.fetch_trades_since(since_trade_id=None, limit=1)
        if latest:
            s.last_trade_id = latest[-1].trade_id
            log.info("[%s] anchored last_trade_id=%d", s.name, s.last_trade_id)
        # Tell HL client the anchor so WS snapshot is filtered correctly
        if hasattr(s.client, "set_anchor"):
            s.client.set_anchor(s.last_trade_id)

    # --- Telegram ---
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_channel = os.environ.get("TELEGRAM_CHANNEL_ID", "")
    tg_client = httpx.AsyncClient(timeout=15.0)
    AGGREGATE_WINDOW = 30  # seconds — SIZE_CHANGE fills accumulate before one alert fires

    # SIZE_CHANGE aggregate buffer
    # (source_id, market_id) -> {net_added, n_fills, leverage, position, task}
    _pending: dict[tuple[str, int], dict] = {}

    # REDUCE aggregate buffer — same structure, separate dict
    # (source_id, market_id) -> {net_reduced, n_fills, total_pnl, leverage, position, task}
    _pending_reduces: dict[tuple[str, int], dict] = {}

    # Dedup guard: MD5(alert text) -> monotonic timestamp of last send
    _tg_sent: dict[str, float] = {}

    async def tg_send(text: str) -> None:
        h = hashlib.md5(text.encode()).hexdigest()
        now = time.monotonic()
        # Evict expired entries to prevent unbounded growth
        expired = [k for k, t in _tg_sent.items() if now - t > TG_DEDUP_WINDOW]
        for k in expired:
            del _tg_sent[k]
        if h in _tg_sent:
            log.warning("tg_send: suppressed duplicate alert (%.0fs since last send)", now - _tg_sent[h])
            return
        _tg_sent[h] = now
        try:
            r = await tg_client.post(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                data={"chat_id": tg_channel, "text": text},
            )
            if not r.json().get("ok"):
                log.warning("tg sendMessage failed: %s", r.text[:200])
        except Exception:
            log.exception("tg_send failed")

    async def tg_send_photo(image_bytes: bytes, caption: str = "") -> None:
        """Send a PNG image to Telegram. Falls back to plain text on error."""
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

    async def _get_sl_tp(src: Source, market_id: int):
        """Fetch SL/TP from client; returns (None, None) silently on any error."""
        try:
            return await src.client.fetch_sl_tp(market_id)
        except Exception:
            return None, None

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
        text = format_aggregate(
            position=pos,
            net_added_usd=buf["net_added"],
            n_fills=buf["n_fills"],
            leverage=buf["leverage"],
            pool_url=src.url,
            source_name=src.name,
            sl=sl,
            tp=tp,
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
        )
        log.info("[%s] reduce aggregate alert: %s −$%.0f across %d fills → remaining $%.0f",
                 src.name, pos.market_symbol, buf["net_reduced"], buf["n_fills"],
                 pos.notional_usd)
        await tg_send(text)

    def _accumulate_reduce(source_id: str, ev: Event) -> None:
        key = (source_id, ev.trade.market_id)
        fill_notional = ev.trade.size * ev.trade.price
        pnl = ev.trade.realized_pnl
        current_pos = by_id[source_id].tracker.snapshot().get(ev.trade.market_id)
        if key in _pending_reduces:
            _pending_reduces[key]["net_reduced"] += fill_notional
            _pending_reduces[key]["n_fills"] += 1
            if pnl is not None:
                prev = _pending_reduces[key]["total_pnl"]
                _pending_reduces[key]["total_pnl"] = (prev or Decimal(0)) + pnl
            if ev.leverage is not None:
                _pending_reduces[key]["leverage"] = ev.leverage
            if current_pos is not None:
                _pending_reduces[key]["position"] = current_pos
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
        # can overwrite it during the 30s accumulation window.
        current_pos = by_id[source_id].tracker.snapshot().get(ev.trade.market_id)
        if key in _pending:
            _pending[key]["net_added"] += fill_notional
            _pending[key]["n_fills"] += 1
            if ev.leverage is not None:
                _pending[key]["leverage"] = ev.leverage
            if current_pos is not None:
                _pending[key]["position"] = current_pos
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

    def all_positions() -> list[Position]:
        out: list[Position] = []
        for s in sources:
            out.extend(s.tracker.snapshot().values())
        return out

    def snapshot_payload(type_: str, extra: dict | None = None) -> dict:
        payload = {
            "type": type_,
            "sources": [s.name for s in sources],
            "positions": all_positions(),
            "recent_events": recent_events[:MAX_RECENT_EVENTS],
        }
        if extra:
            payload.update(extra)
        return payload

    async def position_reconciler(src: Source) -> None:
        """Every 60s: pull ground-truth positions from the exchange API and
        reconcile against the local tracker.

        Positions are ALWAYS replaced by API data — the tracker's calculated
        state is only used for fill-by-fill event classification.  This ensures
        the dashboard and Telegram always reflect blockchain reality.

        If a position disappeared from the API while we weren't watching (e.g.
        closed via many small REDUCE fills each below min_notional), we send a
        TG alert so the user is always notified of a close.
        """
        while True:
            await asyncio.sleep(60)
            try:
                actual = await src.client.current_positions()
                tracked = src.tracker.snapshot()

                # --- detect silently-closed positions ---
                for market_id, pos in tracked.items():
                    if market_id not in actual:
                        log.warning(
                            "[%s] reconcile: %s %s closed while unobserved",
                            src.name, pos.side, pos.market_symbol,
                        )
                        if tg_token and tg_channel:
                            direction = "🟢 LONG" if pos.side == "long" else "🔴 SHORT"
                            notional = pos.notional_usd
                            footer = f"\n{src.url}" if src.url else ""
                            msg = (
                                f"📍 {src.name}\n"
                                f"Closed {direction} {pos.market_symbol}\n"
                                f"Size: {pos.size:,.4f}  |  Entry: ${pos.avg_entry_price:,.2f}\n"
                                f"Notional: ${notional:,.0f}  (close price unavailable)"
                                f"{footer}"
                            )
                            await tg_send(msg)

                # --- detect positions the tracker missed entirely ---
                for market_id, pos in actual.items():
                    if market_id not in tracked:
                        log.warning(
                            "[%s] reconcile: %s %s found in API but missing from tracker — seeding",
                            src.name, pos.side, pos.market_symbol,
                        )

                # --- always sync tracker to API truth ---
                # Replaces tracker's internally-calculated positions with the
                # real on-chain values (size, entry, unrealizedPnl, liquidationPx).
                src.tracker.seed(actual)

                # Broadcast fresh position snapshot to all dashboard clients
                await hub.broadcast(snapshot_payload("snapshot"))

            except Exception:
                log.exception("[%s] position reconciler failed", src.name)

    async def ws_producer(src: Source) -> None:
        async for trade in src.client.stream_trades():
            await queue.put((src.id, trade))

    async def rest_safety_producer(src: Source) -> None:
        while True:
            await asyncio.sleep(REST_POLL_SECONDS)
            try:
                trades = await src.client.fetch_trades_since(src.last_trade_id)
                for t in trades:
                    await queue.put((src.id, t))
            except Exception:
                log.exception("[%s] safety poll failed", src.name)

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
                ev.leverage = await src.client.fetch_leverage(ev.trade.market_id)
                recent_events.insert(0, ev)
                del recent_events[MAX_RECENT_EVENTS:]
                await hub.broadcast(snapshot_payload("event", {"event": ev}))
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
                    if passes_min_notional(ev, src.min_notional):
                        sl, tp = await _get_sl_tp(src, ev.trade.market_id)
                        await tg_send(format_event(ev, src.url, src.name, sl=sl, tp=tp))

                elif ev.kind == EventKind.CLOSE:
                    # Cancel any pending SIZE_CHANGE or REDUCE aggregate — position gone
                    _cancel_pending(key)
                    buf = _pending_reduces.pop(key, None)
                    if buf:
                        buf["task"].cancel()
                    # Generate PnL card and send as photo; fall back to text
                    pnl = calculate_pnl(ev)
                    is_win = pnl is not None and pnl > 0
                    wins, total = record_result(is_win)
                    card_bytes = generate_pnl_card(ev, src.name, wins, total)
                    if card_bytes:
                        await tg_send_photo(card_bytes, caption=src.url)
                    else:
                        await tg_send(format_event(ev, src.url, src.name))

                elif ev.kind == EventKind.REDUCE:
                    # Batch partial-close fills over 30s — avoids spam when many
                    # small fills close a position incrementally.
                    if passes_min_notional(ev, src.min_notional):
                        _accumulate_reduce(source_id, ev)

                elif ev.kind == EventKind.SIZE_CHANGE:
                    # Batch same-side adds over 30s — avoids spam for rapid scaling in.
                    _accumulate_size_change(source_id, ev)

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
    log.info("dashboard on http://localhost:8080/  (%d source(s))", len(sources))

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
