from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx

from ..core.models import RawFill
from .common import dec, dt_from_iso, dt_from_ms


def parse_trade(
    raw: dict[str, Any],
    *,
    source: str = "Lighter",
    account: str = "",
    standard_account: bool = True,
) -> RawFill:
    raw_side = raw.get("side")
    if raw_side is None and raw.get("sign") is not None:
        raw_side = "buy" if int(raw["sign"]) > 0 else "sell"
    side = "buy" if str(raw_side).lower() in {"buy", "bid", "b", "long"} else "sell"
    ts_raw = raw.get("timestamp") or raw.get("time") or raw.get("created_at")
    timestamp = dt_from_ms(ts_raw) if str(ts_raw).isdigit() else dt_from_iso(str(ts_raw))
    fee = Decimal("0") if standard_account else (dec(raw.get("fee"), Decimal("0")) or Decimal("0"))
    return RawFill(
        source=source,
        account=account,
        symbol=str(raw.get("symbol") or raw.get("market_symbol") or raw.get("market_id")).upper(),
        fill_id=str(raw.get("trade_id") or raw.get("id")),
        order_id=str(raw.get("order_id")) if raw.get("order_id") is not None else None,
        timestamp=timestamp,
        side=side,
        qty=abs(dec(raw.get("size") or raw.get("qty"), Decimal("0")) or Decimal("0")),
        price=dec(raw.get("price"), Decimal("0")) or Decimal("0"),
        fee=fee,
        fee_token="USDC",
        exchange_realized_pnl=dec(raw.get("realized_pnl")),
        funding=dec(raw.get("funding")),
        sequence=int(raw.get("trade_id") or raw.get("id") or 0),
        raw=dict(raw),
    )


async def fetch_account_trades(
    account_index: str | int,
    *,
    source: str = "Lighter",
    rest_base: str = "https://mainnet.zklighter.elliot.ai/api/v1",
    limit: int = 100,
    standard_account: bool = True,
) -> list[RawFill]:
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(
            f"{rest_base}/trades",
            params={
                "account_index": str(account_index),
                "sort_by": "trade_id",
                "sort_dir": "desc",
                "limit": str(limit),
            },
        )
        response.raise_for_status()
        payload = response.json()
    rows = payload.get("trades", payload) if isinstance(payload, dict) else payload
    fills = [
        parse_trade(row, source=source, account=str(account_index), standard_account=standard_account)
        for row in rows
    ]
    fills.sort(key=lambda f: (f.timestamp, f.sequence or 0, f.fill_id))
    return fills
