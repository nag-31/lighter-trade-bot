"""Rotating proxy pool with health-check, cooldown, and round-robin selection.

Supports socks5://, socks4://, and http:// proxy URLs.

SOCKS note
----------
httpx does not bundle SOCKS support — it requires the ``socksio`` extra
(``pip install httpx[socks]``).  When socksio is not installed, SOCKS proxies
will raise ``httpx.ProxyError`` on first use and be marked dead automatically.
http:// proxies work with plain httpx.

httpx ≥ 0.28 uses ``proxy=`` (singular); older versions used ``proxies=``.
This module detects the installed version and passes the correct argument.

Usage
-----
    pool = ProxyPool(
        ["socks5://1.2.3.4:1080", "http://5.6.7.8:3128"],
        test_url="https://fapi.binance.com/fapi/v1/ping",
        timeout=8.0,
    )
    proxy_url = await pool.get_working()  # None if all dead
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import httpx

log = logging.getLogger(__name__)

_COOLDOWN_SECONDS   = 300   # 5 minutes before retrying a dead proxy
_MAX_CHECK_PER_CALL = 4     # max proxies tested per get_working() call


# ---------------------------------------------------------------------------
# httpx compatibility: proxy= (≥0.28) vs proxies= (<0.28)
# ---------------------------------------------------------------------------

def _httpx_client_with_proxy(proxy_url: str, timeout: float) -> httpx.AsyncClient:
    """Build a short-lived AsyncClient routed through *proxy_url*.

    Handles the httpx 0.28 API change (``proxy=`` singular) vs earlier
    versions that used ``proxies=`` (dict).
    """
    import httpx as _httpx
    major, minor = (int(x) for x in _httpx.__version__.split(".")[:2])
    if (major, minor) >= (0, 28):
        return _httpx.AsyncClient(proxy=proxy_url, timeout=timeout)
    # Legacy: proxies takes a dict
    return _httpx.AsyncClient(proxies={"all://": proxy_url}, timeout=timeout)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ProxyState
# ---------------------------------------------------------------------------

@dataclass
class ProxyState:
    url: str
    status: str = "unknown"         # "ok" | "dead" | "unknown"
    last_ok: Optional[float] = None  # monotonic timestamp of last success
    last_error: str = ""
    latency_ms: Optional[int] = None
    _dead_since: Optional[float] = field(default=None, repr=False)

    def is_in_cooldown(self, now: float, cooldown_seconds: int = _COOLDOWN_SECONDS) -> bool:
        """True when proxy was marked dead and cooldown hasn't elapsed."""
        if self.status != "dead" or self._dead_since is None:
            return False
        return (now - self._dead_since) < cooldown_seconds


# ---------------------------------------------------------------------------
# Default checker (real network)
# ---------------------------------------------------------------------------

async def _default_checker(test_url: str, proxy_url: str, timeout: float) -> bool:
    """GET test_url through proxy_url.  Returns True only on HTTP 200."""
    try:
        async with _httpx_client_with_proxy(proxy_url, timeout) as client:
            r = await client.get(test_url)
            return r.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# ProxyPool
# ---------------------------------------------------------------------------

class ProxyPool:
    """Async rotating proxy pool over a list of proxy URLs.

    Parameters
    ----------
    proxies:
        List of proxy URLs, e.g. ``["socks5://1.2.3.4:1080", "http://..."]``.
    test_url:
        URL used by health checks to verify a proxy is working.
    timeout:
        Per-request timeout in seconds for health checks and regular requests.
    checker:
        Optional async callable ``(test_url, proxy_url) -> bool`` injected for
        testing.  When None, the real httpx-based checker is used.
    cooldown_seconds:
        How long a dead proxy waits before being re-tried.  Defaults to 300 s.
    """

    def __init__(
        self,
        proxies: list[str],
        test_url: str,
        timeout: float = 8.0,
        checker: Optional[Callable] = None,
        cooldown_seconds: int = _COOLDOWN_SECONDS,
    ) -> None:
        self._test_url        = test_url
        self._timeout         = timeout
        self._checker         = checker   # None → use _default_checker
        self._cooldown        = cooldown_seconds
        self._rr_index        = 0         # round-robin cursor

        # Deduplicate while preserving order
        seen: set[str] = set()
        unique: list[str] = []
        for u in proxies:
            if u and u not in seen:
                seen.add(u)
                unique.append(u)

        self._states: list[ProxyState] = [ProxyState(url=u) for u in unique]

        if self._states:
            log.info("ProxyPool: %d proxies loaded; test_url=%s", len(self._states), test_url)
        else:
            log.warning("ProxyPool: initialised with 0 proxies")

    # ------------------------------------------------------------------
    # Public mutation
    # ------------------------------------------------------------------

    def mark_dead(self, url: str, error: str = "") -> None:
        """Mark *url* dead and start its cooldown clock."""
        state = self._state_for(url)
        if state is None:
            return
        now = time.monotonic()
        state.status     = "dead"
        state.last_error = error
        state._dead_since = now
        log.warning("ProxyPool: marked dead %s — %s", url, error or "(no detail)")

    def mark_ok(self, url: str, latency_ms: int | None = None) -> None:
        """Mark *url* healthy."""
        state = self._state_for(url)
        if state is None:
            return
        state.status     = "ok"
        state.last_ok    = time.monotonic()
        state.last_error = ""
        if latency_ms is not None:
            state.latency_ms = latency_ms
        log.debug("ProxyPool: marked ok %s (latency=%s ms)", url, latency_ms)

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    async def health_check(self, url: str) -> bool:
        """Run a real connectivity probe through *url*.

        Returns True on success (HTTP 200) and calls mark_ok/mark_dead.
        """
        t0 = time.monotonic()
        try:
            if self._checker is not None:
                ok = await self._checker(self._test_url, url)
            else:
                ok = await _default_checker(self._test_url, url, self._timeout)
        except Exception as exc:
            self.mark_dead(url, str(exc))
            return False

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        if ok:
            self.mark_ok(url, latency_ms=elapsed_ms)
            log.info("ProxyPool: health_check OK %s (%d ms)", url, elapsed_ms)
        else:
            self.mark_dead(url, "health check returned non-200")
        return ok

    # ------------------------------------------------------------------
    # get_working
    # ------------------------------------------------------------------

    async def get_working(self) -> Optional[str]:
        """Return a working proxy URL (round-robin), or None if all dead.

        Algorithm:
          1. Walk states starting from the current round-robin index.
          2. Skip proxies that are dead AND still within cooldown.
          3. Return immediately for status="ok".
          4. For status="unknown" or dead-after-cooldown, run health_check.
             Cap the number of live probes per call to _MAX_CHECK_PER_CALL.
          5. Return None when the whole list is exhausted.
        """
        n = len(self._states)
        if n == 0:
            return None

        now      = time.monotonic()
        checked  = 0
        start    = self._rr_index % n

        for offset in range(n):
            idx   = (start + offset) % n
            state = self._states[idx]

            # Skip dead proxies still in cooldown
            if state.is_in_cooldown(now, self._cooldown):
                continue

            # Already known good — advance cursor and return
            if state.status == "ok":
                self._rr_index = (idx + 1) % n
                return state.url

            # Unknown or cooldown-expired dead — probe it (rate-limited)
            if checked >= _MAX_CHECK_PER_CALL:
                continue
            checked += 1

            ok = await self.health_check(state.url)
            if ok:
                self._rr_index = (idx + 1) % n
                return state.url

        # All candidates exhausted
        log.warning("ProxyPool: no working proxy found in pool of %d", n)
        return None

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def any_alive(self) -> bool:
        """True if at least one proxy has status="ok"."""
        return any(s.status == "ok" for s in self._states)

    @property
    def n_total(self) -> int:
        return len(self._states)

    @property
    def n_alive(self) -> int:
        return sum(1 for s in self._states if s.status == "ok")

    def status(self) -> list[dict]:
        """Snapshot of every proxy's state for health pages / logs."""
        now = time.monotonic()
        result = []
        for s in self._states:
            result.append({
                "url":        s.url,
                "status":     s.status,
                "latency_ms": s.latency_ms,
                "last_error": s.last_error,
                "in_cooldown": s.is_in_cooldown(now, self._cooldown),
            })
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _state_for(self, url: str) -> Optional[ProxyState]:
        for s in self._states:
            if s.url == url:
                return s
        log.debug("ProxyPool._state_for: unknown url %s", url)
        return None
