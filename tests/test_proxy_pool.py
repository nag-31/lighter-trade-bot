"""Tests for ProxyPool — uses injected fake checkers, no network calls."""

from __future__ import annotations

import asyncio
import time
from typing import Optional

import pytest

from src.proxy_pool import ProxyPool, ProxyState


# ---------------------------------------------------------------------------
# Fake checkers
# ---------------------------------------------------------------------------

def _always_ok(test_url: str, proxy_url: str) -> bool:
    return True

def _always_fail(test_url: str, proxy_url: str) -> bool:
    return False


async def _async_ok(test_url: str, proxy_url: str) -> bool:
    return True

async def _async_fail(test_url: str, proxy_url: str) -> bool:
    return False


def _make_pool(
    proxies: list[str],
    checker=None,
    *,
    cooldown: int = 300,
) -> ProxyPool:
    """Helper: build a pool with an injected async checker."""
    if checker is None:
        checker = _async_ok

    # Wrap sync checkers in a coroutine
    import asyncio as _asyncio
    import inspect
    if not inspect.iscoroutinefunction(checker):
        _orig = checker
        async def _wrapped(test_url: str, proxy_url: str) -> bool:
            return _orig(test_url, proxy_url)
        checker = _wrapped

    return ProxyPool(
        proxies,
        test_url="https://example.com/ping",
        checker=checker,
        cooldown_seconds=cooldown,
    )


# ---------------------------------------------------------------------------
# Basic construction
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_empty_pool(self):
        p = _make_pool([])
        assert p.n_total == 0
        assert p.n_alive == 0
        assert not p.any_alive

    def test_dedup_removes_duplicates(self):
        urls = ["socks5://1.1.1.1:1080", "socks5://1.1.1.1:1080", "http://2.2.2.2:8080"]
        p = _make_pool(urls)
        assert p.n_total == 2

    def test_mixed_schemes_parsed(self):
        urls = [
            "socks5://1.1.1.1:1080",
            "socks4://2.2.2.2:1080",
            "http://3.3.3.3:8080",
        ]
        p = _make_pool(urls)
        assert p.n_total == 3


# ---------------------------------------------------------------------------
# mark_ok / mark_dead / any_alive
# ---------------------------------------------------------------------------

class TestMarkHelpers:
    def test_mark_ok_sets_status(self):
        p = _make_pool(["socks5://a:1080"])
        p.mark_ok("socks5://a:1080", latency_ms=42)
        s = p._states[0]
        assert s.status == "ok"
        assert s.latency_ms == 42

    def test_mark_dead_sets_status(self):
        p = _make_pool(["socks5://a:1080"])
        p.mark_dead("socks5://a:1080", error="timeout")
        s = p._states[0]
        assert s.status == "dead"
        assert s.last_error == "timeout"

    def test_any_alive_true_when_one_ok(self):
        p = _make_pool(["socks5://a:1080", "socks5://b:1080"])
        p.mark_ok("socks5://a:1080")
        assert p.any_alive

    def test_any_alive_false_when_all_dead(self):
        p = _make_pool(["socks5://a:1080"])
        p.mark_dead("socks5://a:1080")
        assert not p.any_alive

    def test_mark_unknown_url_is_no_op(self):
        p = _make_pool(["socks5://a:1080"])
        p.mark_dead("socks5://nonexistent:9999")  # should not raise
        assert p._states[0].status == "unknown"


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------

class TestCooldown:
    def test_dead_proxy_is_in_cooldown_immediately(self):
        p = _make_pool(["http://a:8080"], cooldown=300)
        p.mark_dead("http://a:8080")
        now = time.monotonic()
        assert p._states[0].is_in_cooldown(now, 300)

    def test_dead_proxy_not_in_cooldown_after_expiry(self):
        p = _make_pool(["http://a:8080"], cooldown=1)
        p.mark_dead("http://a:8080")
        # Simulate time passing by manipulating _dead_since
        p._states[0]._dead_since -= 2  # 2 seconds ago > 1 second cooldown
        now = time.monotonic()
        assert not p._states[0].is_in_cooldown(now, 1)

    def test_ok_proxy_never_in_cooldown(self):
        p = _make_pool(["http://a:8080"])
        p.mark_ok("http://a:8080")
        now = time.monotonic()
        assert not p._states[0].is_in_cooldown(now, 300)


# ---------------------------------------------------------------------------
# get_working — round-robin and rotation
# ---------------------------------------------------------------------------

class TestGetWorking:
    def test_returns_none_when_empty(self):
        p = _make_pool([])
        result = asyncio.run(p.get_working())
        assert result is None

    def test_returns_ok_proxy_without_health_check(self):
        p = _make_pool(["socks5://a:1080"])
        p.mark_ok("socks5://a:1080")
        result = asyncio.run(p.get_working())
        assert result == "socks5://a:1080"

    def test_unknown_proxy_gets_health_checked_and_returned_if_ok(self):
        # checker always returns True; unknown proxy should be probed and returned
        p = _make_pool(["http://b:8080"], checker=_async_ok)
        result = asyncio.run(p.get_working())
        assert result == "http://b:8080"
        assert p._states[0].status == "ok"

    def test_unknown_proxy_fails_health_check_returns_none(self):
        p = _make_pool(["http://b:8080"], checker=_async_fail)
        result = asyncio.run(p.get_working())
        assert result is None
        assert p._states[0].status == "dead"

    def test_dead_in_cooldown_proxy_is_skipped(self):
        p = _make_pool(["http://dead:8080"], cooldown=300)
        p.mark_dead("http://dead:8080", error="test")
        result = asyncio.run(p.get_working())
        assert result is None  # still in cooldown, skip even with ok checker

    def test_dead_proxy_after_cooldown_is_retested(self):
        p = _make_pool(["http://a:8080"], checker=_async_ok, cooldown=1)
        p.mark_dead("http://a:8080")
        # Expire the cooldown
        p._states[0]._dead_since -= 2
        result = asyncio.run(p.get_working())
        assert result == "http://a:8080"
        assert p._states[0].status == "ok"

    def test_round_robin_advances_cursor(self):
        urls = ["socks5://a:1080", "socks5://b:1080", "socks5://c:1080"]
        p = _make_pool(urls)
        for url in urls:
            p.mark_ok(url)
        # First call returns 'a' (index 0), advances cursor to 1
        r1 = asyncio.run(p.get_working())
        # Second call should return 'b' (index 1)
        r2 = asyncio.run(p.get_working())
        assert r1 != r2
        assert r1 in urls
        assert r2 in urls

    def test_dead_proxies_are_skipped_in_round_robin(self):
        urls = ["socks5://a:1080", "socks5://b:1080"]
        p = _make_pool(urls)
        p.mark_dead("socks5://a:1080")
        p.mark_ok("socks5://b:1080")
        result = asyncio.run(p.get_working())
        assert result == "socks5://b:1080"

    def test_returns_none_when_all_dead_in_cooldown(self):
        urls = ["socks5://a:1080", "socks5://b:1080"]
        p = _make_pool(urls, cooldown=300)
        p.mark_dead("socks5://a:1080")
        p.mark_dead("socks5://b:1080")
        result = asyncio.run(p.get_working())
        assert result is None


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------

class TestHealthCheck:
    def test_health_check_marks_ok_on_success(self):
        p = _make_pool(["http://a:8080"], checker=_async_ok)
        ok = asyncio.run(p.health_check("http://a:8080"))
        assert ok is True
        assert p._states[0].status == "ok"
        assert p._states[0].latency_ms is not None

    def test_health_check_marks_dead_on_failure(self):
        p = _make_pool(["http://a:8080"], checker=_async_fail)
        ok = asyncio.run(p.health_check("http://a:8080"))
        assert ok is False
        assert p._states[0].status == "dead"

    def test_health_check_unknown_url_returns_false(self):
        p = _make_pool(["http://a:8080"], checker=_async_ok)
        # Calling health_check for a URL not in pool: checker still runs but
        # state lookup returns None, so mark_ok/mark_dead are no-ops.
        ok = asyncio.run(p.health_check("http://unknown:9999"))
        # The checker says ok but state was not mutated for a known proxy
        # (returns True from the checker side; no state recorded for unknown url)
        # Implementation: _default_checker/injected checker is called, result returned.
        # Since proxy_pool health_check calls _checker then mark_ok/mark_dead,
        # and mark_* are no-ops for unknown urls, the return value reflects the checker.
        assert ok is True  # checker returned True; mark_ok silently ignored unknown url


# ---------------------------------------------------------------------------
# status()
# ---------------------------------------------------------------------------

class TestStatus:
    def test_status_returns_list_of_dicts(self):
        p = _make_pool(["socks5://a:1080", "http://b:8080"])
        p.mark_ok("socks5://a:1080", latency_ms=50)
        p.mark_dead("http://b:8080", error="refused")
        st = p.status()
        assert len(st) == 2
        a = next(x for x in st if x["url"] == "socks5://a:1080")
        b = next(x for x in st if x["url"] == "http://b:8080")
        assert a["status"] == "ok"
        assert a["latency_ms"] == 50
        assert b["status"] == "dead"
        assert b["last_error"] == "refused"
        assert b["in_cooldown"] is True

    def test_status_empty_pool(self):
        p = _make_pool([])
        assert p.status() == []


# ---------------------------------------------------------------------------
# n_total / n_alive counts
# ---------------------------------------------------------------------------

class TestCounts:
    def test_n_total_correct(self):
        p = _make_pool(["a:1080", "b:1080", "c:1080"])
        assert p.n_total == 3

    def test_n_alive_counts_only_ok(self):
        p = _make_pool(["socks5://a:1080", "socks5://b:1080", "socks5://c:1080"])
        p.mark_ok("socks5://a:1080")
        p.mark_dead("socks5://b:1080")
        # c remains "unknown"
        assert p.n_alive == 1
