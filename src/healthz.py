import time
from aiohttp import web


class Healthz:
    """Tiny aiohttp app. /healthz returns 200 only if main loop ticked recently."""

    def __init__(self, stale_after_seconds: int = 60):
        self._last_tick = time.monotonic()
        self._stale = stale_after_seconds

    def mark_alive(self) -> None:
        self._last_tick = time.monotonic()

    async def _handler(self, _request: web.Request) -> web.Response:
        age = time.monotonic() - self._last_tick
        if age > self._stale:
            return web.Response(status=503, text=f"stale ({age:.0f}s)")
        return web.Response(status=200, text="ok")

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/healthz", self._handler)
        return app
