"""Hyperliquid client — REST + WebSocket.

Public wallet reads do not require auth. Raw HTTP/WS (no SDK) to mirror
lighter_client.py and keep the surface small. Exposes the same interface the
dashboard depends on (see ExchangeClient in sources.py):
  bootstrap_markets / current_positions / fetch_leverage /
  fetch_trades_since / stream_trades

References:
  REST:  POST https://api.hyperliquid.xyz/info
    {"type":"meta"}              -> {"universe":[{"name":"BTC",...}, ...]}  (index = asset id)
    {"type":"userFills","user":"0x..."}          -> [fill, ...]
    {"type":"clearinghouseState","user":"0x..."} -> {"assetPositions":[...], ...}
  WS:   wss://api.hyperliquid.xyz/ws
    subscribe {"method":"subscribe","subscription":{"type":"userFills","user":"0x..."}}
    channel "userFills", data {"isSnapshot":bool,"user":"0x...","fills":[...]}
    keepalive: send {"method":"ping"} (HL drops idle sockets after ~60s)
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import AsyncIterator, Optional

import httpx
import websockets

from .types import Position, Trade

log = logging.getLogger(__name__)

INFO_URL = "https://api.hyperliquid.xyz/info"
WS_URL = "wss://api.hyperliquid.xyz/ws"


class HyperliquidClient:
    def __init__(self, address: str, info_url: str = INFO_URL, ws_url: str = WS_URL, source: str = ""):
        self.address = address.lower()
        self.source = source
        self._info_url = info_url
        self._ws_url = ws_url
        self._http = httpx.AsyncClient(timeout=20.0)
        # coin <-> numeric id maps; populated by bootstrap_markets()
        self._coin_to_id: dict[str, int] = {}
        self._id_to_coin: dict[int, str] = {}

    async def close(self) -> None:
        await self._http.aclose()

    # ----- low-level POST -----

    async def _info(self, body: dict) -> Optional[object]:
        try:
            r = await self._http.post(self._info_url, json=body)
            r.raise_for_status()
            return r.json()
        except Exception:
            log.exception("HL info request failed: %s", body.get("type"))
            return None

    # ----- bootstrap -----

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
            log.info("loaded %d HL market symbols", len(self._id_to_coin))
        else:
            log.warning("HL meta returned no universe — coins will get synthetic ids")
        return dict(self._id_to_coin)

    def market_id(self, coin: str) -> int:
        """Map a coin name to a stable numeric id. Newly listed coins not in the
        universe get a synthetic id so tracking still works."""
        c = coin.upper()
        if c in self._coin_to_id:
            return self._coin_to_id[c]
        synthetic = max(self._id_to_coin.keys(), default=-1) + 1
        self._coin_to_id[c] = synthetic
        self._id_to_coin[synthetic] = c
        log.warning("HL coin %s not in universe — assigned synthetic id %d", c, synthetic)
        return synthetic

    def market_symbol(self, market_id: int) -> str:
        return self._id_to_coin.get(market_id, f"M{market_id}")

    # ----- positions / leverage snapshot -----

    async def current_positions(self) -> dict[int, Position]:
        """Snapshot the wallet's open perp positions to seed PositionTracker."""
        data = await self._info({"type": "clearinghouseState", "user": self.address})
        if not isinstance(data, dict):
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
                log.exception("could not parse HL position %r", ap)
        return out

    async def fetch_leverage(self, market_id: int) -> Optional[float]:
        """Return current leverage on the given market, or None if unknown."""
        data = await self._info({"type": "clearinghouseState", "user": self.address})
        if not isinstance(data, dict):
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
                    return None
        return None

    # ----- trades: REST safety net -----

    async def fetch_trades_since(self, since_trade_id: Optional[int], limit: int = 100) -> list[Trade]:
        """Return fills with tid > since_trade_id in chronological order."""
        data = await self._info({"type": "userFills", "user": self.address})
        if not isinstance(data, list):
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
        return trades[-limit:] if limit else trades

    # ----- trades: WebSocket primary stream -----

    async def stream_trades(self) -> AsyncIterator[Trade]:
        """Yield fills as they arrive on the wallet's userFills WS channel.

        Auto-reconnects with backoff. Skips the initial isSnapshot frame so we
        don't replay history. Sends a keepalive ping every 50s.
        """
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self._ws_url, ping_interval=20, ping_timeout=20) as ws:
                    await ws.send(json.dumps({
                        "method": "subscribe",
                        "subscription": {"type": "userFills", "user": self.address},
                    }))
                    log.info("HL WS subscribed to userFills/%s", self.address)
                    backoff = 1.0

                    keepalive_task = asyncio.create_task(self._keepalive(ws))
                    try:
                        async for raw_msg in ws:
                            try:
                                msg = json.loads(raw_msg)
                            except json.JSONDecodeError:
                                continue
                            for raw_fill in self._extract_fills(msg):
                                t = self._parse_fill(raw_fill)
                                if t is not None:
                                    yield t
                    finally:
                        keepalive_task.cancel()
            except Exception as e:
                log.warning("HL WS disconnected (%s) — reconnecting in %.1fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _keepalive(self, ws) -> None:
        while True:
            await asyncio.sleep(50)
            try:
                await ws.send(json.dumps({"method": "ping"}))
            except Exception:
                return

    @staticmethod
    def _extract_fills(msg: dict) -> list[dict]:
        """Pull fill dicts from a WS message.

        userFills push: {"channel":"userFills","data":{"isSnapshot":bool,"fills":[...]}}
        The first frame after subscribe carries isSnapshot=true (history) — skip it.
        """
        if msg.get("channel") != "userFills":
            return []
        data = msg.get("data")
        if not isinstance(data, dict):
            return []
        if data.get("isSnapshot"):
            return []
        fills = data.get("fills")
        return fills if isinstance(fills, list) else []

    # ----- shared parser -----

    def _parse_fill(self, raw: dict) -> Optional[Trade]:
        try:
            trade_id = int(raw["tid"])
            coin = str(raw["coin"])
            # userFills carries spot + builder-market fills too — track perps only.
            # Skip anything not in the perp universe (unless bootstrap failed and
            # the universe map is empty, in which case fall through).
            if self._coin_to_id and coin.upper() not in self._coin_to_id:
                return None
            market_id = self.market_id(coin)
            size = Decimal(str(raw["sz"]))
            price = Decimal(str(raw["px"]))
            ts_raw = raw.get("time") or 0
            ts = datetime.fromtimestamp(int(ts_raw) / 1000, tz=timezone.utc)
            # HL side: "B" = buy (long-direction), "A" = sell (short-direction)
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
            log.warning("could not parse HL fill %r: %s", raw, e)
            return None
