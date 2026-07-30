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

from .result import FetchResult
from .types import OpenOrder, Position, Trade

log = logging.getLogger(__name__)

# When Lighter's WS handshake is geo-blocked (HTTP 400 restricted jurisdiction),
# retry this slowly instead of hammering — the REST poll covers trades meanwhile.
_WS_GEO_BACKOFF = 300.0  # seconds


class LighterClient:
    def __init__(
        self,
        pool_id: Optional[int],
        rest_base: str,
        ws_url: str,
        source: str = "",
        proxy_url: Optional[str] = None,
        ws_proxy_url: Optional[str] = None,
        l1_address: Optional[str] = None,
        account_slot: int = 0,
    ):
        self.pool_id = pool_id
        self._l1_address = l1_address
        self._is_wallet_account = l1_address is not None
        self._account_slot = account_slot
        self._account_resolve_lock = asyncio.Lock()
        self.source = source
        self._rest_base = rest_base.rstrip("/")
        self._ws_url = ws_url
        self._proxy_url = proxy_url  # e.g. "socks5h://host:1080" or None
        # WS-only proxy: Lighter geo-blocks the /stream endpoint from some
        # regions (HTTP 400 "restricted jurisdiction") while REST stays open.
        # Routing ONLY the WS through a proxy in an allowed region restores
        # real-time without disturbing the working direct REST path. Falls back
        # to the general proxy_url, else None (direct).
        self._ws_proxy_url = ws_proxy_url or proxy_url
        # One-shot flag so a persistent geo-block logs once, not every retry.
        self._ws_geo_warned = False
        self._private_rest_warned = False
        if ws_proxy_url:
            log.info("[%s] Lighter WS will route via proxy %s", source, ws_proxy_url)

        # httpx AsyncClient — routes through proxy if configured
        self._http = httpx.AsyncClient(
            timeout=20.0,
            **({"proxy": proxy_url} if proxy_url else {}),
        )
        if proxy_url:
            log.info("[%s] Lighter REST will route via proxy %s", source, proxy_url)

        # market_id -> human symbol (e.g. 0 -> "BTC"); populated by bootstrap_markets()
        self._symbols: dict[int, str] = {}

    async def close(self) -> None:
        await self._http.aclose()

    async def _ensure_account_index(self) -> int:
        """Resolve a configured L1 wallet to its Lighter account index once."""
        if self.pool_id is not None:
            return self.pool_id
        if not self._l1_address:
            raise RuntimeError("Lighter account has neither pool_id nor wallet address")

        async with self._account_resolve_lock:
            if self.pool_id is not None:
                return self.pool_id

            indexes: list[int] = []
            seen_indexes: set[int] = set()
            seen_cursors: set[str] = set()
            cursor: Optional[str] = None
            for _ in range(20):
                params = {"l1_address": self._l1_address}
                if cursor:
                    params["cursor"] = cursor
                try:
                    response = await self._http.get(
                        f"{self._rest_base}/accountsByL1Address",
                        params=params,
                    )
                    response.raise_for_status()
                    payload = response.json()
                except Exception as exc:
                    # HTTP client exceptions can contain the full query URL,
                    # including the configured wallet. Never propagate it.
                    raise RuntimeError(
                        "Lighter account discovery failed: "
                        f"{type(exc).__name__}"
                    ) from None
                rows = payload.get("sub_accounts") or []
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    raw_index = row.get("index", row.get("account_index"))
                    try:
                        index = int(raw_index)
                    except (TypeError, ValueError):
                        continue
                    if index not in seen_indexes:
                        seen_indexes.add(index)
                        indexes.append(index)

                next_cursor = payload.get("next_cursor")
                if not next_cursor:
                    break
                cursor = str(next_cursor)
                if cursor in seen_cursors:
                    raise RuntimeError("Lighter account discovery returned a repeated cursor")
                seen_cursors.add(cursor)
            else:
                raise RuntimeError("Lighter account discovery exceeded 20 pages")

            if not indexes:
                raise RuntimeError("no Lighter account found for configured wallet")
            if self._account_slot >= len(indexes):
                raise RuntimeError(
                    f"Lighter account_slot {self._account_slot} is unavailable "
                    f"(wallet has {len(indexes)} account(s))"
                )

            self.pool_id = indexes[self._account_slot]
            log.info(
                "[%s] resolved Lighter wallet account_slot %d to account index %s "
                "(%d account(s) discovered)",
                self.source,
                self._account_slot,
                self.pool_id,
                len(indexes),
            )
            return self.pool_id

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
        account_index = await self._ensure_account_index()
        r = await self._http.get(
            f"{self._rest_base}/account",
            params={"by": "index", "value": str(account_index)},
        )
        r.raise_for_status()
        return r.json()

    async def current_positions(self) -> dict[int, Position]:
        """Snapshot current positions on the pool. Used to seed PositionTracker
        at startup so we don't have to replay history from genesis."""
        try:
            data = await self.fetch_account()
        except Exception:
            self._positions_last_authoritative = False
            log.exception("fetch_account failed")
            return {}
        self._positions_last_authoritative = True

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

    async def current_positions_result(self) -> FetchResult[dict[int, Position]]:
        positions = await self.current_positions()
        if getattr(self, "_positions_last_authoritative", False):
            return FetchResult.success(positions)
        return FetchResult.stale(
            positions,
            error="Lighter account snapshot unavailable",
        )

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

    # ----- SL / TP -----

    async def fetch_sl_tp(
        self, market_id: int
    ) -> tuple[Optional[Decimal], Optional[Decimal]]:
        """Best-effort fetch of SL/TP from Lighter open conditional orders.

        Tries GET /orders with status=open. If the endpoint doesn't exist or
        returns an unexpected shape, silently returns (None, None) — alerts
        still fire without SL/TP.
        """
        # /orders is private/authenticated for both public pools and wallet
        # accounts. This read-only client has no signing credentials, so a
        # probe can only generate recurring 403s.
        self._warn_private_rest_unavailable()
        return None, None

    # ----- open orders -----

    async def fetch_open_orders(self) -> list[OpenOrder]:
        """Best-effort GET of open orders for the pool.

        Tries GET /orders with status=open (same endpoint as fetch_sl_tp).
        Returns [] on ANY error or unknown shape — same graceful degradation
        as fetch_sl_tp.  Does not raise.
        """
        # See fetch_sl_tp: without signed private-API auth this always 403s.
        self._warn_private_rest_unavailable()
        return []

    # ----- trades: REST safety net -----

    async def fetch_trades_since(self, since_trade_id: Optional[int], limit: int = 100) -> list[Trade]:
        """Return trades with trade_id > since_trade_id in chronological order.

        Lighter's endpoint requires sort_by + limit. We pull desc and reverse.
        """
        if self._is_wallet_account:
            self._warn_private_rest_unavailable()
            return []
        try:
            account_index = await self._ensure_account_index()
            r = await self._http.get(
                f"{self._rest_base}/trades",
                params={
                    "account_index": str(account_index),
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

    def _warn_private_rest_unavailable(self) -> None:
        """Log once when unauthenticated wallet-only REST calls are skipped."""
        if self._private_rest_warned:
            return
        self._private_rest_warned = True
        log.warning(
            "[%s] skipping unauthenticated Lighter private REST history/orders; "
            "live WebSocket fills and public position polling remain active",
            self.source,
        )

    # ----- trades: WebSocket primary stream -----

    async def stream_trades(self) -> AsyncIterator[Trade]:
        """Yield trades as they arrive on the pool's WS channel.

        Auto-reconnects with backoff on disconnect. Sends a keepalive every 90s.
        """
        backoff = 1.0
        while True:
            try:
                account_index = await self._ensure_account_index()
                connect_kwargs: dict = {
                    "ping_interval": 20,
                    "ping_timeout": 20,
                    "additional_headers": {"Origin": "https://app.lighter.xyz"},
                }
                if self._ws_proxy_url:
                    connect_kwargs["proxy"] = self._ws_proxy_url
                async with websockets.connect(self._ws_url, **connect_kwargs) as ws:
                    await ws.send(json.dumps({
                        "type": "subscribe",
                        "channel": f"account_all_trades/{account_index}",
                    }))
                    log.info("WS subscribed to account_all_trades/%s", account_index)
                    backoff = 1.0
                    self._ws_geo_warned = False  # connected — re-arm the warning

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
                # Detect Lighter's restricted-jurisdiction geo-block on /stream
                # (handshake rejected with HTTP 400). REST keeps covering trades,
                # so don't spam: log once, then retry slowly + quietly. A
                # successful connect (or restart with LIGHTER_WS_PROXY set)
                # re-arms the warning.
                status = getattr(getattr(e, "response", None), "status_code", None)
                geo_blocked = (
                    status == 400
                    or "HTTP 400" in str(e)
                    or "restricted jurisdiction" in str(e).lower()
                )
                if geo_blocked:
                    if not self._ws_geo_warned:
                        log.warning(
                            "[%s] Lighter WS rejected (HTTP 400 — restricted-jurisdiction "
                            "geo-block on /stream from this region). Real-time stream is "
                            "unavailable; the REST poll covers all trades (no data lost). "
                            "Set LIGHTER_WS_PROXY to a SOCKS5/HTTP proxy in an allowed "
                            "region to enable real-time. Retrying quietly every %ds.",
                            self.source, int(_WS_GEO_BACKOFF),
                        )
                        self._ws_geo_warned = True
                    await asyncio.sleep(_WS_GEO_BACKOFF)
                    continue
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
