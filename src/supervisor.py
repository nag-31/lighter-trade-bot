"""Crash isolation and bounded restart backoff for source tasks."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from .health import HealthRegistry

log = logging.getLogger(__name__)


async def supervise(
    component: str,
    task_factory: Callable[[], Awaitable[None]],
    health: HealthRegistry,
    *,
    initial_backoff: float = 1.0,
    max_backoff: float = 60.0,
) -> None:
    """Restart one failed task without allowing it to stop sibling sources."""
    backoff = initial_backoff
    while True:
        try:
            health.mark_up(component, detail="task running")
            await task_factory()
            health.mark_degraded(component, "task exited unexpectedly; restarting")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("%s failed; restarting in %.1fs", component, backoff)
            health.mark_degraded(
                component,
                f"{type(exc).__name__}: {exc}",
                detail=f"restart in {backoff:.1f}s",
            )
        await asyncio.sleep(backoff)
        backoff = min(max_backoff, backoff * 2)
