"""The Lighter WS /stream endpoint is geo-blocked from some regions (HTTP 400
"restricted jurisdiction"). stream_trades must detect that, warn ONCE, and back
off slowly (REST poll covers trades) instead of hammering every second."""

from __future__ import annotations

import pytest

import src.lighter_client as lc
from src.lighter_client import LighterClient, _WS_GEO_BACKOFF


class _FakeResp:
    status_code = 400


class _GeoBlock(Exception):
    """Mimics websockets' InvalidStatus on a 400 handshake rejection."""
    def __init__(self):
        super().__init__("server rejected WebSocket connection: HTTP 400")
        self.response = _FakeResp()


class _BreakLoop(Exception):
    pass


@pytest.mark.asyncio
async def test_ws_geoblock_warns_once_and_backs_off_slowly(monkeypatch):
    client = LighterClient(123, "https://rest", "wss://x/stream", source="L")

    def _raise_geoblock(*a, **k):
        raise _GeoBlock()

    monkeypatch.setattr(lc.websockets, "connect", _raise_geoblock)

    sleeps: list[float] = []

    async def _sleep(d):
        sleeps.append(d)
        raise _BreakLoop()  # break the `while True` after the first handling

    monkeypatch.setattr(lc.asyncio, "sleep", _sleep)

    agen = client.stream_trades()
    with pytest.raises(_BreakLoop):
        await agen.__anext__()

    # Slow geo-backoff, NOT the normal 1s reconnect
    assert sleeps == [_WS_GEO_BACKOFF]
    assert _WS_GEO_BACKOFF >= 60
    # Warning is armed so a persistent block won't re-log every retry
    assert client._ws_geo_warned is True


@pytest.mark.asyncio
async def test_non_geo_error_uses_fast_reconnect(monkeypatch):
    client = LighterClient(123, "https://rest", "wss://x/stream", source="L")

    def _raise_other(*a, **k):
        raise RuntimeError("connection reset")  # not a 400

    monkeypatch.setattr(lc.websockets, "connect", _raise_other)

    sleeps: list[float] = []

    async def _sleep(d):
        sleeps.append(d)
        raise _BreakLoop()

    monkeypatch.setattr(lc.asyncio, "sleep", _sleep)

    agen = client.stream_trades()
    with pytest.raises(_BreakLoop):
        await agen.__anext__()

    assert sleeps == [1.0]                 # normal fast reconnect
    assert client._ws_geo_warned is False  # not a geo-block


def test_ws_proxy_url_falls_back_to_general_proxy():
    # http:// avoids httpx's optional socksio dependency in CI.
    c1 = LighterClient(1, "r", "w", ws_proxy_url="http://ws:1")
    assert c1._ws_proxy_url == "http://ws:1"
    c2 = LighterClient(1, "r", "w", proxy_url="http://gen:2")
    assert c2._ws_proxy_url == "http://gen:2"  # falls back to general
    c3 = LighterClient(1, "r", "w")
    assert c3._ws_proxy_url is None            # direct
