"""Plug-and-play source layer.

A "source" is one tracked pool or wallet. Add an entry to config.yaml, restart,
and the dashboard picks it up — no code change. Each source pairs an exchange
client with its own PositionTracker so market_id keys never collide.

config.yaml shape:

    sources:
      - type: lighter
        name: "My NK pool"
        pool_id: 281474976684763
      - type: hyperliquid
        name: "Whale A"
        address: "0x..."
        min_notional_usd: 1000   # optional per-source override
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import AsyncIterator, Optional, Protocol

import yaml

from .binance_client import BinanceClient
from .hyperliquid_client import HyperliquidClient
from .lighter_client import LighterClient
from .position_tracker import PositionTracker
from .types import Position, Trade

log = logging.getLogger(__name__)

LIGHTER_REST_BASE = "https://mainnet.zklighter.elliot.ai/api/v1"
LIGHTER_WS_URL = "wss://mainnet.zklighter.elliot.ai/stream"
DEFAULT_MIN_NOTIONAL = Decimal("1000")


class ExchangeClient(Protocol):
    """The interface the dashboard depends on. LighterClient and
    HyperliquidClient both satisfy this via duck typing."""

    source: str

    async def bootstrap_markets(self) -> dict[int, str]: ...
    async def current_positions(self) -> dict[int, Position]: ...
    async def fetch_trades_since(
        self, since_trade_id: Optional[int], limit: int = 100
    ) -> list[Trade]: ...
    def stream_trades(self) -> AsyncIterator[Trade]: ...
    async def fetch_leverage(self, market_id: int) -> Optional[float]: ...
    async def fetch_sl_tp(self, market_id: int) -> tuple[Optional[Decimal], Optional[Decimal]]: ...
    async def close(self) -> None: ...


@dataclass
class Source:
    """One tracked pool/wallet plus its live tracking state."""

    id: str
    name: str
    client: ExchangeClient
    tracker: PositionTracker
    url: str
    min_notional: Decimal
    last_trade_id: Optional[int] = None
    # Set-based dedup: protects against WS replay and REST/WS overlap.
    # Using a set catches duplicates with any tid, not just the last one.
    seen_tids: set[int] = field(default_factory=set)


def _proxy_url() -> Optional[str]:
    """Return the SOCKS5 proxy URL from env, or None if not set.

    Set SOCKS_PROXY_URL=socks5h://host:1080 in .env to route Lighter and
    Binance traffic through a proxy in a non-restricted jurisdiction.
    Hyperliquid does NOT use this proxy (it is not geo-blocked).
    Use socks5h:// (not socks5://) so DNS resolves on the proxy side.
    """
    url = os.getenv("SOCKS_PROXY_URL", "").strip()
    return url if url else None


def _build_source(raw: dict) -> Optional[Source]:
    stype = str(raw.get("type", "")).lower().strip()
    name = str(raw.get("name", "")).strip()
    if not name:
        log.warning("source entry missing 'name' — skipping: %r", raw)
        return None

    min_notional = (
        Decimal(str(raw["min_notional_usd"]))
        if raw.get("min_notional_usd") is not None
        else DEFAULT_MIN_NOTIONAL
    )

    if stype == "lighter":
        pool_id = raw.get("pool_id")
        if pool_id is None:
            log.warning("lighter source '%s' missing 'pool_id' — skipping", name)
            return None
        pool_id = int(pool_id)
        client = LighterClient(
            pool_id, LIGHTER_REST_BASE, LIGHTER_WS_URL,
            source=name, proxy_url=_proxy_url(),
        )
        return Source(
            id=f"lighter:{pool_id}",
            name=name,
            client=client,
            tracker=PositionTracker(source=name),
            url=f"https://app.lighter.xyz/public-pools/{pool_id}",
            min_notional=min_notional,
        )

    if stype == "hyperliquid":
        # Address is loaded from the HL_ADDRESS env var, not from config.yaml,
        # to keep the wallet address out of version control.
        address = os.getenv("HL_ADDRESS", "").strip()
        if not address:
            log.warning(
                "hyperliquid source '%s': HL_ADDRESS env var is missing or empty — "
                "skipping HL source (Lighter continues unaffected)",
                name,
            )
            return None
        # footer_url is an optional public website to append to HL alerts.
        # The HL explorer URL is intentionally NOT used here — it exposes the wallet address.
        footer_url = str(raw.get("footer_url", "")).strip()
        client = HyperliquidClient(address, source=name)
        return Source(
            id=f"hyperliquid:{address.lower()}",
            name=name,
            client=client,
            tracker=PositionTracker(source=name),
            url=footer_url,   # wallet address is NEVER put here; only an explicit public footer_url
            min_notional=min_notional,
        )

    if stype == "binance":
        # API key + secret loaded from env vars — never put credentials in config.yaml.
        api_key    = os.getenv("BINANCE_API_KEY", "").strip()
        api_secret = os.getenv("BINANCE_API_SECRET", "").strip()
        if not api_key or not api_secret:
            log.warning(
                "binance source '%s': BINANCE_API_KEY and/or BINANCE_API_SECRET "
                "env vars are missing or empty — skipping Binance source "
                "(other sources continue unaffected)",
                name,
            )
            return None
        footer_url = str(raw.get("footer_url", "")).strip()
        client = BinanceClient(api_key, api_secret, source=name, proxy_url=_proxy_url())
        return Source(
            id=f"binance:{name.lower().replace(' ', '_')}",
            name=name,
            client=client,
            tracker=PositionTracker(source=name),
            url=footer_url,  # API keys/account info are NEVER put here; only an explicit public footer_url
            min_notional=min_notional,
        )

    log.warning("unknown source type %r for '%s' — skipping", stype, name)
    return None


def load_sources(path: str | Path = "config.yaml") -> list[Source]:
    """Parse config.yaml and build a Source per entry. Raises if no valid source."""
    p = Path(path)
    if not p.exists():
        raise RuntimeError(f"config file not found: {p}")
    with open(p, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    raw_sources = cfg.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise RuntimeError(f"{p} has no 'sources' list")

    sources: list[Source] = []
    seen_ids: set[str] = set()
    for raw in raw_sources:
        if not isinstance(raw, dict):
            continue
        src = _build_source(raw)
        if src is None:
            continue
        if src.id in seen_ids:
            log.warning("duplicate source %s — skipping", src.id)
            continue
        seen_ids.add(src.id)
        sources.append(src)

    if not sources:
        raise RuntimeError(f"no valid sources in {p}")
    log.info("loaded %d source(s): %s", len(sources), ", ".join(s.name for s in sources))
    return sources
