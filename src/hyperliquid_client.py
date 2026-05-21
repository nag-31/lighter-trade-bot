"""Hyperliquid client — REST + WebSocket.

Public wallet reads do not require auth. Raw HTTP/WS (no SDK) to mirror
lighter_client.py and keep the surface small. Exposes the same interface the
dashboard depends on (see ExchangeClient in sources.py):
  bootstrap_markets / current_positions / fetch_leverage /
  fetch_trades_since / stream_trades / set_anchor

References:
  REST:  POST https://api.hyperliquid.xyz/info
    {"type":"meta"}                        -> {"universe":[{"name":"BTC",...},...]}
    {"type":"userFills","user":"0x..."}    -> [fill, ...]  (up to 2000 recent)
    {"type":"clearinghouseState","user":"0x..."} -> {"assetPositions":[...], ...}
  WS:   wss://api.hyperliquid.xyz/ws
    subscribe: {"method":"subscribe","subscription":{"type":"userFills","user":"0x..."}}
    response channel: "user"  (NOT "userFills" — common mistake)
    data shape: {"channel":"user","data":{"isSnapshot":bool,"user":"0x...","fills":[...]}}
    isSnapshot=true  → first frame, contains recent history
    isSnapshot=false → live fill pushed in real time
    keepalive: send {"method":"ping"} every 50s (HL drops idle sockets after ~60s)

Fill fields: tid, coin, px, sz, side ("B"=buy/"A"=sell), time (ms), hash,
             dir ("Open Long"/"Close Short"/...), closedPnl, startPosition
Side mapping: "B" → "long", "A" → "short"
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import AsyncIterator, Callable, Optional

import httpx
import websockets

from .types import Position, Trade

log = logging.getLogger(__name__)

INFO_URL = "https://api.hyperliquid.xyz/info"
WS_URL = "wss://api.hyperliquid.xyz/ws"

# WS channel names that carry userFills data (HL uses "user" in practice)
_FILLS_CHANNELS = {"userFills", "user"}

# Clearinghouse state cache TTL in seconds — shared between current_positions()
# and fetch_leverage() so back-to-back calls within one event only hit the API once.
_CH_TTL = 5.0


class HyperliquidClient:
    def __init__(
        self,
        address: str,
        info_url: str = INFO_URL,
        ws_url: str = WS_URL,
        source: str = "",
    ):
        self.address = address.lower()
        self.source = source
        self._info_url = info_url
        self._ws_url = ws_url
        self._http = httpx.AsyncClient(timeout=20.0)

        # coin <-> numeric id maps; populated by bootstrap_markets()
        self._coin_to_id: dict[str, int] = {}
        self._id_to_coin: dict[int, str] = {}

        # Anchor tid set after startup via set_anchor().  The WS snapshot frame
        # (isSnapshot=True) is filtered against this so we don't replay history
        # but also don't miss fills that happened between REST anchor and WS connect.
        self._ws_anchor_tid: Optional[int] = None

        # Clearinghouse state cache
        self._ch_cache: Optional[dict] = None
        self._ch_cache_ts: float = 0.0

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ #
    # Low-level helpers                                                    #
    # ------------------------------------------------------------------ #

    async def _info(self, body: dict) -> Optional[object]:
        try:
            r = await self._http.post(self._info_url, json=body)
            r.raise_for_status()
            return r.json()
        except Exception:
            log.exception("HL info request failed: %s", body.get("type"))
            return None

    async def _fetch_clearinghouse(self) -> Optional[dict]:
        """Return clearinghouseState, using a short TTL cache.

        Both current_positions() and fetch_leverage() call this so consecutive
        calls within the same event handler only make one HTTP request.
        """
        now = time.monotonic()
        if self._ch_cache is not None and now - self._ch_cache_ts < _CH_TTL:
            return self._ch_cache
        data = await self._info({"type": "clearinghouseState", "user": self.address})
        if isinstance(data, dict):
            self._ch_cache = data
            self._ch_cache_ts = now
        return self._ch_cache if isinstance(data, dict) else None

    # ------------------------------------------------------------------ #
    # Bootstrap                                                            #
    # ------------------------------------------------------------------ #

    async def bootstrap_markets(self) -> dict[int, str]:
        """Build coin <-> id maps from the perp universe. Coin index = asset id."""
        data = await self._info({"type": "meta"})
        universe = data.get("universe") if isinstance(data, dict) else None
        if isinstance(universe, list):
            for idx, item in enumerate(universe):
                name = str(item.get("name", "")).upper() if isinstance(item, dict) else ""
                if name:
                    self._coin_to_id[name] = idx
                    self._id_to_coin[idx] = name
            log.info("[%s] loaded %d HL market symbols", self.source, len(self._id_to_coin))
        else:
            log.warning("[%s] HL meta returned no universe — coins will get synthetic ids", self.source)
        return dict(self._id_to_coin)

    def market_id(self, coin: str) -> int:
        """Map a coin name to a stable numeric id."""
        c = coin.upper()
        if c in self._coin_to_id:
            return self._coin_to_id[c]
        synthetic = max(self._id_to_coin.keys(), default=-1) + 1
        self._coin_to_id[c] = synthetic
        self._id_to_coin[synthetic] = c
        log.warning("[%s] HL coin %s not in universe — synthetic id %d", self.source, c, synthetic)
        return synthetic

    def market_symbol(self, market_id: int) -> str:
        return self._id_to_coin.get(market_id, f"M{market_id}")

    # ------------------------------------------------------------------ #
    # Anchor                                                               #
    # ------------------------------------------------------------------ #

    def set_anchor(self, tid: Optional[int]) -> None:
        """Called by the dashboard after anchoring last_trade_id at startup.

        The WS snapshot frame is filtered against this tid so fills that
        arrived between REST anchor and WS subscription are not missed.
        """
        self._ws_anchor_tid = tid
        if tid is not None:
            log.info("[%s] HL WS anchor set to tid=%d", self.source, tid)

    # ------------------------------------------------------------------ #
    # Positions / leverage snapshot                                        #
    # ------------------------------------------------------------------ #

    async def current_positions(self) -> dict[int, Position]:
        """Snapshot open perp positions to seed PositionTracker at startup."""
        data = await self._fetch_clearinghouse()
        if not data:
            return {}
        out: dict[int, Position] = {}
        for ap in data.get("assetPositions") or []:
            try:
                pos = ap.get("position") if isinstance(ap, dict) else None
                if not pos:
                    continue
                coin = str(pos.get("coin", ""))
                szi = Decimal(str(pos.get("szi", "0")))
                if szi == 0:
                    continue
                mid = self.market_id(coin)
                side = "long" if szi > 0 else "short"
                avg = Decimal(str(pos.get("entryPx") or "0"))
                out[mid] = Position(
                    market_id=mid,
                    market_symbol=coin.upper(),
                    side=side,
                    size=abs(szi),
                    avg_entry_price=avg,
                    source=self.source,
                )
            except Exception:
                log.exception("[%s] could not parse HL position %r", self.source, ap)
        log.info("[%s] %d open HL positions seeded", self.source, len(out))
        return out

    async def fetch_leverage(self, market_id: int) -> Optional[float]:
        """Return current leverage on the given market, or None if unknown."""
        data = await self._fetch_clearinghouse()
        if not data:
            return None
        coin = self.market_symbol(market_id)
        for ap in data.get("assetPositions") or []:
            pos = ap.get("position") if isinstance(ap, dict) else None
            if not pos or str(pos.get("coin", "")).upper() != coin:
                continue
            lev = pos.get("leverage")
            if isinstance(lev, dict) and lev.get("value") is not None:
                try:
                    return float(lev["value"])
                except (TypeError, ValueError):
                    pass
        return None

    # ------------------------------------------------------------------ #
    # Trades: REST safety net                                              #
    # ------------------------------------------------------------------ #

    async def fetch_trades_since(
        self, since_trade_id: Optional[int], limit: int = 100
    ) -> list[Trade]:
        """Return fills with tid > since_trade_id in chronological order.

        Also updates _ws_anchor_tid to the highest tid seen so the WS snapshot
        filter is primed when stream_trades() connects shortly after.
        """
        data = await self._info({"type": "userFills", "user": self.address})
        if not isinstance(data, list):
            log.warning("[%s] HL userFills returned non-list: %r", self.source, type(data))
            return []

        trades: list[Trade] = []
        for raw in data:
            t = self._parse_fill(raw)
            if t is None:
                continue
            if since_trade_id is not None and t.trade_id <= since_trade_id:
                continue
            trades.append(t)

        trades.sort(key=lambda x: x.trade_id)

        # Prime the WS anchor with the highest tid from this batch
        if trades:
            latest_tid = trades[-1].trade_id
            if self._ws_anchor_tid is None or latest_tid > self._ws_anchor_tid:
                self._ws_anchor_tid = latest_tid

        return trades[-limit:] if limit else trades

    # ------------------------------------------------------------------ #
    # Trades: WebSocket primary stream                                     #
    # ------------------------------------------------------------------ #

    async def stream_trades(self) -> AsyncIterator[Trade]:
        """Yield fills as they arrive on the wallet's userFills WS channel.

        Auto-reconnects with exponential backoff. The first isSnapshot frame is
        processed (not skipped) — fills with tid > _ws_anchor_tid are yielded so
        nothing is missed between REST anchor and WS connect. Sends a keepalive
        ping every 50s to prevent HL's ~60s idle timeout.
        """
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(
                    self._ws_url, ping_interval=None, close_timeout=10
                ) as ws:
                    await ws.send(json.dumps({
                        "method": "subscribe",
                        "subscription": {"type": "userFills", "user": self.address},
                    }))
                    log.info("[%s] HL WS subscribed to userFills/%s", self.source, self.address)
                    backoff = 1.0

                    keepalive_task = asyncio.create_task(self._keepalive(ws))
                    try:
                        async for raw_msg in ws:
                            try:
                                msg = json.loads(raw_msg)
                            except json.JSONDecodeError:
                                continue
                            for raw_fill in self._extract_fills(msg, since_tid=self._ws_anchor_tid):
                                t = self._parse_fill(raw_fill)
                                if t is not None:
                                    # Keep anchor up to date as fills arrive
                                    if self._ws_anchor_tid is None or t.trade_id > self._ws_anchor_tid:
                                        self._ws_anchor_tid = t.trade_id
                                    yield t
                    finally:
                        keepalive_task.cancel()
                        try:
                            await keepalive_task
                        except asyncio.CancelledError:
                            pass

            except Exception as e:
                log.warning(
                    "[%s] HL WS disconnected (%s) — reconnecting in %.1fs",
                    self.source, e, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _keepalive(self, ws) -> None:
        while True:
            await asyncio.sleep(50)
            try:
                await ws.send(json.dumps({"method": "ping"}))
            except Exception:
                return

    # ------------------------------------------------------------------ #
    # Message parsing                                                      #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _extract_fills(msg: dict, since_tid: Optional[int] = None) -> list[dict]:
        """Pull fill dicts from a WS message.

        HL sends the userFills subscription data under channel "user" (not
        "userFills" as one might expect). We accept both to be safe.

        isSnapshot=True  → first frame after subscribe; contains recent fills.
                           Filtered by since_tid so we don't replay history but
                           also don't miss fills that arrived during the gap
                           between REST anchor and WS connection.
        isSnapshot=False → live fill; always yielded.
        """
        if msg.get("channel") not in _FILLS_CHANNELS:
            return []
        data = msg.get("data")
        if not isinstance(data, dict):
            return []
        fills = data.get("fills") or []
        if not isinstance(fills, list):
            return []

        is_snapshot = bool(data.get("isSnapshot"))
        if is_snapshot:
            if since_tid is None:
                # No anchor yet — skip snapshot entirely to avoid replaying
                # all historical fills on a cold start.
                log.info("HL WS snapshot received — skipped (no anchor tid yet)")
                return []
            # Filter snapshot to only fills newer than our anchor
            new_fills = [f for f in fills if int(f.get("tid", 0)) > since_tid]
            log.info(
                "HL WS snapshot: %d total fills, %d new after tid=%d",
                len(fills), len(new_fills), since_tid,
            )
            return new_fills

        return fills

    def _parse_fill(self, raw: dict) -> Optional[Trade]:
        try:
            trade_id = int(raw["tid"])
            coin = str(raw["coin"])

            # Perp-only filter: skip spot and builder fills.
            # If bootstrap failed and the map is empty, fall through (fail open).
            if self._coin_to_id and coin.upper() not in self._coin_to_id:
                log.debug(
                    "[%s] HL fill skipped (not perp): coin=%s tid=%d",
                    self.source, coin, trade_id,
                )
                return None

            market_id = self.market_id(coin)
            size = Decimal(str(raw["sz"]))
            price = Decimal(str(raw["px"]))
            ts_raw = raw.get("time") or 0
            ts = datetime.fromtimestamp(int(ts_raw) / 1000, tz=timezone.utc)

            # "B" = buy (long direction), "A" = sell (short direction)
            side = "long" if str(raw.get("side", "")).upper() == "B" else "short"

            return Trade(
                trade_id=trade_id,
                timestamp=ts,
                market_id=market_id,
                market_symbol=coin.upper(),
                side=side,
                size=size,
                price=price,
                tx_hash=str(raw.get("hash", "")),
                source=self.source,
            )
        except (KeyError, ValueError, TypeError) as e:
            log.warning("[%s] could not parse HL fill %r: %s", self.source, raw, e)
            return None
