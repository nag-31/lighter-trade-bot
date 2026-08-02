"""Public futures mark-price adapters used by the Trade Journal.

These endpoints are read-only and do not require exchange credentials:

* Lighter ``orderBookDetails?market_id=255`` exposes every market's
  ``mark_price`` in one response.
* Hyperliquid ``metaAndAssetCtxs`` exposes ``markPx`` for each perp (including
  HIP-3 DEXes via ``dex``).
* Gate.io futures tickers expose ``mark_price`` and provide a broad fallback
  for symbols not covered by a native source.

The provider is deliberately best-effort. A public API outage leaves a mark
missing rather than fabricating a price or changing realized PnL.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
from typing import Any, Iterable
from urllib.request import Request, urlopen


log = logging.getLogger(__name__)

LIGHTER_DETAILS_URL = (
    "https://mainnet.zklighter.elliot.ai/api/v1/orderBookDetails?market_id=255"
)
HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"
GATE_TICKERS_URL = "https://api.gateio.ws/api/v4/futures/usdt/tickers"


@dataclass(frozen=True)
class PublicMark:
    price: float
    source: str
    captured_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _request_json(url: str, *, payload: dict[str, Any] | None = None) -> Any:
    timeout = max(2.0, float(os.getenv("PUBLIC_MARK_TIMEOUT_SECONDS", "8")))
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method="POST" if body else "GET")
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed public URLs
        return json.loads(response.read().decode("utf-8"))


def _normalized_symbol(value: Any) -> str:
    symbol = str(value or "").upper().strip()
    if ":" in symbol:
        symbol = symbol.split(":", 1)[1]
    for suffix in ("_USDT", "-USDT", "/USDT", "USDT", "_USD", "-USD", "USD"):
        if symbol.endswith(suffix):
            symbol = symbol[: -len(suffix)]
            break
    return symbol.replace("-", "_").replace("/", "_")


def _hyper_key(symbol: str) -> tuple[str, str]:
    text = str(symbol or "").upper().strip()
    if ":" in text:
        dex, coin = text.split(":", 1)
        return dex.lower(), coin
    return "", text


class PublicMarkProvider:
    """Fetch native public marks, then fall back to Gate futures tickers."""

    def fetch(self, positions: Iterable[dict[str, Any]]) -> dict[str, PublicMark]:
        rows = [row for row in positions if row.get("symbol")]
        if not rows:
            return {}
        captured_at = _now()
        result: dict[str, PublicMark] = {}

        if any(str(row.get("source") or "").lower().startswith("lighter") for row in rows):
            self._merge_lighter(result, rows, captured_at)
        hyper_rows = [
            row for row in rows
            if str(row.get("source") or "").upper().startswith("HL")
        ]
        if hyper_rows:
            self._merge_hyperliquid(result, hyper_rows, captured_at)

        unresolved = [row for row in rows if self._key(row) not in result]
        if unresolved:
            self._merge_gate(result, unresolved, captured_at)
        return result

    @staticmethod
    def _key(row: dict[str, Any]) -> str:
        source = str(row.get("source") or "unknown")
        symbol = str(row.get("symbol") or "").upper()
        return f"{source}:{symbol}:{str(row.get('side') or 'unknown').lower()}"

    def _put(
        self,
        result: dict[str, PublicMark],
        rows: Iterable[dict[str, Any]],
        price_by_symbol: dict[str, tuple[float, str]],
        captured_at: str,
    ) -> None:
        for row in rows:
            symbol = str(row.get("symbol") or "").upper()
            price_source = price_by_symbol.get(symbol)
            if price_source is None:
                price_source = price_by_symbol.get(_normalized_symbol(symbol))
            if price_source is None:
                continue
            price, source = price_source
            result[self._key(row)] = PublicMark(price, source, captured_at)

    def _merge_lighter(
        self,
        result: dict[str, PublicMark],
        rows: list[dict[str, Any]],
        captured_at: str,
    ) -> None:
        try:
            payload = _request_json(LIGHTER_DETAILS_URL)
            details = payload.get("order_book_details") if isinstance(payload, dict) else payload
            prices: dict[str, tuple[float, str]] = {}
            for item in details or ():
                if not isinstance(item, dict):
                    continue
                mark = _positive(item.get("mark_price"))
                if mark is not None:
                    prices[str(item.get("symbol") or "").upper()] = (
                        mark, "lighter_orderBookDetails.mark_price"
                    )
            self._put(
                result,
                [row for row in rows if str(row.get("source") or "").lower().startswith("lighter")],
                prices,
                captured_at,
            )
        except Exception as exc:
            log.warning("Lighter public mark request failed: %s", type(exc).__name__)

    def _merge_hyperliquid(
        self,
        result: dict[str, PublicMark],
        rows: list[dict[str, Any]],
        captured_at: str,
    ) -> None:
        dexes = {_hyper_key(str(row.get("symbol") or ""))[0] for row in rows}
        prices: dict[tuple[str, str], tuple[float, str]] = {}
        for dex in dexes:
            try:
                request = {"type": "metaAndAssetCtxs"}
                if dex:
                    request["dex"] = dex
                payload = _request_json(HYPERLIQUID_INFO_URL, payload=request)
                if not isinstance(payload, list) or len(payload) < 2:
                    continue
                universe = (payload[0] or {}).get("universe") or {}
                contexts = payload[1] or []
                for market, context in zip(universe, contexts):
                    if not isinstance(market, dict) or not isinstance(context, dict):
                        continue
                    mark = _positive(context.get("markPx"))
                    coin = str(market.get("name") or "").upper()
                    prefix = f"{dex.upper()}:"
                    if dex and coin.startswith(prefix):
                        coin = coin[len(prefix):]
                    if mark is not None and coin:
                        prices[(dex, coin)] = (
                            mark, "hyperliquid_metaAndAssetCtxs.markPx"
                        )
            except Exception as exc:
                log.warning(
                    "Hyperliquid public mark request failed dex=%s: %s",
                    dex or "default", type(exc).__name__,
                )
        for row in rows:
            price_source = prices.get(_hyper_key(str(row.get("symbol") or "")))
            if price_source is None:
                continue
            price, source = price_source
            result[self._key(row)] = PublicMark(price, source, captured_at)

    def _merge_gate(
        self,
        result: dict[str, PublicMark],
        rows: list[dict[str, Any]],
        captured_at: str,
    ) -> None:
        try:
            payload = _request_json(GATE_TICKERS_URL)
            prices: dict[str, tuple[float, str]] = {}
            for item in payload or ():
                if not isinstance(item, dict):
                    continue
                mark = _positive(item.get("mark_price"))
                source = "gate_futures_tickers.mark_price"
                if mark is None:
                    mark = _positive(item.get("index_price"))
                    source = "gate_futures_tickers.index_price"
                if mark is None:
                    continue
                prices[_normalized_symbol(item.get("contract"))] = (mark, source)
            self._put(result, rows, prices, captured_at)
        except Exception as exc:
            log.warning("Gate public mark request failed: %s", type(exc).__name__)
