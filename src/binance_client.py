"""Binance USDT-M Futures client.

Implements the ExchangeClient duck-typed protocol (see sources.py):
    bootstrap_markets / current_positions / fetch_leverage /
    fetch_trades_since / stream_trades / close

Auth: HMAC-SHA256 signed requests. Credentials loaded from env at source-build
time (sources.py) and passed in here. Never logged.

Fail-safes baked in:
  - Missing API key/secret   → skipped in sources.py before this class is built
  - Auth error (401/403)     → logs error, returns empty; bot keeps running
  - Hedge mode detected      → all methods no-op; clear error in logs
  - Listen key expiry        → automatic reconnect with new listen key
  - Network error            → exponential-backoff reconnect (1s → 30s)
  - REST errors              → logged, empty result returned (never raises)
  - Zero-size / zero-price   → fill silently skipped

One-way mode only. Hedge mode (dualSidePosition=true) disables this source
entirely — user must switch to one-way mode in Binance settings.

REST:  https://fapi.binance.com
WS:    wss://fstream.binance.com/ws/<listenKey>
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import AsyncIterator, Optional

import httpx
import websockets

from .result import FetchResult
from .types import OpenOrder, Position, Trade

# Imported here (not TYPE_CHECKING) so proxy_pool instances can be passed
# directly without a forward-reference cast. proxy_pool.py has no import from
# binance_client so there is no circular dependency.
from .proxy_pool import ProxyPool  # noqa: E402


class BinanceUnavailable(Exception):
    """Raised when all proxies are exhausted or Binance is unreachable.

    The caller must treat this as a transient error: log it, mark the
    source down in the health registry, and continue with other sources.
    """

log = logging.getLogger(__name__)

_REST_BASE       = "https://fapi.binance.com"
_WS_BASE         = "wss://fstream.binance.com/ws"
_POS_TTL         = 5.0        # seconds — positionRisk cache TTL
_KEEPALIVE_SEC   = 25 * 60   # 25 minutes — PUT listenKey interval
_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX     = 30.0


def _to_decimal(v) -> Optional[Decimal]:
    """Safely coerce a Binance string/numeric value to Decimal."""
    if v is None:
        return None
    try:
        d = Decimal(str(v))
        return d if d.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


class BinanceClient:
    """Binance USDT-M Futures watcher.

    Parameters
    ----------
    api_key:
        Binance API key.  Read-only «Futures» permission is enough.
    api_secret:
        Binance API secret.
    source:
        Label injected into every Trade/Position for display.
    rest_base / ws_base:
        Override URLs for testnet.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        source: str = "",
        rest_base: str = _REST_BASE,
        ws_base: str = _WS_BASE,
        proxy_url: Optional[str] = None,
        proxy_pool: "Optional[ProxyPool]" = None,
    ) -> None:
        self.source = source
        self._api_key    = api_key
        self._api_secret = api_secret.encode()   # bytes for hmac
        self._rest_base  = rest_base.rstrip("/")
        self._ws_base    = ws_base.rstrip("/")
        self._proxy_url  = proxy_url   # single proxy fallback (legacy)
        self._proxy_pool = proxy_pool  # rotating pool (preferred when set)

        # Market maps ---------------------------------------------------
        # _sym_to_id : "BTCUSDT" → int   (full Binance symbol)
        # _id_to_disp: int → "BTC"       (display label in alerts)
        # _id_to_full: int → "BTCUSDT"   (full symbol for API calls)
        self._sym_to_id:  dict[str, int] = {}
        self._id_to_disp: dict[int, str] = {}
        self._id_to_full: dict[int, str] = {}
        self._position_ids: dict[tuple[str, str], int] = {}
        self._recent_symbols: set[str] = set()

        # positionRisk cache
        self._pos_cache:    Optional[list] = None
        self._pos_cache_ts: float = 0.0

        # Set True in bootstrap if dualSidePosition=true; disables source
        self._hedge_mode: bool = False
        self._pos_last_authoritative: bool = False
        self._time_offset_ms: int = 0
        self._auth_disabled: bool = False
        self._rate_limited_until: float = 0.0

        # WS / shutdown
        self._closed:     bool = False
        self._listen_key: Optional[str] = None

        # When a proxy_pool is set, individual requests build per-proxy
        # clients in _request().  Otherwise use a shared persistent client
        # (legacy behaviour: single optional proxy or direct).
        if proxy_pool:
            self._http = httpx.AsyncClient(
                base_url=self._rest_base,
                headers={"X-MBX-APIKEY": self._api_key},
                timeout=10.0,
            )
            log.info("[%s] Binance client initialised with rotating proxy pool (%d proxies)",
                     source, proxy_pool.n_total)
        else:
            self._http = httpx.AsyncClient(
                base_url=self._rest_base,
                headers={"X-MBX-APIKEY": self._api_key},
                timeout=10.0,
                **({"proxy": proxy_url} if proxy_url else {}),
            )
            if proxy_url:
                log.info("[%s] Binance REST will route via proxy %s", source, proxy_url)
        log.info("[%s] Binance client initialized", source)

    # ------------------------------------------------------------------ #
    # Protocol: close                                                      #
    # ------------------------------------------------------------------ #

    async def close(self) -> None:
        self._closed = True
        try:
            await self._http.aclose()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Proxy-pool aware HTTP dispatcher                                     #
    # ------------------------------------------------------------------ #

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Dispatch a single HTTP request, rotating through the proxy pool on failure.

        When no pool is configured, falls back to the shared ``self._http``
        client (which may itself have a static proxy configured).

        On 451 (geo-block), timeout, or connection error with a pool:
          - marks the used proxy dead
          - tries the next working proxy
          - up to ``min(pool_size, 5)`` attempts total

        Raises
        ------
        BinanceUnavailable
            When all pool proxies fail or the pool has no working proxy.
        httpx.HTTPError / other
            Propagated unchanged when no pool is configured (callers handle
            these with try/except as before).
        """
        pool = self._proxy_pool

        # ── No pool: use the shared persistent client ───────────────────
        if pool is None:
            url = self._rest_base + path
            return await self._http.request(method, url, **kwargs)

        # ── Proxy-pool path ─────────────────────────────────────────────
        max_attempts = min(pool.n_total, 5)
        last_error   = "no working proxy"

        for attempt in range(max_attempts):
            proxy_url = await pool.get_working()
            if proxy_url is None:
                raise BinanceUnavailable(
                    f"[{self.source}] Binance proxy pool exhausted — {last_error}"
                )

            t0 = time.monotonic()
            try:
                # Build a short-lived per-proxy client for clean rotation.
                # httpx 0.28: proxy= (singular); older: proxies= (dict).
                major, minor = (int(x) for x in httpx.__version__.split(".")[:2])
                proxy_kwarg: dict
                if (major, minor) >= (0, 28):
                    proxy_kwarg = {"proxy": proxy_url}
                else:
                    proxy_kwarg = {"proxies": {"all://": proxy_url}}  # type: ignore[dict-item]

                async with httpx.AsyncClient(
                    base_url=self._rest_base,
                    headers={"X-MBX-APIKEY": self._api_key},
                    timeout=kwargs.pop("timeout", 10.0),
                    **proxy_kwarg,
                ) as client:
                    response = await client.request(method, path, **kwargs)

                # Geo-block → rotate
                if response.status_code == 451:
                    last_error = f"HTTP 451 geo-block via {proxy_url}"
                    log.warning("[%s] Binance 451 via %s — rotating proxy", self.source, proxy_url)
                    pool.mark_dead(proxy_url, "HTTP 451 geo-block")
                    continue

                latency_ms = int((time.monotonic() - t0) * 1000)
                pool.mark_ok(proxy_url, latency_ms=latency_ms)
                return response

            except (httpx.TimeoutException, httpx.ConnectError, httpx.ProxyError) as exc:
                last_error = f"{type(exc).__name__}: {exc} via {proxy_url}"
                log.warning("[%s] Binance proxy %s failed (%s: %s) — rotating",
                            self.source, proxy_url, type(exc).__name__, exc)
                pool.mark_dead(proxy_url, str(exc))
                # Remove timeout kwarg already popped above from successive attempts
                kwargs.pop("timeout", None)
                continue

        raise BinanceUnavailable(
            f"[{self.source}] Binance unavailable after {max_attempts} proxy attempt(s) — {last_error}"
        )

    # ------------------------------------------------------------------ #
    # Ping / liveness check                                                #
    # ------------------------------------------------------------------ #

    async def ping(self) -> bool:
        """Lightweight liveness probe against the Binance ping endpoint.

        Routes through the proxy pool when one is configured.  Used at
        bootstrap to decide whether the Binance source is reachable at all.

        Returns True on HTTP 200, False on any error (including BinanceUnavailable).
        """
        try:
            r = await self._request("GET", "/fapi/v1/ping")
            return r.status_code == 200
        except BinanceUnavailable:
            return False
        except Exception as exc:
            log.debug("[%s] Binance ping failed: %s", self.source, exc)
            return False

    # ------------------------------------------------------------------ #
    # HMAC-SHA256 signing                                                  #
    # ------------------------------------------------------------------ #

    def _sign(self, params: dict) -> dict:
        """Append timestamp + HMAC-SHA256 signature to *params* (mutates copy)."""
        params["timestamp"] = int(time.time() * 1000) + self._time_offset_ms
        params.setdefault("recvWindow", 5000)
        qs  = urllib.parse.urlencode(params)
        sig = hmac.new(self._api_secret, qs.encode(), hashlib.sha256).hexdigest()
        params["signature"] = sig
        return params

    # ------------------------------------------------------------------ #
    # REST helpers                                                         #
    # ------------------------------------------------------------------ #

    async def _get(self, path: str, params: dict | None = None, signed: bool = True):
        if signed and self._auth_disabled:
            return None
        if time.monotonic() < self._rate_limited_until:
            return None
        p = dict(params or {})
        if signed:
            p = self._sign(p)
        try:
            r = await self._request("GET", path, params=p)
            r.raise_for_status()
            return r.json()
        except BinanceUnavailable:
            log.error("[%s] Binance GET %s — BinanceUnavailable (all proxies exhausted)", self.source, path)
            return None
        except httpx.HTTPStatusError as e:
            try:
                body = e.response.json()
            except Exception:
                body = {}
            code = body.get("code") if isinstance(body, dict) else None
            if code in {-2014, -2015} or e.response.status_code in {401, 403}:
                self._auth_disabled = True
                log.error(
                    "[%s] Binance credentials rejected; automatic authenticated "
                    "retries paused until process restart/config reload",
                    self.source,
                )
            elif code == -1021:
                await self._sync_server_time()
            if e.response.status_code in {418, 429}:
                retry_after = e.response.headers.get("Retry-After", "60")
                try:
                    delay = max(1.0, float(retry_after))
                except ValueError:
                    delay = 60.0
                self._rate_limited_until = time.monotonic() + delay
            log.error(
                "[%s] Binance GET %s → HTTP %d: %s",
                self.source, path, e.response.status_code, e.response.text[:200],
            )
            return None
        except Exception:
            log.exception("[%s] Binance GET %s failed", self.source, path)
            return None

    async def _sync_server_time(self) -> None:
        """Refresh Binance clock offset after error -1021."""
        try:
            response = await self._request("GET", "/fapi/v1/time")
            response.raise_for_status()
            body = response.json()
            server_time = int(body["serverTime"])
            self._time_offset_ms = server_time - int(time.time() * 1000)
            log.info("[%s] Binance server-time offset refreshed", self.source)
        except Exception:
            log.warning("[%s] Binance server-time sync failed", self.source)

    async def _post(self, path: str, params: dict | None = None):
        p = self._sign(dict(params or {}))
        try:
            r = await self._request("POST", path, params=p)
            r.raise_for_status()
            return r.json()
        except BinanceUnavailable:
            log.error("[%s] Binance POST %s — BinanceUnavailable (all proxies exhausted)", self.source, path)
            return None
        except httpx.HTTPStatusError as e:
            log.error(
                "[%s] Binance POST %s → HTTP %d: %s",
                self.source, path, e.response.status_code, e.response.text[:200],
            )
            return None
        except Exception:
            log.exception("[%s] Binance POST %s failed", self.source, path)
            return None

    async def _put(self, path: str, params: dict | None = None):
        p = self._sign(dict(params or {}))
        try:
            r = await self._request("PUT", path, params=p)
            r.raise_for_status()
            return r.json()
        except BinanceUnavailable:
            log.error("[%s] Binance PUT %s — BinanceUnavailable (all proxies exhausted)", self.source, path)
            return None
        except httpx.HTTPStatusError as e:
            log.error(
                "[%s] Binance PUT %s → HTTP %d: %s",
                self.source, path, e.response.status_code, e.response.text[:200],
            )
            return None
        except Exception:
            log.exception("[%s] Binance PUT %s failed", self.source, path)
            return None

    # ------------------------------------------------------------------ #
    # Protocol: bootstrap_markets                                          #
    # ------------------------------------------------------------------ #

    async def bootstrap_markets(self) -> dict[int, str]:
        """Load all USDT-M perpetual symbols and detect position mode."""
        # 1. Detect position mode — must fail-fast before any WS subscription.
        mode_data = await self._get("/fapi/v1/positionSide/dual")
        if isinstance(mode_data, dict):
            if mode_data.get("dualSidePosition", False):
                self._hedge_mode = True
                log.info(
                    "[%s] Binance hedge mode (dualSidePosition=true) is ENABLED on "
                    "this account. LONG and SHORT legs are tracked independently. "
                    "Go to Binance Futures settings → disable hedge mode → restart bot.  "
                    "Hedge-mode support is active.",
                    self.source,
                )
        else:
            log.warning(
                "[%s] Binance could not read positionSide/dual — assuming one-way mode",
                self.source,
            )

        # 2. Load exchange info (public, unsigned).
        data = await self._get("/fapi/v1/exchangeInfo", signed=False)
        if not isinstance(data, dict):
            log.warning("[%s] Binance exchangeInfo failed — empty market map", self.source)
            return {}

        idx = 0
        for sym_info in data.get("symbols") or []:
            if not isinstance(sym_info, dict):
                continue
            sym    = str(sym_info.get("symbol", ""))
            ctype  = str(sym_info.get("contractType", ""))
            quote  = str(sym_info.get("quoteAsset", ""))
            status = str(sym_info.get("status", ""))
            if ctype != "PERPETUAL" or quote != "USDT" or status != "TRADING":
                continue
            base = str(sym_info.get("baseAsset", sym.replace("USDT", "")))
            self._sym_to_id[sym]  = idx
            self._id_to_disp[idx] = base   # e.g. "BTC" — shown in TG messages
            self._id_to_full[idx] = sym    # e.g. "BTCUSDT" — used in API calls
            idx += 1

        log.info("[%s] loaded %d Binance USDT-M perpetual markets", self.source, idx)
        return dict(self._id_to_disp)

    # ------------------------------------------------------------------ #
    # Helpers: symbol/id mapping                                           #
    # ------------------------------------------------------------------ #

    def _market_id(self, symbol: str) -> int:
        s = symbol.upper()
        if s in self._sym_to_id:
            return self._sym_to_id[s]
        # Assign a synthetic id for unknown symbols (e.g. new listings)
        synthetic = max(self._id_to_disp.keys(), default=-1) + 1
        self._sym_to_id[s]          = synthetic
        self._id_to_disp[synthetic] = s.replace("USDT", "")
        self._id_to_full[synthetic] = s
        log.warning(
            "[%s] Binance symbol %s not in market map — synthetic id %d",
            self.source, s, synthetic,
        )
        return synthetic

    def _market_symbol(self, market_id: int) -> str:
        return self._id_to_disp.get(market_id, f"M{market_id}")

    def _full_symbol(self, market_id: int) -> str:
        return self._id_to_full.get(market_id, f"M{market_id}USDT")

    def _position_market_id(self, symbol: str, position_side: str) -> int:
        """Return a distinct market id for BOTH/LONG/SHORT position legs."""
        sym = symbol.upper()
        base_id = self._market_id(sym)
        ps = position_side.upper()
        if ps == "BOTH":
            return base_id
        key = (sym, ps)
        if key in self._position_ids:
            return self._position_ids[key]
        side_bit = 0 if ps == "LONG" else 1
        position_id = 1_000_000_000 + base_id * 2 + side_bit
        self._position_ids[key] = position_id
        self._id_to_disp[position_id] = self._id_to_disp[base_id]
        self._id_to_full[position_id] = sym
        return position_id

    # ------------------------------------------------------------------ #
    # positionRisk cache (shared by current_positions + fetch_leverage)   #
    # ------------------------------------------------------------------ #

    async def _fetch_position_risk(self) -> Optional[list]:
        """GET /fapi/v2/positionRisk with a 5-second TTL cache.

        Returns the raw list (all symbols including zero-size).
        Returns stale cache on error so a transient REST hiccup doesn't
        blank all position data.
        """
        now = time.monotonic()
        if self._pos_cache is not None and now - self._pos_cache_ts < _POS_TTL:
            return self._pos_cache
        data = await self._get("/fapi/v2/positionRisk")
        if isinstance(data, list):
            self._pos_cache    = data
            self._pos_cache_ts = now
            self._pos_last_authoritative = True
            return data
        log.warning("[%s] Binance positionRisk failed — using stale cache", self.source)
        self._pos_last_authoritative = False
        return self._pos_cache

    # ------------------------------------------------------------------ #
    # Protocol: current_positions                                          #
    # ------------------------------------------------------------------ #

    async def current_positions(self) -> dict[int, Position]:
        """Snapshot all non-zero USDT-M positions from positionRisk."""
        rows = await self._fetch_position_risk()
        if rows is None:
            return {}

        out: dict[int, Position] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                amt = _to_decimal(row.get("positionAmt", "0")) or Decimal(0)
                if amt == 0:
                    continue

                sym  = str(row.get("symbol", ""))
                position_side = str(row.get("positionSide", "BOTH")).upper()
                if position_side not in {"BOTH", "LONG", "SHORT"}:
                    position_side = "BOTH"
                mid  = self._position_market_id(sym, position_side)
                side = "long" if amt > 0 else "short"
                if position_side == "LONG":
                    side = "long"
                elif position_side == "SHORT":
                    side = "short"
                self._recent_symbols.add(sym)
                avg  = _to_decimal(row.get("entryPrice") or "0") or Decimal(0)
                unr  = _to_decimal(row.get("unrealizedProfit"))
                liq  = _to_decimal(row.get("liquidationPrice"))

                # Binance returns "0" for liquidation price when there is none
                if liq is not None and liq == 0:
                    liq = None

                out[mid] = Position(
                    market_id=mid,
                    market_symbol=self._market_symbol(mid),
                    side=side,
                    size=abs(amt),
                    avg_entry_price=avg,
                    source=self.source,
                    unrealized_pnl=unr,
                    liquidation_px=liq,
                    exchange="binance",
                    position_side=position_side,
                )
            except Exception:
                log.exception(
                    "[%s] could not parse Binance positionRisk row %r", self.source, row
                )

        log.debug("[%s] %d open Binance positions", self.source, len(out))
        return out

    async def current_positions_result(self) -> FetchResult[dict[int, Position]]:
        positions = await self.current_positions()
        if self._pos_last_authoritative:
            return FetchResult.success(positions)
        return FetchResult.stale(
            positions,
            error="Binance positionRisk unavailable; using cached snapshot",
        )

    # ------------------------------------------------------------------ #
    # Protocol: fetch_leverage                                             #
    # ------------------------------------------------------------------ #

    async def fetch_leverage(self, market_id: int) -> Optional[float]:
        """Leverage for *market_id* from the cached positionRisk."""
        rows = await self._fetch_position_risk()
        if not rows:
            return None

        full = self._full_symbol(market_id).upper()
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("symbol", "")).upper() == full:
                lev = _to_decimal(row.get("leverage"))
                return float(lev) if lev is not None else None
        return None

    # ------------------------------------------------------------------ #
    # Protocol: fetch_sl_tp                                               #
    # ------------------------------------------------------------------ #

    async def fetch_sl_tp(
        self, market_id: int
    ) -> tuple[Optional[Decimal], Optional[Decimal]]:
        """Return (stop_loss_price, take_profit_price) from open reduce-only orders.

        Looks for STOP_MARKET / STOP (SL) and TAKE_PROFIT_MARKET / TAKE_PROFIT (TP)
        orders on the given symbol. Returns (None, None) on any error.
        """
        full = self._full_symbol(market_id)
        data = await self._get("/fapi/v1/openOrders", {"symbol": full})
        if not isinstance(data, list):
            return None, None

        sl: Optional[Decimal] = None
        tp: Optional[Decimal] = None

        for order in data:
            if not isinstance(order, dict):
                continue
            otype = str(order.get("type", ""))
            stop_px = _to_decimal(order.get("stopPrice"))
            if stop_px is None or stop_px == 0:
                continue

            if otype in ("STOP_MARKET", "STOP"):
                sl = stop_px
            elif otype in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT"):
                tp = stop_px

        return sl, tp

    # ------------------------------------------------------------------ #
    # Protocol: fetch_open_orders (no-op stub)                           #
    # ------------------------------------------------------------------ #

    async def fetch_open_orders(self) -> list[OpenOrder]:
        """No-op stub — satisfies ExchangeClient Protocol.

        Returns every account-level resting order, including hedge-mode legs.
        """
        data = await self._get("/fapi/v1/openOrders")
        if not isinstance(data, list):
            return []
        out: list[OpenOrder] = []
        for row in data:
            if not isinstance(row, dict):
                continue
            try:
                symbol = str(row.get("symbol", "")).upper()
                ps = str(row.get("positionSide", "BOTH")).upper()
                if ps not in {"BOTH", "LONG", "SHORT"}:
                    ps = "BOTH"
                mid = self._position_market_id(symbol, ps)
                side = "long" if str(row.get("side", "")).upper() == "BUY" else "short"
                order_type = str(row.get("type", "")).upper()
                if order_type.startswith("STOP"):
                    kind = "stop_loss"
                elif order_type.startswith("TAKE_PROFIT"):
                    kind = "take_profit"
                else:
                    kind = "limit"
                price = _to_decimal(row.get("price"))
                trigger = _to_decimal(row.get("stopPrice"))
                size = _to_decimal(row.get("origQty"))
                out.append(
                    OpenOrder(
                        source=self.source,
                        market_id=mid,
                        market_symbol=self._market_symbol(mid),
                        side=side,
                        order_kind=kind,
                        price=price if price and price != 0 else None,
                        trigger_px=trigger if trigger and trigger != 0 else None,
                        size=size,
                        reduce_only=bool(row.get("reduceOnly", False)),
                        order_id=(
                            int(row["orderId"])
                            if row.get("orderId") is not None
                            else None
                        ),
                    )
                )
            except Exception:
                log.exception("[%s] could not parse Binance open order", self.source)
        return out

    # ------------------------------------------------------------------ #
    # Protocol: fetch_trades_since (REST safety net)                      #
    # ------------------------------------------------------------------ #

    async def fetch_trades_since(
        self, since_trade_id: Optional[int], limit: int = 100
    ) -> list[Trade]:
        """Fetch fills for all open positions since *since_trade_id* (ms epoch).

        Binance requires per-symbol queries; we iterate over currently-open
        symbols.  At startup (since_trade_id=None) returns [] — the anchor is
        set by the first WS fill and the reconciler handles silent closes.
        """
        if since_trade_id is None:
            # Cannot anchor without a reference timestamp; WS will prime it.
            return []

        # Query current positions, symbols seen during this process, and the
        # realized-income ledger. The income discovery step recovers symbols
        # that opened and fully closed while the bot was offline.
        rows = await self._fetch_position_risk()
        candidate_syms: set[str] = set(self._recent_symbols)
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            amt = _to_decimal(row.get("positionAmt", "0")) or Decimal(0)
            if amt != 0:
                sym = str(row.get("symbol", ""))
                if sym:
                    candidate_syms.add(sym)

        income = await self._get(
            "/fapi/v1/income",
            {
                "incomeType": "REALIZED_PNL",
                "startTime": since_trade_id + 1,
                "limit": 1000,
            },
        )
        if isinstance(income, list):
            for row in income:
                if isinstance(row, dict) and row.get("symbol"):
                    candidate_syms.add(str(row["symbol"]))

        if not candidate_syms:
            return []

        all_trades: list[Trade] = []
        for sym in candidate_syms:
            page_start = since_trade_id + 1
            for _page in range(20):
                data = await self._get(
                    "/fapi/v1/userTrades",
                    {"symbol": sym, "startTime": page_start, "limit": 1000},
                )
                if not isinstance(data, list):
                    log.debug(
                        "[%s] Binance userTrades for %s returned non-list",
                        self.source,
                        sym,
                    )
                    break
                page_times: list[int] = []
                for raw in data:
                    t = self._parse_rest_trade(raw, sym)
                    if t is not None:
                        all_trades.append(t)
                        page_times.append(int(t.timestamp.timestamp() * 1000))
                if len(data) < 1000 or not page_times:
                    break
                next_start = max(page_times) + 1
                if next_start <= page_start:
                    log.warning(
                        "[%s] Binance pagination stalled for %s", self.source, sym
                    )
                    break
                page_start = next_start
            else:
                log.warning(
                    "[%s] Binance recovery hit 20-page safety limit for %s; "
                    "history gap may remain",
                    self.source,
                    sym,
                )

        all_trades.sort(key=lambda x: (x.timestamp, x.market_id, x.trade_id))
        log.info(
            "[%s] Binance REST gap-fill: %d trade(s) since ts=%d across %d symbol(s)",
            self.source, len(all_trades), since_trade_id, len(candidate_syms),
        )
        return all_trades[-limit:] if limit else all_trades

    # ------------------------------------------------------------------ #
    # Protocol: stream_trades (WebSocket user data stream)                #
    # ------------------------------------------------------------------ #

    async def stream_trades(self) -> AsyncIterator[Trade]:
        """Async generator yielding Trade objects from the Binance user data stream.

        Flow per connection attempt:
          1. POST /fapi/v1/listenKey → get a fresh listen key (valid 60 min)
          2. Connect wss://fstream.binance.com/ws/<listenKey>
          3. Process ORDER_TRADE_UPDATE where o.x == "TRADE"
          4. Background task PUT /fapi/v1/listenKey every 25 min to extend
          5. On any error: cancel keepalive, exponential-backoff, new listen key
        """
        backoff = _BACKOFF_INITIAL

        while not self._closed:
            keepalive_task: Optional[asyncio.Task] = None
            try:
                # 1. Obtain a fresh listen key
                lk_data = await self._post("/fapi/v1/listenKey")
                if not isinstance(lk_data, dict) or "listenKey" not in lk_data:
                    log.error(
                        "[%s] Binance failed to obtain listenKey: %r — retrying in %.1fs",
                        self.source, lk_data, backoff,
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, _BACKOFF_MAX)
                    continue

                listen_key       = lk_data["listenKey"]
                self._listen_key = listen_key
                log.info("[%s] Binance listenKey obtained", self.source)

                # 2. Start keepalive background task
                keepalive_task = asyncio.get_running_loop().create_task(
                    self._keepalive_loop()
                )

                # 3. Connect WS and stream
                ws_url = f"{self._ws_base}/{listen_key}"
                log.info("[%s] Binance WS connecting to user data stream…", self.source)

                connect_kwargs: dict = {"ping_interval": 20, "ping_timeout": 30}
                if self._proxy_url:
                    connect_kwargs["proxy"] = self._proxy_url
                async with websockets.connect(ws_url, **connect_kwargs) as ws:
                    log.info("[%s] Binance WS connected", self.source)
                    backoff = _BACKOFF_INITIAL  # reset on successful connect

                    async for raw_msg in ws:
                        if self._closed:
                            return
                        try:
                            msg = json.loads(raw_msg)
                        except Exception:
                            continue

                        t = self._parse_ws_message(msg)
                        if t is not None:
                            yield t

            except Exception as e:
                log.warning(
                    "[%s] Binance WS error (%s: %s) — reconnecting in %.1fs",
                    self.source, type(e).__name__, e, backoff,
                )
            finally:
                # Always cancel the keepalive task when we exit the connection
                if keepalive_task is not None and not keepalive_task.done():
                    keepalive_task.cancel()
                    try:
                        await keepalive_task
                    except asyncio.CancelledError:
                        pass
                self._listen_key = None

            if self._closed:
                return

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)

    # ------------------------------------------------------------------ #
    # ListenKey keepalive                                                  #
    # ------------------------------------------------------------------ #

    async def _keepalive_loop(self) -> None:
        """PUT /fapi/v1/listenKey every 25 minutes to prevent expiry."""
        try:
            while not self._closed and self._listen_key:
                await asyncio.sleep(_KEEPALIVE_SEC)
                if not self._listen_key:
                    break
                result = await self._put("/fapi/v1/listenKey")
                if result is not None:
                    log.debug("[%s] Binance listenKey renewed", self.source)
                else:
                    log.warning(
                        "[%s] Binance listenKey renewal failed — connection will "
                        "reconnect automatically after expiry",
                        self.source,
                    )
                    break
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------ #
    # WS message parsing                                                   #
    # ------------------------------------------------------------------ #

    def _parse_ws_message(self, msg: dict) -> Optional[Trade]:
        """Parse an ORDER_TRADE_UPDATE WS message.  Returns None to skip."""
        if not isinstance(msg, dict):
            return None
        if msg.get("e") != "ORDER_TRADE_UPDATE":
            return None

        order = msg.get("o")
        if not isinstance(order, dict):
            return None

        # Only TRADE execution type generates a fill
        if order.get("x") != "TRADE":
            return None

        # One-way mode: position side must be "BOTH"
        ps = str(order.get("ps", "BOTH")).upper()
        if ps not in {"BOTH", "LONG", "SHORT"}:
            log.warning(
                "[%s] Binance fill with ps=%r (not BOTH) — hedge mode leak? skipping",
                self.source, ps,
            )
            return None

        try:
            symbol   = str(order.get("s", ""))
            mid      = self._position_market_id(symbol, ps)
            self._recent_symbols.add(symbol.upper())
            side_raw = str(order.get("S", "")).upper()
            side     = "long" if side_raw == "BUY" else "short"

            # L = last filled price, l = last filled quantity
            price = _to_decimal(order.get("L"))
            size  = _to_decimal(order.get("l"))

            if price is None or size is None or price == 0 or size == 0:
                log.debug(
                    "[%s] Binance WS fill skipped (zero price/size): %r",
                    self.source, order,
                )
                return None

            # T = transaction time in ms — used as synthetic global trade_id
            tx_time  = int(order.get("T", 0))
            native_id = str(order.get("t", "") or tx_time)
            rp       = _to_decimal(order.get("rp"))  # realized profit (USD)
            ts       = datetime.fromtimestamp(tx_time / 1000, tz=timezone.utc)

            return Trade(
                trade_id=int(native_id),
                timestamp=ts,
                market_id=mid,
                market_symbol=self._market_symbol(mid),
                side=side,
                size=abs(size),
                price=price,
                tx_hash=str(order.get("c", "")),   # client order id as hash proxy
                source=self.source,
                realized_pnl=rp if (rp is not None and rp != 0) else None,
                exchange="binance",
                native_trade_id=native_id,
                position_side=ps,
            )
        except Exception:
            log.exception("[%s] Binance WS fill parse error: %r", self.source, order)
            return None

    # ------------------------------------------------------------------ #
    # REST trade parsing (fetch_trades_since)                              #
    # ------------------------------------------------------------------ #

    def _parse_rest_trade(self, raw: dict, symbol: str) -> Optional[Trade]:
        """Parse a /fapi/v1/userTrades row.  Returns None to skip."""
        if not isinstance(raw, dict):
            return None
        try:
            trade_time = int(raw.get("time", 0))
            ps = str(raw.get("positionSide", "BOTH")).upper()
            if ps not in {"BOTH", "LONG", "SHORT"}:
                ps = "BOTH"
            mid = self._position_market_id(symbol, ps)
            self._recent_symbols.add(symbol.upper())

            # In USDT-M one-way mode: buyer=True means BUY side → long
            buyer = bool(raw.get("buyer", False))
            side  = "long" if buyer else "short"

            price = _to_decimal(raw.get("price"))
            size  = _to_decimal(raw.get("qty"))
            rp    = _to_decimal(raw.get("realizedPnl"))

            if price is None or size is None:
                return None

            ts = datetime.fromtimestamp(trade_time / 1000, tz=timezone.utc)

            native_id = str(raw.get("id", "") or trade_time)
            return Trade(
                trade_id=int(native_id),
                timestamp=ts,
                market_id=mid,
                market_symbol=self._market_symbol(mid),
                side=side,
                size=abs(size),
                price=price,
                tx_hash=str(raw.get("id", "")),
                source=self.source,
                realized_pnl=rp if (rp is not None and rp != 0) else None,
                exchange="binance",
                native_trade_id=native_id,
                position_side=ps,
            )
        except Exception:
            log.exception(
                "[%s] Binance REST trade parse error: %r", self.source, raw
            )
            return None
