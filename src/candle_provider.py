"""Best-effort OHLC candle providers for lifecycle charts.

The chart domain stays exchange-neutral.  This adapter translates the public
Hyperliquid and Lighter candle APIs into ``architecture_v2.domain.charts.Candle``
objects, returning an empty tuple on transport/schema failure so Telegram
alerts can still use the deterministic Pillow execution-only fallback.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Sequence

from architecture_v2.domain.charts import Candle, select_interval_seconds

log = logging.getLogger(__name__)

_RESOLUTIONS = {
    60: "1m",
    180: "3m",
    300: "5m",
    900: "15m",
    1800: "30m",
    3600: "1h",
    7200: "2h",
    14400: "4h",
    28800: "8h",
    43200: "12h",
    86400: "1d",
}

# A trade pinned to the first/last pixel does not read like an exchange chart.
# Request a small amount of market context around the lifecycle while keeping
# the lifecycle timestamps themselves unchanged in the chart spec.
_CONTEXT_BEFORE_BARS = 12
_CONTEXT_AFTER_BARS = 8


def _timestamp(value: Any) -> datetime:
    raw = float(value)
    # Exchange APIs use either milliseconds or seconds.
    if raw > 10_000_000_000:
        raw /= 1000
    return datetime.fromtimestamp(raw, tz=timezone.utc)


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _candle(row: Any) -> Candle | None:
    """Normalize abbreviated dict rows or Binance-style OHLC arrays."""
    try:
        if isinstance(row, (list, tuple)):
            timestamp, opened, high, low, closed, volume = row[:6]
        else:
            timestamp = row.get("t", row.get("timestamp", row.get("time")))
            opened = row.get("o", row.get("open"))
            high = row.get("h", row.get("high"))
            low = row.get("l", row.get("low"))
            closed = row.get("c", row.get("close"))
            volume = row.get("v", row.get("volume"))
        if None in (timestamp, opened, high, low, closed):
            return None
        return Candle(
            opened_at=_timestamp(timestamp),
            open=_decimal(opened),
            high=_decimal(high),
            low=_decimal(low),
            close=_decimal(closed),
            volume=None if volume is None else _decimal(volume),
        )
    except (AttributeError, IndexError, TypeError, ValueError, ArithmeticError):
        return None


def _normalize(rows: Any) -> tuple[Candle, ...]:
    if isinstance(rows, dict):
        rows = rows.get("candles") or rows.get("data") or rows.get("rows") or []
    if not isinstance(rows, (list, tuple)):
        return ()
    dedup: dict[datetime, Candle] = {}
    for row in rows:
        candle = _candle(row)
        if candle is not None:
            dedup[candle.opened_at] = candle
    return tuple(dedup[key] for key in sorted(dedup))


class CandleProvider:
    """Fetch public candles from the exchange client already owned by a source."""

    def __init__(self, *, timeout_seconds: float = 8.0, max_candles: int = 500):
        self.timeout_seconds = timeout_seconds
        self.max_candles = max(1, min(int(max_candles), 500))

    async def fetch_for_lifecycle(
        self,
        source: Any,
        *,
        market_id: int,
        market_symbol: str,
        opened_at: datetime,
        closed_at: datetime,
    ) -> tuple[tuple[Candle, ...], str]:
        interval_seconds = select_interval_seconds(opened_at, closed_at)
        resolution = _RESOLUTIONS.get(interval_seconds, "1m")
        context_before = interval_seconds * _CONTEXT_BEFORE_BARS
        context_after = interval_seconds * _CONTEXT_AFTER_BARS
        start_ms = int((opened_at.timestamp() - context_before) * 1000)
        end_ms = int((closed_at.timestamp() + context_after) * 1000)
        try:
            if source.exchange == "hyperliquid":
                rows = await asyncio.wait_for(
                    asyncio.to_thread(
                        source.client._info.candles_snapshot,
                        market_symbol,
                        resolution,
                        start_ms,
                        end_ms,
                    ),
                    timeout=self.timeout_seconds,
                )
                return _normalize(rows), f"hyperliquid:candleSnapshot:{resolution}"

            if source.exchange == "lighter":
                client = source.client
                response = await asyncio.wait_for(
                    client._http.get(
                        f"{client._rest_base}/candles",
                        params={
                            "market_id": market_id,
                            "resolution": resolution,
                            "start_timestamp": start_ms,
                            "end_timestamp": end_ms,
                            "count_back": self.max_candles,
                            "set_timestamp_to_end": "true",
                        },
                    ),
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                return _normalize(response.json()), f"lighter:candles:{resolution}"

            if source.exchange == "binance":
                client = source.client
                symbol = client._id_to_full.get(market_id, f"{market_symbol.upper()}USDT")
                response = await asyncio.wait_for(
                    client._request(
                        "GET",
                        "/fapi/v1/klines",
                        params={
                            "symbol": symbol,
                            "interval": resolution,
                            "startTime": start_ms,
                            "endTime": end_ms,
                            "limit": self.max_candles,
                        },
                    ),
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                return _normalize(response.json()), f"binance:klines:{resolution}"
        except Exception as exc:
            log.warning(
                "candle fetch failed for %s/%s (%s)",
                getattr(source, "name", "source"),
                market_symbol,
                type(exc).__name__,
            )
        return (), "execution-only"
