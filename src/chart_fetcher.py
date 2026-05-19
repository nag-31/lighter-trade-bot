"""Chart image fetcher — chart-img.com TradingView snapshot.

Returns None on any failure so posts gracefully degrade to text-only.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from .config import ChartConfig

log = logging.getLogger(__name__)


class ChartFetcher:
    BASE = "https://api.chart-img.com/v2/tradingview/advanced-chart"

    def __init__(self, api_key: str, cfg: ChartConfig):
        self._key = api_key
        self._cfg = cfg
        self._enabled = bool(api_key) and cfg.enabled
        self._client = httpx.AsyncClient(timeout=15.0)
        if not self._enabled:
            log.info("chart fetcher disabled — posts will be text-only")

    async def close(self) -> None:
        await self._client.aclose()

    def _resolve_symbol(self, lighter_symbol: str) -> str:
        override = self._cfg.symbol_overrides.get(lighter_symbol)
        if override:
            return override
        return self._cfg.symbol_template.format(symbol=lighter_symbol.upper())

    async def get_chart(self, lighter_symbol: str) -> Optional[bytes]:
        if not self._enabled:
            return None
        body = {
            "symbol": self._resolve_symbol(lighter_symbol),
            "interval": self._cfg.interval,
            "width": self._cfg.width,
            "height": self._cfg.height,
            "theme": self._cfg.theme,
        }
        try:
            r = await self._client.post(
                self.BASE,
                json=body,
                headers={"x-api-key": self._key, "content-type": "application/json"},
            )
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
                return r.content
            log.warning("chart-img returned %d %s", r.status_code, r.text[:200])
            return None
        except Exception:
            log.exception("chart-img request failed")
            return None
