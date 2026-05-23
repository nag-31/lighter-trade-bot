"""Hyperliquid client — built on the official hyperliquid-python-sdk >= 0.23.0.

Public wallet reads require no authentication. Wraps the SDK's Info REST client
and WebsocketManager with the reconnect logic, isSnapshot guard, and Decimal
precision the protocol demands.

Implements the ExchangeClient duck-typed protocol (see sources.py):
    bootstrap_markets / current_positions / fetch_leverage /
    fetch_trades_since / stream_trades / close

Key design decisions (see research file for rationale):
  - Dedup on `tid` (never `hash` — one L1 tx -> many fills).
  - isSnapshot=true messages warm internal state but are NEVER yielded as events.
  - All numeric fields use decimal.Decimal (HL API returns strings).
  - Side: "B" -> "long", "A" -> "short".
  - Exponential-backoff reconnect (1s -> 2s -> 4s, cap 30s); REST gap-fill on reconnect.
  - `clearinghouseState` cached for 5 s (shared between current_positions + fetch_leverage).
  - WebsocketManager runs on its own thread (sync); asyncio bridge via call_soon_threadsafe.

SDK note: WebsocketManager.__init__ expects an HTTP base URL (e.g. https://api.hyperliquid.xyz);
it constructs the WS URL internally by replacing "http" with "ws" and appending "/ws".
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import AsyncIterator, Optional

from hyperliquid.info import Info
from hyperliquid.websocket_manager import WebsocketManager

from .types import Position, Trade

log = logging.getLogger(__name__)

_MAINNET_HTTP = "https://api.hyperliquid.xyz"

# Clearinghouse-state cache TTL in seconds.
_CH_TTL = 5.0

# Reconnect back-off parameters.
_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX = 30.0


def _to_decimal(value) -> Optional[Decimal]:
    """Safely coerce an HL string/numeric value to Decimal. Returns None on failure."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


class HyperliquidClient:
    """Hyperliquid wallet-watcher using the official SDK.

    Parameters
    ----------
    address:
        The 0x wallet address to watch (public; no auth required).
    http_url:
        Override the mainnet HTTP base URL (useful for testnet).
        WebsocketManager derives its WS URL from this automatically.
    ws_url:
        Unused — kept for protocol compatibility. The SDK derives the WS URL
        from http_url. Pass None (the default) in all normal cases.
    source:
        Label injected into every Trade/Position for logging and post formatting.
    """

    def __init__(
        self,
        address: str,
        http_url: Optional[str] = None,
        ws_url: Optional[str] = None,  # noqa: ARG002 — kept for protocol compat
        source: str = "",
    ) -> None:
        self.address = address.lower()
        self.source = source

        self._http_base = (http_url or _MAINNET_HTTP).rstrip("/")

        # SDK Info client handles REST (POST /info). skip_ws=True — we manage WS ourselves.
        self._info = Info(self._http_base, skip_ws=True)

        # coin-index maps populated by bootstrap_markets()
        self._coin_to_id: dict[str, int] = {}
        self._id_to_coin: dict[int, str] = {}

        # Clearinghouse state cache
        self._ch_cache: Optional[dict] = None
        self._ch_cache_ts: float = 0.0

        # Tracks the highest tid seen; used for dedup and gap-fill.
        self._last_tid: Optional[int] = None

        # asyncio event loop captured when stream_trades() first runs.
        # Used to bridge sync WS callbacks -> async queue via call_soon_threadsafe.
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Internal asyncio queue: sync WS callback puts raw fill dicts here;
        # async generator pulls from it. None is the stop sentinel.
        self._fill_queue: asyncio.Queue = asyncio.Queue()

        # Flag to signal clean shutdown to stream_trades()
        self._closed = False

        log.info("[%s] HL client initialized for %s", source, self.address)

    # ------------------------------------------------------------------ #
    # Protocol: close                                                      #
    # ------------------------------------------------------------------ #

    async def close(self) -> None:
        """Signal shutdown. stream_trades() will exit on next iteration."""
        self._closed = True
        # Sentinel to unblock any waiting consumer.
        if self._loop is not None and self._fill_queue is not None:
            self._loop.call_soon_threadsafe(self._fill_queue.put_nowait, None)

    # ------------------------------------------------------------------ #
    # Protocol: bootstrap_markets                                          #
    # ------------------------------------------------------------------ #

    async def bootstrap_markets(self) -> dict[int, str]:
        """Build asset-index -> coin-name map from perp universe. Cached in-process."""
        loop = asyncio.get_event_loop()
        meta = await loop.run_in_executor(None, self._info.meta)
        universe = meta.get("universe") if isinstance(meta, dict) else None
        if isinstance(universe, list):
            for idx, item in enumerate(universe):
                name = str(item.get("name", "")).upper() if isinstance(item, dict) else ""
                if name:
                    self._coin_to_id[name] = idx
                    self._id_to_coin[idx] = name
            log.info("[%s] loaded %d HL market symbols", self.source, len(self._id_to_coin))
        else:
            log.warning(
                "[%s] HL meta() returned no universe — coins get synthetic ids",
                self.source,
            )
        return dict(self._id_to_coin)

    # ------------------------------------------------------------------ #
    # Helpers: coin/id mapping                                             #
    # ------------------------------------------------------------------ #

    def _market_id(self, coin: str) -> int:
        c = coin.upper()
        if c in self._coin_to_id:
            return self._coin_to_id[c]
        synthetic = max(self._id_to_coin.keys(), default=-1) + 1
        self._coin_to_id[c] = synthetic
        self._id_to_coin[synthetic] = c
        log.warning("[%s] HL coin %s not in universe — synthetic id %d", self.source, c, synthetic)
        return synthetic

    def _market_symbol(self, market_id: int) -> str:
        return self._id_to_coin.get(market_id, f"M{market_id}")

    # ------------------------------------------------------------------ #
    # Clearinghouse state cache                                            #
    # ------------------------------------------------------------------ #

    async def _fetch_clearinghouse(self) -> Optional[dict]:
        """Return clearinghouseState with a 5-second TTL cache.

        Both current_positions() and fetch_leverage() share this, so
        back-to-back calls within the same event handler hit the API only once.
        """
        now = time.monotonic()
        if self._ch_cache is not None and now - self._ch_cache_ts < _CH_TTL:
            return self._ch_cache

        loop = asyncio.get_event_loop()
        try:
            data = await loop.run_in_executor(
                None, self._info.user_state, self.address
            )
        except Exception:
            log.exception("[%s] HL user_state() failed", self.source)
            return self._ch_cache  # return stale cache rather than None

        if isinstance(data, dict):
            self._ch_cache = data
            self._ch_cache_ts = now
            return data
        return self._ch_cache

    # ------------------------------------------------------------------ #
    # Protocol: current_positions                                          #
    # ------------------------------------------------------------------ #

    async def current_positions(self) -> dict[int, Position]:
        """Snapshot open perp positions from clearinghouseState."""
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
                szi = _to_decimal(pos.get("szi", "0")) or Decimal(0)
                if szi == 0:
                    continue

                mid = self._market_id(coin)
                side = "long" if szi > 0 else "short"
                avg = _to_decimal(pos.get("entryPx") or "0") or Decimal(0)
                unrealized_pnl = _to_decimal(pos.get("unrealizedPnl"))
                liquidation_px = _to_decimal(pos.get("liquidationPx"))

                out[mid] = Position(
                    market_id=mid,
                    market_symbol=coin.upper(),
                    side=side,
                    size=abs(szi),
                    avg_entry_price=avg,
                    source=self.source,
                    unrealized_pnl=unrealized_pnl,
                    liquidation_px=liquidation_px,
                )
            except Exception:
                log.exception("[%s] could not parse HL position %r", self.source, ap)

        log.debug("[%s] %d open HL positions loaded", self.source, len(out))
        return out

    # ------------------------------------------------------------------ #
    # Protocol: fetch_leverage                                             #
    # ------------------------------------------------------------------ #

    async def fetch_leverage(self, market_id: int) -> Optional[float]:
        """Return leverage for the given market from cached clearinghouseState."""
        data = await self._fetch_clearinghouse()
        if not data:
            return None
        coin = self._market_symbol(market_id)
        for ap in data.get("assetPositions") or []:
            pos = ap.get("position") if isinstance(ap, dict) else None
            if not pos or str(pos.get("coin", "")).upper() != coin:
                continue
            lev = pos.get("leverage")
            if isinstance(lev, dict) and lev.get("value") is not None:
                val = _to_decimal(lev["value"])
                if val is not None:
                    return float(val)
        return None

    # ------------------------------------------------------------------ #
    # Protocol: fetch_trades_since (REST gap-fill)                        #
    # ------------------------------------------------------------------ #

    async def fetch_trades_since(
        self, since_trade_id: Optional[int], limit: int = 100
    ) -> list[Trade]:
        """Return fills with tid > since_trade_id in chronological order.

        Uses user_fills (last 2000) for gap-fill. Lightweight for the typical
        use-case of bridging a short reconnect window.
        """
        loop = asyncio.get_event_loop()
        try:
            raw_fills = await loop.run_in_executor(
                None, self._info.user_fills, self.address
            )
        except Exception:
            log.exception("[%s] HL user_fills() REST failed", self.source)
            return []

        if not isinstance(raw_fills, list):
            log.warning(
                "[%s] HL user_fills returned non-list: %r", self.source, type(raw_fills)
            )
            return []

        trades: list[Trade] = []
        for raw in raw_fills:
            t = self._parse_fill(raw)
            if t is None:
                continue
            if since_trade_id is not None and t.trade_id <= since_trade_id:
                continue
            trades.append(t)

        trades.sort(key=lambda x: x.trade_id)

        # Advance _last_tid so the WS anchor is primed
        if trades:
            latest = trades[-1].trade_id
            if self._last_tid is None or latest > self._last_tid:
                self._last_tid = latest

        return trades[-limit:] if limit else trades

    # ------------------------------------------------------------------ #
    # Protocol: stream_trades (WS primary stream)                         #
    # ------------------------------------------------------------------ #

    async def stream_trades(self) -> AsyncIterator[Trade]:
        """Async generator yielding Trade objects as fills arrive on userFills WS.

        Internally:
        - Wraps WebsocketManager (which runs on its own daemon thread) with
          exponential-backoff reconnect.
        - isSnapshot=true frames warm internal state (update _last_tid) but are
          NEVER yielded as trade events.
        - On reconnect, calls fetch_trades_since(_last_tid) to gap-fill missed fills.
        - Dedup is on `tid`; a local seen-set guards against WS/REST overlap.
        - Also subscribes webData2 to keep clearinghouseState fresh.
        """
        # Capture the running loop for use in sync WS callbacks.
        self._loop = asyncio.get_event_loop()

        seen_tids: set[int] = set()
        backoff = _BACKOFF_INITIAL

        while True:
            if self._closed:
                return

            ws_manager: Optional[WebsocketManager] = None
            try:
                log.info("[%s] HL WS connecting to %s ...", self.source, self._http_base)
                # WebsocketManager takes HTTP base URL; it derives wss:// internally.
                ws_manager = WebsocketManager(self._http_base)
                ws_manager.daemon = True
                ws_manager.start()

                # Wait briefly for on_open to fire so ws_ready becomes True.
                for _ in range(50):
                    if ws_manager.ws_ready:
                        break
                    await asyncio.sleep(0.1)
                else:
                    log.warning("[%s] HL WS did not become ready in 5s — retrying", self.source)
                    ws_manager.stop()
                    raise ConnectionError("WS not ready in time")

                # Subscribe userFills — fills + snapshot warm-state
                ws_manager.subscribe(
                    {"type": "userFills", "user": self.address},
                    self._make_fills_callback(seen_tids),
                )
                # Subscribe webData2 — position reconciliation + leverage refresh
                ws_manager.subscribe(
                    {"type": "webData2", "user": self.address},
                    self._on_webdata2,
                )

                log.info(
                    "[%s] HL WS subscribed to userFills + webData2 for %s",
                    self.source, self.address,
                )
                backoff = _BACKOFF_INITIAL  # reset on successful connect

                # Drain the fill queue and yield Trades
                while True:
                    if self._closed:
                        return

                    # Check WS is still alive (ws_manager stops updating ws_ready on disconnect)
                    if not ws_manager.ws_ready or not ws_manager.is_alive():
                        log.warning("[%s] HL WS manager died — reconnecting", self.source)
                        break

                    try:
                        item = await asyncio.wait_for(
                            self._fill_queue.get(), timeout=5.0
                        )
                    except asyncio.TimeoutError:
                        continue  # poll alive check again

                    if item is None:
                        # Sentinel from close()
                        return

                    # item may be a raw fill dict (from WS callback)
                    # or a pre-parsed Trade (from gap-fill replay)
                    if isinstance(item, Trade):
                        t = item
                    else:
                        t = self._parse_fill(item)
                        if t is None:
                            continue

                    if t.trade_id in seen_tids:
                        log.debug(
                            "[%s] HL dedup skipped tid=%d", self.source, t.trade_id
                        )
                        continue
                    seen_tids.add(t.trade_id)
                    self._last_tid = t.trade_id
                    yield t

            except Exception as e:
                log.warning(
                    "[%s] HL WS error (%s: %s) — reconnecting in %.1fs",
                    self.source, type(e).__name__, e, backoff,
                )
            finally:
                if ws_manager is not None:
                    try:
                        ws_manager.stop()
                    except Exception:
                        pass

            if self._closed:
                return

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)

            # REST gap-fill: replay any fills missed during the disconnect
            if self._last_tid is not None:
                log.info(
                    "[%s] HL REST gap-fill after reconnect (last tid=%d)",
                    self.source, self._last_tid,
                )
                try:
                    missed = await self.fetch_trades_since(self._last_tid)
                    for t in missed:
                        if t.trade_id not in seen_tids:
                            seen_tids.add(t.trade_id)
                            # Put pre-parsed Trade directly to avoid re-parsing
                            await self._fill_queue.put(t)
                except Exception:
                    log.exception("[%s] HL REST gap-fill failed", self.source)

    # ------------------------------------------------------------------ #
    # WS callbacks (called from SDK's sync thread — must be thread-safe)  #
    # ------------------------------------------------------------------ #

    def _make_fills_callback(self, seen_tids: set[int]):
        """Return a callback closure for the userFills WS subscription.

        Called by WebsocketManager on its own thread. Uses call_soon_threadsafe
        to put raw fill dicts onto the asyncio queue.
        """
        def _on_fills(msg: dict) -> None:
            data = msg.get("data")
            if not isinstance(data, dict):
                return

            fills = data.get("fills") or []
            if not isinstance(fills, list):
                return

            is_snapshot = bool(data.get("isSnapshot"))

            if is_snapshot:
                # Warm _last_tid and seen_tids from snapshot data.
                # NEVER enqueue snapshot fills as trade events.
                tids = [int(f.get("tid", 0)) for f in fills if f.get("tid")]
                if tids:
                    snap_max = max(tids)
                    if self._last_tid is None or snap_max > self._last_tid:
                        self._last_tid = snap_max
                    seen_tids.update(tids)
                log.info(
                    "[%s] HL WS isSnapshot=true — %d fills (warm state only, NOT yielded as events)",
                    self.source, len(fills),
                )
                return

            # Live fills — enqueue each one for the async generator to process.
            if self._loop is None:
                return
            for fill in fills:
                try:
                    self._loop.call_soon_threadsafe(self._fill_queue.put_nowait, fill)
                except Exception:
                    log.debug("[%s] HL fill queue put failed (loop gone?)", self.source)

        return _on_fills

    def _on_webdata2(self, msg: dict) -> None:
        """Handle webData2 messages — refresh clearinghouseState cache.

        Called from WebsocketManager's thread; updates are atomic enough for
        our read-heavy access pattern (no asyncio lock needed).
        """
        data = msg.get("data")
        if not isinstance(data, dict):
            return
        ch_state = data.get("clearinghouseState")
        if isinstance(ch_state, dict):
            self._ch_cache = ch_state
            self._ch_cache_ts = time.monotonic()
            log.debug("[%s] HL clearinghouseState refreshed from webData2", self.source)

    # ------------------------------------------------------------------ #
    # Fill parsing                                                         #
    # ------------------------------------------------------------------ #

    def _parse_fill(self, raw) -> Optional[Trade]:
        """Parse a raw HL fill dict into a Trade. Returns None on any error."""
        if not isinstance(raw, dict):
            return None
        try:
            trade_id = int(raw["tid"])
            coin = str(raw["coin"])

            # Perp-only filter — skip spot / builder fills once we have the map.
            if self._coin_to_id and coin.upper() not in self._coin_to_id:
                log.debug(
                    "[%s] HL fill skipped (not perp): coin=%s tid=%d",
                    self.source, coin, trade_id,
                )
                return None

            market_id = self._market_id(coin)

            size = _to_decimal(raw.get("sz"))
            price = _to_decimal(raw.get("px"))
            if size is None or price is None:
                log.warning("[%s] HL fill missing sz/px: %r", self.source, raw)
                return None

            ts_raw = raw.get("time") or 0
            ts = datetime.fromtimestamp(int(ts_raw) / 1000, tz=timezone.utc)

            # "B" = Bid/buy -> long; "A" = Ask/sell -> short
            side = "long" if str(raw.get("side", "")).upper() == "B" else "short"

            realized_pnl = _to_decimal(raw.get("closedPnl"))
            trade_dir: Optional[str] = raw.get("dir")  # "Open Long", "Close Short", etc.

            return Trade(
                trade_id=trade_id,
                timestamp=ts,
                market_id=market_id,
                market_symbol=coin.upper(),
                side=side,
                size=abs(size),
                price=price,
                tx_hash=str(raw.get("hash", "")),
                source=self.source,
                realized_pnl=realized_pnl,
                dir=trade_dir,
                closed_pnl=realized_pnl,
            )
        except (KeyError, ValueError, TypeError) as e:
            log.warning("[%s] could not parse HL fill %r: %s", self.source, raw, e)
            return None
