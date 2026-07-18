from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx

from ..core.models import RawFill
from .common import dec, dt_from_ms


def parse_user_fill(raw: dict[str, Any], *, source: str = "Hyperliquid", account: str = "") -> RawFill:
    side = "buy" if str(raw.get("side", "")).upper() == "B" else "sell"
    fee = dec(raw.get("fee"), Decimal("0")) or Decimal("0")
    return RawFill(
        source=source,
        account=account,
        symbol=str(raw["coin"]).upper(),
        fill_id=str(raw["tid"]),
        order_id=str(raw["oid"]) if raw.get("oid") is not None else None,
        timestamp=dt_from_ms(raw["time"]),
        side=side,
        qty=abs(dec(raw.get("sz"), Decimal("0")) or Decimal("0")),
        price=dec(raw.get("px"), Decimal("0")) or Decimal("0"),
        fee=fee,
        fee_token=str(raw.get("feeToken") or "USDC").strip(),
        exchange_realized_pnl=dec(raw.get("closedPnl")),
        funding=dec(raw.get("funding")),
        sequence=int(raw["tid"]),
        raw=dict(raw),
    )


async def fetch_user_fills(
    address: str,
    *,
    source: str = "Hyperliquid",
    account: str | None = None,
    base_url: str = "https://api.hyperliquid.xyz/info",
    limit: int | None = None,
) -> list[RawFill]:
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(base_url, json={"type": "userFills", "user": address})
        response.raise_for_status()
        rows = response.json()
    fills = [parse_user_fill(row, source=source, account=account or address) for row in rows]
    fills.sort(key=lambda f: (f.timestamp, f.sequence or 0, f.fill_id))
    return fills[-limit:] if limit else fills
