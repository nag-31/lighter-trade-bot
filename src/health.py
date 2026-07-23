"""Component health / status registry.

A lightweight, dependency-free registry for tracking the up/down/degraded
state of every critical bot component (exchange sources, Telegram, database,
WebSocket feeds, etc.).

Designed for a single asyncio event loop — no locks needed.

Usage
-----
    health = HealthRegistry(started_at_iso=datetime.now(timezone.utc).isoformat())
    health.mark_up("database")
    health.mark_down("source:Binance", error="proxy timeout")
    health.mark_degraded("source:HL", error="WS reconnect loop")
    health.mark_disabled("source:Binance", detail="not configured")
    snap = health.snapshot()   # {"ok": bool, "components": [...], ...}
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


_VALID_STATUSES = frozenset({"up", "degraded", "down", "disabled"})


class HealthRegistry:
    """Tracks status for a set of named components.

    Parameters
    ----------
    started_at_iso:
        ISO-8601 timestamp of when the bot process started (recorded in
        every snapshot for uptime calculation).
    """

    def __init__(self, started_at_iso: str) -> None:
        self._started_at: str = started_at_iso
        # component name -> record dict
        self._components: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Public mutation helpers
    # ------------------------------------------------------------------

    def set(
        self,
        component: str,
        status: str,
        *,
        detail: str = "",
        error: str = "",
        ok_ts: Optional[str] = None,
    ) -> None:
        """Set arbitrary status for a component.

        Parameters
        ----------
        component:
            Free-form name, e.g. ``"source:Binance"``, ``"telegram"``.
        status:
            One of ``"up"`` | ``"degraded"`` | ``"down"`` | ``"disabled"``.
        detail:
            Optional human-readable detail string.
        error:
            Error message; shown in the /health page when non-empty.
        ok_ts:
            ISO timestamp of the last successful operation.  When ``None``
            and the new status is ``"up"``, ``datetime.now(timezone.utc)``
            is used automatically.
        """
        if status not in _VALID_STATUSES:
            raise ValueError(f"Invalid status {status!r}; must be one of {sorted(_VALID_STATUSES)}")

        existing = self._components.get(component, {})
        # Carry forward last_ok if transitioning away from "up"
        last_ok = existing.get("last_ok")

        if status == "up":
            last_ok = ok_ts or datetime.now(timezone.utc).isoformat()

        self._components[component] = {
            "component": component,
            "status": status,
            "detail": detail,
            "error": error if status != "up" else "",
            "last_ok": last_ok,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def mark_up(self, component: str, detail: str = "", ok_ts: Optional[str] = None) -> None:
        """Mark a component as healthy (status="up")."""
        self.set(component, "up", detail=detail, ok_ts=ok_ts)

    def mark_down(self, component: str, error: str, detail: str = "") -> None:
        """Mark a component as fully unavailable (status="down")."""
        self.set(component, "down", error=error, detail=detail)

    def mark_degraded(self, component: str, error: str, detail: str = "") -> None:
        """Mark a component as partially unavailable (status="degraded")."""
        self.set(component, "degraded", error=error, detail=detail)

    def mark_disabled(self, component: str, detail: str = "") -> None:
        """Mark a component as intentionally off, e.g. not configured (status="disabled")."""
        self.set(component, "disabled", detail=detail)

    def remove(self, component: str) -> None:
        """Remove a component that no longer exists after config reload."""
        self._components.pop(component, None)

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        """Return the current health state as a JSON-serializable dict.

        The ``ok`` field is ``True`` only when NO component has status
        ``"down"`` or ``"degraded"`` (``"disabled"`` is intentionally
        excluded from the health signal).

        Returns
        -------
        dict with keys:
            - ``started_at``: ISO timestamp the registry was created.
            - ``now``: ISO timestamp of this snapshot.
            - ``ok``: ``bool`` — ``False`` if any component is down/degraded.
            - ``components``: list of component dicts (sorted by name).
        """
        problem_statuses = {"down", "degraded"}
        ok = not any(
            rec["status"] in problem_statuses
            for rec in self._components.values()
        )
        return {
            "started_at": self._started_at,
            "now": datetime.now(timezone.utc).isoformat(),
            "ok": ok,
            "live": True,
            "ready": ok,
            "components": sorted(
                list(self._components.values()),
                key=lambda r: r["component"],
            ),
        }
