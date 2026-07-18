from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

import httpx

from ..cards.renderer import render_round_trip_card
from ..core.models import RoundTrip


def _money(v: Decimal) -> str:
    sign = "+" if v >= 0 else "-"
    return f"{sign}${abs(v):,.2f}"


def _pct(v: Decimal | None) -> str:
    if v is None:
        return "n/a"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}%"


def format_round_trip_alert(rt: RoundTrip) -> str:
    return (
        f"PnL card · CLOSE {rt.direction.upper()} {rt.symbol} · "
        f"{_money(rt.net_pnl)} · Return on cost {_pct(rt.return_on_cost)} "
        f"[{rt.source}]"
    )


@dataclass
class AlertDeduper:
    ttl_seconds: float = 300.0
    max_per_minute: int = 6
    _sent: dict[str, float] = field(default_factory=dict)
    _minute_window: list[float] = field(default_factory=list)

    def should_send(self, key: str, *, now: float | None = None) -> bool:
        t = time.time() if now is None else now
        self._sent = {k: v for k, v in self._sent.items() if t - v < self.ttl_seconds}
        self._minute_window = [v for v in self._minute_window if t - v < 60.0]
        if key in self._sent:
            return False
        if len(self._minute_window) >= self.max_per_minute:
            return False
        self._sent[key] = t
        self._minute_window.append(t)
        return True


class TelegramTransport(Protocol):
    async def send_photo(self, *, caption: str, png_bytes: bytes) -> None:
        ...


class HttpxTelegramTransport:
    def __init__(self, *, bot_token: str, chat_id: str, timeout: float = 20.0):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout

    async def send_photo(self, *, caption: str, png_bytes: bytes) -> None:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendPhoto"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                url,
                data={"chat_id": self.chat_id, "caption": caption},
                files={"photo": ("pnl-card.png", png_bytes, "image/png")},
            )
            response.raise_for_status()


class RoundTripTelegramAlerter:
    def __init__(self, transport: TelegramTransport, deduper: AlertDeduper | None = None):
        self.transport = transport
        self.deduper = deduper or AlertDeduper()

    async def alert_closed_round_trip(self, rt: RoundTrip) -> bool:
        key = self._key(rt)
        if not self.deduper.should_send(key):
            return False
        await self.transport.send_photo(
            caption=format_round_trip_alert(rt),
            png_bytes=render_round_trip_card(rt),
        )
        return True

    @staticmethod
    def _key(rt: RoundTrip) -> str:
        material = "|".join([rt.id, ",".join(rt.exit_fill_ids), str(rt.net_pnl)])
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

