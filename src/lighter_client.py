"""Lighter.xyz client — REST + WebSocket.

Public-pool reads do not require auth. We use raw HTTP/WS instead of the
lighter-sdk to keep the surface small and the failure modes obvious; the SDK
can be dropped in later without changing this module's interface.

References (per research):
  REST base: https://mainnet.zklighter.elliot.ai/api/v1
    GET /trades?account_index=<pool>&sort_by=trade_id&sort_dir=desc&limit=1..100
    GET /account?by=index&value=<pool>
    GET /orderBooks  (or similar — for market_id -> symbol map)
  WS:   wss://mainnet.zklighter.elliot.ai/stream
    channel: account_all_trades/<pool_id>   (push-based, no auth)
    keepalive: send a frame every 2 minutes
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


class LighterClient:
    def __init__(self, pool_id: int, rest_base: str, ws_url: str, source: str = ""):
        self.pool_id = pool_id
        self.source = source
        self._rest_base = rest_base.rstrip("/")
        self._ws_url = ws_url
        self._http = httpx.AsyncClient(timeout=20.0)
        # market_id -> human symbol (e.g. 0 -> "BTC"); populated by bootstrap_markets()
        self._symbols: dict[int, str] = {}

    async def close(self) -> None:
        await self._http.aclose()

    # ----- bootstrap -----

    async def bootstrap_markets(self) -> dict[int, str]:
        """Build market_id -> symbol map. Tries a few likely endpoint names; falls
        back to an empty map (callers should treat unknown markets as 'M<id>')."""
        for path in ("/orderBooks", "/orderbooks", "/markets"):
            try:
                r = await self._http.get(f"{self._rest_base}{path}")
                if r.status_code != 200:
                    continue
                data = r.json()
                items = data if isinstance(data, list) else data.get("order_books") or data.get("markets") or []
                out: dict[int, str] = {}
                for it in items:
                    mid = it.get("market_id", it.get("id"))
                    sym = it.get("symbol", it.get("base", ""))
                    if mid is not None and sym:
                        out[int(mid)] = str(sym).upper()
                if out:
                    self._symbols = out
                    log.info("loaded %d market symbols", len(out))
                    return out
            except Exception:
                log.exception("market lookup via %s failed", path)
        log.warning("could not resolve market symbols — falling back to numeric ids")
        return {}

    def market_symbol(self, market_id: int) -> str:
        return self._symbols.get(market_id, f"M{market_id}")

    # ----- positions / leverage snapshot -----

    async def fetch_account(self) -> dict:
        r = await self._http.get(
            f"{self._rest_base}/account",
            params={"by": "index", "value": str(self.pool_id)},
        )
        r.raise_for_status()
        return r.json()

    async def current_positions(self) -> dict[int, Position]:
        """Snapshot current positions on the pool. Used to seed PositionTracker
        at startup so we don't have to replay history from genesis."""
        try:
            data = await self.fetch_account()
        except Exception:
            log.exception("fetch_account failed")
            return {}

        positions_raw = (
            (data.get("accounts") or [{}])[0].get("positions")
            if isinstance(data.get("accounts"), list) else None
        ) or data.get("positions") or []

        out: dict[int, Position] = {}
        for p in positions_raw:
            try:
                mid = int(p.get("market_id"))
                size = Decimal(str(p.get("position", "0")))
                if size == 0:
                    continue
                sign = int(p.get("sign", 1))
                side = "long" if sign > 0 else "short"
                symbol = str(p.get("symbol") or self.market_symbol(mid))
                avg = Decimal(str(p.get("avg_entry_price", "0")))

                unrealized_pnl: Optional[Decimal] = None
                upnl_str = p.get("unrealized_pnl") or p.get("unrealizedPnl")
                if upnl_str is not None:
                    try:
                        unrealized_pnl = Decimal(str(upnl_str))
                    except Exception:
                        pass

                liquidation_px: Optional[Decimal] = None
                liq_str = p.get("liquidation_price") or p.get("liquidationPx")
                if liq_str is not None:
                    try:
                        liquidation_px = Decimal(str(liq_str))
                    except Exception:
                        pass

                out[mid] = Position(
                    market_id=mid,
                    market_symbol=symbol,
                    side=side,
                    size=abs(size),
                    avg_entry_price=avg,
                    source=self.source,
                    unrealized_pnl=unrealized_pnl,
                    liquidation_px=liquidation_px,
                )
            except Exception:
                log.exception("could not parse position %r", p)
        return out

    async def fetch_leverage(self, market_id: int) -> Optional[float]:
        """Return current leverage on the given market, or None if unknown."""
        try:
            data = await self.fetch_account()
        except Exception:
            return None
        positions_raw = (
            (data.get("accounts") or [{}])[0].get("positions")
            if isinstance(data.get("accounts"), list) else None
        ) or data.get("positions") or []
        for p in positions_raw:
            if int(p.get("market_id", -1)) == market_id:
                # initial_margin_fraction comes back as a percent string e.g. "5.00".
                # leverage = 100 / IMF.
                imf = p.get("initial_margin_fraction")
                if imf:
                    try:
                        f = float(imf)
                        if f > 0:
                            return 100.0 / f
                    except (TypeError, ValueError):
                        pass
        return None

    # ----- trades: REST safety net -----

    async def fetch_trades_since(self, since_trade_id: Optional[int], limit: int = 100) -> list[Trade]:
        """Return trades with trade_id > since_trade_id in chronological order.

        Lighter's endpoint requires sort_by + limit. We pull desc and reverse.
        """
        try:
            r = await self._http.get(
                f"{self._rest_base}/trades",
                params={
                    "account_index": str(self.pool_id),
                    "sort_by": "trade_id",
                    "sort_dir": "desc",
                    "limit": str(limit),
                },
            )
            r.raise_for_status()
            payload = r.json()
        except Exception:
            log.exception("fetch_trades_since failed")
            return []

        raw_list = payload.get("trades") if isinstance(payload, dict) else payload
        if not isinstance(raw_list, list):
            return []

        trades: list[Trade] = []
        for raw in raw_list:
            t = self._parse_trade(raw)
            if t is None:
                continue
            if since_trade_id is not None and t.trade_id <= since_trade_id:
                continue
            trades.append(t)
        trades.sort(key=lambda x: x.trade_id)
        return trades

    # ----- trades: WebSocket primary stream -----

    async def stream_trades(self) -> AsyncIterator[Trade]:
        """Yield trades as they arrive on the pool's WS channel.

        Auto-reconnects with backoff on disconnect. Sends a keepalive every 90s.
        """
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(
                    self._ws_url,
                    ping_interval=20,
                    ping_timeout=20,
                    additional_headers={"Origin": "https://app.lighter.xyz"},
                ) as ws:
                    await ws.send(json.dumps({
                        "type": "subscribe",
                        "channel": f"account_all_trades/{self.pool_id}",
                    }))
                    log.info("WS subscribed to account_all_trades/%s", self.pool_id)
                    backoff = 1.0

                    keepalive_task = asyncio.create_task(self._keepalive(ws))
                    try:
                        async for raw_msg in ws:
                            try:
                                msg = json.loads(raw_msg)
                            except json.JSONDecodeError:
                                continue
                            for raw_trade in self._extract_trades(msg):
                                t = self._parse_trade(raw_trade)
                                if t is not None:
                                    yield t
                    finally:
                        keepalive_task.cancel()
            except Exception as e:
                log.warning("WS disconnected (%s) — reconnecting in %.1fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _keepalive(self, ws) -> None:
        while True:
            await asyncio.sleep(90)
            try:
                await ws.send(json.dumps({"type": "ping"}))
            except Exception:
                return

    @staticmethod
    def _extract_trades(msg: dict) -> list[dict]:
        """Pull trade dicts from a WS message.

        Lighter's account_all_trades channel uses two shapes:
          - subscribed snapshot: {"type": "subscribed/account_all_trades", "trades": []}
          - update:              {"type": "update/account_all_trades",
                                  "trades": {"<market_id>": [trade, ...]}}
        """
        msg_type = msg.get("type", "")
        if msg_type.startswith("subscribed") or msg_type in {"pong", "error"}:
            return []
        val = msg.get("trades")
        if isinstance(val, list):
            return val
        if isinstance(val, dict):
            out: list[dict] = []
            for trades_list in val.values():
                if isinstance(trades_list, list):
                    out.extend(trades_list)
            return out
        if "trade_id" in msg and "market_id" in msg:
            return [msg]
        return []

    # ----- shared parser -----

    def _parse_trade(self, raw: dict) -> Optional[Trade]:
        try:
            trade_id = int(raw["trade_id"])
            market_id = int(raw["market_id"])
            size = Decimal(str(raw["size"]))
            price = Decimal(str(raw["price"]))
            ts_raw = raw.get("timestamp") or raw.get("created_at") or 0
            if isinstance(ts_raw, (int, float)):
                # Lighter timestamps are typically ms since epoch
                ts = datetime.fromtimestamp(ts_raw / 1000 if ts_raw > 1e12 else ts_raw, tz=timezone.utc)
            else:
                ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
            ask = int(raw.get("ask_account_id", -1))
            bid = int(raw.get("bid_account_id", -1))
            if ask == self.pool_id:
                side = "short"   # pool is the seller
            elif bid == self.pool_id:
                side = "long"    # pool is the buyer
            else:
                # not our pool; can happen on broad channels — caller should ignore
                return None
            return Trade(
                trade_id=trade_id,
                timestamp=ts,
                market_id=market_id,
                market_symbol=self.market_symbol(market_id),
                side=side,
                size=size,
                price=price,
                tx_hash=str(raw.get("tx_hash", "")),
                source=self.source,
            )
        except (KeyError, ValueError, TypeError) as e:
            log.warning("could not parse trade %r: %s", raw, e)
            return None
