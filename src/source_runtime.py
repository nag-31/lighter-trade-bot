"""Per-source runtime state and safe exchange result adaptation."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from typing import Optional

from .result import FetchResult, coerce_fetch_result
from .sources import Source
from .types import Position


class SourceRuntime:
    """Owns last-good state for one source.

    Clients may implement ``current_positions_result`` for native typed
    semantics. Legacy clients are supported, but exceptions are converted into
    non-authoritative results and the last-good snapshot is retained.
    """

    def __init__(self, source: Source) -> None:
        self.source = source
        self.last_good_positions: dict[int, Position] = {}
        self.last_success_at: Optional[datetime] = None
        self.created_at = datetime.now(timezone.utc)
        self.consecutive_failures = 0
        self.bootstrapped = False
        self._bootstrap_lock = asyncio.Lock()

    async def bootstrap(self) -> dict[int, Position]:
        """Bootstrap metadata and an authoritative snapshot exactly once."""
        if self.bootstrapped:
            return dict(self.last_good_positions)
        async with self._bootstrap_lock:
            if self.bootstrapped:
                return dict(self.last_good_positions)
            await self.source.client.bootstrap_markets()
            result = await self.fetch_positions()
            if not result.authoritative:
                raise RuntimeError(result.error or "position snapshot unavailable")
            self.bootstrapped = True
            return dict(result.value)

    async def fetch_positions(self) -> FetchResult[dict[int, Position]]:
        try:
            typed = getattr(self.source.client, "current_positions_result", None)
            raw = await typed() if typed is not None else await self.source.client.current_positions()
            result = coerce_fetch_result(raw)
        except Exception as exc:
            result = FetchResult.failure(
                self.last_good_positions,
                error=f"{type(exc).__name__}: {exc}",
            )

        if result.authoritative:
            normalized = {
                key: replace(
                    pos,
                    source_id=pos.source_id or self.source.id,
                    exchange=pos.exchange or self.source.exchange,
                )
                for key, pos in result.value.items()
            }
            result = FetchResult.success(normalized, detail=result.detail)
            self.last_good_positions = dict(normalized)
            self.last_success_at = datetime.now(timezone.utc)
            self.consecutive_failures = 0
            return result

        self.consecutive_failures += 1
        stale_since = self.last_success_at or datetime.now(timezone.utc)
        stale = {
            key: replace(pos, stale=True, stale_since=stale_since)
            for key, pos in self.last_good_positions.items()
        }
        return FetchResult.stale(
            stale,
            error=result.error,
            detail=result.detail or "retaining last authoritative position snapshot",
        )
