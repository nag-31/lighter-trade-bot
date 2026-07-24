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
  - `clearinghouseState` cached for 5 s per Hyperliquid DEX (shared between
    current_positions + fetch_leverage).
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

from .result import FetchResult
from .types import OpenOrder, Position, Trade

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
        # Masked form for logging — never log the full wallet address.
        # Shows only the last 4 chars (e.g. "0x…4c74").
        self.address_masked = (
            f"0x…{self.address[-4:]}" if len(self.address) >= 4 else "0x…"
        )
        self.source = source

        self._http_base = (http_url or _MAINNET_HTTP).rstrip("/")

        # SDK Info client handles REST (POST /info). skip_ws=True — we manage WS ourselves.
        self._info = Info(self._http_base, skip_ws=True)

        # coin-index maps populated by bootstrap_markets()
        self._coin_to_id: dict[str, int] = {}
        self._id_to_coin: dict[int, str] = {}
        # Stable frozen set of perp coin names (uppercased), populated once by
        # bootstrap_markets().  Used as the perp-only filter in _parse_fill so
        # that _market_id()'s side-effect of adding synthetic ids to _coin_to_id
        # cannot corrupt the filter (the cascade bug).
        self._perp_universe: set[str] = set()

        # Hyperliquid returns separate clearinghouseState payloads for the
        # default DEX and each HIP-3 DEX. Keep an independent short-lived
        # cache per namespace so position reconciliation does not lose HIP-3
        # positions after reading the default DEX state.
        self._ch_cache: Optional[dict] = None  # compatibility alias for default DEX
        self._ch_cache_ts: float = 0.0
        self._ch_cache_by_dex: dict[str, dict] = {}
        self._ch_cache_ts_by_dex: dict[str, float] = {}
        self._perp_dexes: list[str] = [""]

        # Tracks the highest tid seen for compatibility/logging. HIP-3 DEXes
        # have independent tid sequences, so filtering must use the per-DEX map.
        self._last_tid: Optional[int] = None
        self._last_tid_by_dex: dict[str, int] = {}

        # asyncio event loop captured when stream_trades() first runs.
        # Used to bridge sync WS callbacks -> async queue via call_soon_threadsafe.
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Internal asyncio queue: sync WS callback puts raw fill dicts here;
        # async generator pulls from it. None is the stop sentinel.
        self._fill_queue: asyncio.Queue = asyncio.Queue()

        # Flag to signal clean shutdown to stream_trades()
        self._closed = False

        log.info("[%s] HL client initialized for %s", source, self.address_masked)

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
            # Builder-deployed HIP-3 perps live in separate DEX metadata
            # universes. Their asset IDs use the official 110000 + DEX offset
            # scheme; do not create order-dependent synthetic IDs for them.
            try:
                dex_rows = await loop.run_in_executor(None, self._info.perp_dexs)
                dex_names = [
                    str(row.get("name", "")).strip()
                    for row in (dex_rows or [])
                    if isinstance(row, dict) and str(row.get("name", "")).strip()
                ]
                self._perp_dexes = [""] + dex_names
                for dex_index, dex_name in enumerate(dex_names):
                    dex_meta = await loop.run_in_executor(
                        None, lambda d=dex_name: self._info.meta(dex=d)
                    )
                    dex_universe = dex_meta.get("universe") if isinstance(dex_meta, dict) else None
                    if not isinstance(dex_universe, list):
                        continue
                    offset = 110000 + dex_index * 10000
                    for asset_index, item in enumerate(dex_universe):
                        name = str(item.get("name", "")).upper() if isinstance(item, dict) else ""
                        if name:
                            self._coin_to_id[name] = offset + asset_index
                            self._id_to_coin[offset + asset_index] = name
                log.info(
                    "[%s] loaded %d HL market symbols across default + DEXs %s",
                    self.source, len(self._id_to_coin), dex_names,
                )
            except Exception:
                log.exception("[%s] failed to load HIP-3 DEX metadata", self.source)
            # Populate the stable perp-universe set so _parse_fill can use it
            # as a filter without being affected by _market_id()'s side-effects.
            self._perp_universe = set(self._coin_to_id.keys())
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

    async def _fetch_clearinghouse(self, dex: str = "") -> Optional[dict]:
        """Return clearinghouseState with a 5-second TTL cache.

        Both current_positions() and fetch_leverage() share this per-Dex cache,
        so back-to-back calls within the same event handler hit each API only
        once. HIP-3 DEXes have independent clearinghouseState responses.
        """
        dex = str(dex or "").strip().lower()
        now = time.monotonic()
        cached = self._ch_cache_by_dex.get(dex)
        cached_ts = self._ch_cache_ts_by_dex.get(dex, 0.0)
        if cached is not None and now - cached_ts < _CH_TTL:
            return cached

        loop = asyncio.get_event_loop()
        try:
            data = await loop.run_in_executor(
                None, self._info.user_state, self.address, dex
            )
        except Exception:
            log.exception("[%s] HL user_state(dex=%s) failed", self.source, dex or "default")
            return cached  # return stale cache rather than None

        if isinstance(data, dict):
            self._ch_cache_by_dex[dex] = data
            self._ch_cache_ts_by_dex[dex] = now
            if dex == "":
                self._ch_cache = data
                self._ch_cache_ts = now
            return data
        return cached

    # ------------------------------------------------------------------ #
    # Protocol: current_positions                                          #
    # ------------------------------------------------------------------ #

    async def current_positions(self) -> dict[int, Position]:
        """Snapshot open perp positions across the default and HIP-3 DEXes."""
        dexes = tuple(dict.fromkeys(self._perp_dexes or [""]))
        states = await asyncio.gather(
            *(self._fetch_clearinghouse(dex) for dex in dexes),
            return_exceptions=True,
        )
        self._positions_last_authoritative = all(
            isinstance(state, dict) for state in states
        )

        out: dict[int, Position] = {}
        for dex, data in zip(dexes, states):
            if isinstance(data, Exception) or not isinstance(data, dict):
                continue
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
                    log.exception(
                        "[%s] could not parse HL position from dex=%s: %r",
                        self.source, dex or "default", ap,
                    )

        log.info("[%s] %d open HL positions loaded across DEXs %s", self.source, len(out), list(dexes))
        return out

    async def current_positions_result(self) -> FetchResult[dict[int, Position]]:
        positions = await self.current_positions()
        if getattr(self, "_positions_last_authoritative", False):
            return FetchResult.success(positions)
        return FetchResult.stale(
            positions,
            error="one or more Hyperliquid DEX position snapshots failed",
        )

    # ------------------------------------------------------------------ #
    # Protocol: fetch_leverage                                             #
    # ------------------------------------------------------------------ #

    async def fetch_leverage(self, market_id: int) -> Optional[float]:
        """Return leverage for the given market from its DEX state."""
        coin = self._market_symbol(market_id)
        data = await self._fetch_clearinghouse(self._dex_key(coin))
        if not data:
            return None
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
    # Protocol: fetch_sl_tp                                               #
    # ------------------------------------------------------------------ #

    async def fetch_sl_tp(
        self, market_id: int
    ) -> tuple[Optional[Decimal], Optional[Decimal]]:
        """Return (stop_loss_price, take_profit_price) from open trigger orders.

        HL includes SL/TP in the standard open_orders response as orders with
        triggerCondition='sl'/'tp' and a non-zero triggerPx.
        Returns (None, None) on any error — alerts still fire without SL/TP.
        """
        loop = asyncio.get_event_loop()
        try:
            orders = await loop.run_in_executor(
                None, self._info.open_orders, self.address
            )
        except Exception:
            log.debug("[%s] HL fetch_sl_tp failed", self.source)
            return None, None

        if not isinstance(orders, list):
            return None, None

        coin = self._market_symbol(market_id).upper()
        sl: Optional[Decimal] = None
        tp: Optional[Decimal] = None

        for order in orders:
            if not isinstance(order, dict):
                continue
            if str(order.get("coin", "")).upper() != coin:
                continue

            trigger_px = _to_decimal(order.get("triggerPx"))
            if trigger_px is None or trigger_px == 0:
                continue

            cond = str(order.get("triggerCondition", "")).lower()
            otype = str(order.get("orderType", "")).lower()

            if cond == "sl" or "stop" in otype:
                sl = trigger_px
            elif cond == "tp" or "take profit" in otype:
                tp = trigger_px

        return sl, tp

    # ------------------------------------------------------------------ #
    # Protocol: fetch_open_orders                                         #
    # ------------------------------------------------------------------ #

    async def fetch_open_orders(self) -> list[OpenOrder]:
        """Return all resting/pending orders for this wallet.

        Prefers the SDK's frontend_open_orders() (superset with orderType,
        isPositionTpsl, reduceOnly, triggerCondition, triggerPx) when available;
        falls back to open_orders().  Returns [] on any error.
        """
        loop = asyncio.get_event_loop()
        try:
            # Try the richer endpoint first; it may not exist in older SDK versions.
            if hasattr(self._info, "frontend_open_orders"):
                orders = await loop.run_in_executor(
                    None, self._info.frontend_open_orders, self.address
                )
            else:
                orders = await loop.run_in_executor(
                    None, self._info.open_orders, self.address
                )
        except Exception:
            log.debug("[%s] HL fetch_open_orders failed", self.source)
            return []

        if not isinstance(orders, list):
            return []

        out: list[OpenOrder] = []
        for order in orders:
            if not isinstance(order, dict):
                continue
            try:
                coin = str(order.get("coin", "")).upper()
                if not coin:
                    continue

                market_id = self._market_id(coin)
                side_raw = str(order.get("side", "")).upper()
                side: str = "long" if side_raw == "B" else "short"

                limit_px    = _to_decimal(order.get("limitPx") or order.get("px"))
                trigger_px  = _to_decimal(order.get("triggerPx"))
                size        = _to_decimal(order.get("sz") or order.get("size"))
                reduce_only = bool(order.get("reduceOnly", False))
                oid_raw     = order.get("oid") or order.get("orderId")
                order_id    = int(oid_raw) if oid_raw is not None else None

                # Classify order kind
                cond  = str(order.get("triggerCondition", "")).lower()
                otype = str(order.get("orderType", "")).lower()

                if trigger_px and trigger_px != 0:
                    # Has a trigger — classify as SL or TP
                    if cond == "tp" or "take profit" in otype or "take_profit" in otype:
                        kind = "take_profit"
                    elif cond == "sl" or "stop" in otype:
                        kind = "stop_loss"
                    else:
                        # Best-effort: reduce-only sell protects a long → SL
                        kind = "stop_loss"
                else:
                    kind = "limit"

                out.append(OpenOrder(
                    source=self.source,
                    market_id=market_id,
                    market_symbol=coin,
                    side=side,
                    order_kind=kind,
                    price=limit_px,
                    trigger_px=trigger_px,
                    size=size,
                    reduce_only=reduce_only,
                    order_id=order_id,
                ))
            except Exception:
                log.debug("[%s] HL could not parse open order %r", self.source, order)

        log.debug("[%s] %d HL open orders loaded", self.source, len(out))
        return out

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
        max_tid_by_dex: dict[str, int] = {}
        for raw in raw_fills:
            if isinstance(raw, dict) and raw.get("tid") is not None:
                dex = self._dex_key(str(raw.get("coin", "")))
                tid = int(raw["tid"])
                max_tid_by_dex[dex] = max(max_tid_by_dex.get(dex, tid), tid)
            t = self._parse_fill(raw)
            if t is None:
                continue
            if since_trade_id is not None:
                dex = self._dex_key(t.market_symbol)
                anchor = self._last_tid_by_dex.get(dex)
                if anchor is not None and t.trade_id <= anchor:
                    continue
                # Preserve the old default-Dex fallback for callers that pass
                # a tid before this client has seen a per-DEX anchor. HIP-3
                # tids must not be compared with that default-Dex tid.
                if anchor is None and dex == "" and t.trade_id <= since_trade_id:
                    continue
            trades.append(t)

        # Tids are only monotonic within one DEX. Sort by fill time so a busy
        # default DEX cannot push recent HIP-3 fills out of the limit window.
        trades.sort(key=lambda x: (x.timestamp, x.trade_id))

        # Advance per-DEX anchors from the complete API window, including
        # fills that were filtered as already seen. This primes all DEXes on
        # startup and makes reconnect gap-fill independent of tid ordering.
        for dex, tid in max_tid_by_dex.items():
            if tid > self._last_tid_by_dex.get(dex, -1):
                self._last_tid_by_dex[dex] = tid
            if self._last_tid is None or tid > self._last_tid:
                self._last_tid = tid

        return trades[-limit:] if limit else trades

    async def prime_trade_anchors(self) -> None:
        """Warm every DEX's latest trade cursor without yielding any fills.

        Hyperliquid/HIP-3 trade IDs are only monotonic within their DEX. A
        single persisted global cursor cannot safely filter a fresh REST
        window for every namespace after restart.
        """
        await self.fetch_trades_since(None, limit=1)

    # ------------------------------------------------------------------ #
    # Realizing fills (closes / scale-outs)                               #
    # ------------------------------------------------------------------ #

    async def fetch_realizing_fills(
        self,
        *,
        market_id: Optional[int] = None,
        start_time_ms: Optional[int] = None,
        limit: int = 2000,
    ) -> list[Trade]:
        """Return REALIZING fills (closes / scale-outs) from HL, each carrying the
        exact closedPnl in Trade.realized_pnl. Used by the silent-close backstop and
        the one-time reconciliation sweep.

        When start_time_ms is given, pages via Info.user_fills_by_time(address, start, end)
        back-to-front to cover history; otherwise uses Info.user_fills(address) (last ~2000).
        market_id (optional) filters to one coin.

        A fill is 'realizing' when:
          - its parsed realized_pnl is not None and != 0, OR
          - its dir starts with 'Close'

        Returns fills in oldest-first order (convenient for sequential recording).
        Returns [] and logs on any SDK error — never raises into the caller.
        """
        _MAX_PAGES = 60   # 60 x 2000 = up to 120k raw fills — covers heavy histories

        try:
            if start_time_ms is not None:
                # Page user_fills_by_time from start_time_ms to now.
                raw_fills: list[dict] = []
                seen_tids_page: set[tuple[str, int]] = set()
                page_start = start_time_ms
                now_ms = int(time.time() * 1000)

                for _page in range(_MAX_PAGES):
                    batch = await asyncio.to_thread(
                        self._info.user_fills_by_time,
                        self.address,
                        page_start,
                        now_ms,
                    )
                    if not isinstance(batch, list) or not batch:
                        break
                    added = 0
                    last_time = page_start
                    for fill in batch:
                        if not isinstance(fill, dict):
                            continue
                        tid = fill.get("tid")
                        fill_key = self._fill_key(fill)
                        if fill_key is not None and fill_key in seen_tids_page:
                            continue
                        if fill_key is not None:
                            seen_tids_page.add(fill_key)
                        raw_fills.append(fill)
                        added += 1
                        fill_time = int(fill.get("time", page_start))
                        if fill_time > last_time:
                            last_time = fill_time

                    # Advance window; stop if the batch didn't push us forward
                    if last_time <= page_start or len(batch) < 2000:
                        break
                    # +1 ms to avoid re-fetching the boundary fill
                    page_start = last_time + 1
                    if page_start >= now_ms:
                        break
            else:
                raw_fills_any = await asyncio.to_thread(
                    self._info.user_fills, self.address
                )
                if not isinstance(raw_fills_any, list):
                    log.warning(
                        "[%s] fetch_realizing_fills: user_fills returned non-list: %r",
                        self.source, type(raw_fills_any),
                    )
                    return []
                raw_fills = raw_fills_any

        except Exception:
            log.exception("[%s] fetch_realizing_fills: SDK call failed", self.source)
            return []

        # Parse and filter
        trades: list[Trade] = []
        seen_ids: set[tuple[str, int]] = set()
        for raw in raw_fills:
            try:
                t = self._parse_fill(raw)
            except Exception:
                log.debug("[%s] fetch_realizing_fills: parse error on %r", self.source, raw)
                continue
            if t is None:
                continue
            # De-dupe by trade_id
            fill_key = self._fill_key(raw)
            if fill_key is not None and fill_key in seen_ids:
                continue
            if fill_key is not None:
                seen_ids.add(fill_key)

            # Market-id filter
            if market_id is not None and t.market_id != market_id:
                continue

            # Realizing check: non-zero closedPnl OR dir starts with "Close"
            is_realizing = (
                (t.realized_pnl is not None and t.realized_pnl != 0)
                or (t.dir is not None and str(t.dir).startswith("Close"))
            )
            if not is_realizing:
                continue

            trades.append(t)

        # Oldest-first (convenient for sequential recording)
        trades.sort(key=lambda x: x.trade_id)

        # Cap to limit
        if limit:
            trades = trades[-limit:]

        log.debug(
            "[%s] fetch_realizing_fills: %d realizing fills (market_id=%s, start_ms=%s)",
            self.source, len(trades), market_id, start_time_ms,
        )
        return trades

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

        seen_tids: set[tuple[str, int]] = set()
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
                    self.source, self.address_masked,
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

                    key = self._trade_key(t)
                    if key in seen_tids:
                        log.debug(
                            "[%s] HL dedup skipped tid=%d", self.source, t.trade_id
                        )
                        continue
                    seen_tids.add(key)
                    self._last_tid_by_dex[self._dex_key(t.market_symbol)] = t.trade_id
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
                        key = self._trade_key(t)
                        if key not in seen_tids:
                            seen_tids.add(key)
                            # Put pre-parsed Trade directly to avoid re-parsing
                            await self._fill_queue.put(t)
                except Exception:
                    log.exception("[%s] HL REST gap-fill failed", self.source)

    # ------------------------------------------------------------------ #
    # WS callbacks (called from SDK's sync thread — must be thread-safe)  #
    # ------------------------------------------------------------------ #

    def _make_fills_callback(self, seen_tids: set[tuple[str, int]]):
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
                keys = [self._fill_key(f) for f in fills]
                keys = [key for key in keys if key is not None]
                if keys:
                    for dex, tid in keys:
                        if tid > self._last_tid_by_dex.get(dex, -1):
                            self._last_tid_by_dex[dex] = tid
                        if self._last_tid is None or tid > self._last_tid:
                            self._last_tid = tid
                    seen_tids.update(keys)
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
            self._ch_cache_by_dex[""] = ch_state
            self._ch_cache_ts_by_dex[""] = self._ch_cache_ts
            log.debug("[%s] HL clearinghouseState refreshed from webData2", self.source)

    # ------------------------------------------------------------------ #
    # Fill parsing                                                         #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _dex_key(coin: str) -> str:
        """Return the DEX namespace used by a fill (empty for the default DEX)."""
        return str(coin).split(":", 1)[0].lower() if ":" in str(coin) else ""

    @classmethod
    def _fill_key(cls, raw) -> Optional[tuple[str, int]]:
        if not isinstance(raw, dict) or raw.get("tid") is None:
            return None
        try:
            return cls._dex_key(str(raw.get("coin", ""))), int(raw["tid"])
        except (TypeError, ValueError):
            return None

    @classmethod
    def _trade_key(cls, trade: Trade) -> tuple[str, int]:
        return cls._dex_key(trade.market_symbol), trade.trade_id

    def _parse_fill(self, raw) -> Optional[Trade]:
        """Parse a raw HL fill dict into a Trade. Returns None on any error."""
        if not isinstance(raw, dict):
            return None
        try:
            trade_id = int(raw["tid"])
            coin = str(raw["coin"])

            # Perp-only filter — skip spot fills once we have the
            # stable universe set.  We intentionally use _perp_universe (not
            # _coin_to_id) here: _market_id() adds synthetic entries to
            # _coin_to_id as a side-effect, which made _coin_to_id non-empty
            # after the first fill was parsed, causing every subsequent
            # different coin to fail the old "not in _coin_to_id" check and be
            # silently dropped (the multi-coin cascade bug).
            # _perp_universe is written exactly once by bootstrap_markets() and
            # is never mutated afterwards, so the filter is stable.
            # When _perp_universe is empty (bootstrap not called) the filter is
            # skipped entirely — safe fallback, all fills are parsed.
            # HIP-3 / builder-deployed perps use a dex-prefixed coin such as
            # "xyz:XYZ100"; spot fills use "@<spot_index>". Keep HIP-3 perps
            # even though they are not present in the default meta() universe.
            is_spot_fill = coin.startswith("@")
            is_hip3_perp = ":" in coin and not is_spot_fill
            if self._perp_universe and not is_hip3_perp and coin.upper() not in self._perp_universe:
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
                # Signed position size BEFORE this fill — lets consumers tell a
                # FULL close (fill size >= |startPosition|) from a scale-out.
                start_position=_to_decimal(raw.get("startPosition")),
            )
        except (KeyError, ValueError, TypeError) as e:
            log.warning("[%s] could not parse HL fill %r: %s", self.source, raw, e)
            return None
