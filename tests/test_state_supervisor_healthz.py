from __future__ import annotations

import asyncio
from datetime import date

import pytest

from src.health import HealthRegistry
from src.healthz import Healthz
from src.recap import Recap, format_recap
from src.state import State, load, save
from src.supervisor import supervise


def _run(coro):
    return asyncio.run(coro)


def test_state_round_trips_and_resets_daily_twitter_counter(tmp_path):
    path = tmp_path / "state.json"
    state = State()
    state.bump_twitter_count(date(2026, 7, 24))
    state.bump_twitter_count(date(2026, 7, 24))
    assert state.twitter_count_for(date(2026, 7, 25)) == 0

    save(state, path)
    restored = load(path)

    assert restored.twitter_posts_today == 2
    assert restored.twitter_count_for(date(2026, 7, 24)) == 2
    assert restored.paused is False


def test_load_missing_state_returns_fresh_state(tmp_path):
    assert load(tmp_path / "missing.json") == State()


@pytest.mark.asyncio
async def test_healthz_returns_200_then_503_when_tick_is_stale(monkeypatch):
    healthz = Healthz(stale_after_seconds=60)

    healthy = await healthz._handler(None)
    healthz._last_tick -= 61
    stale = await healthz._handler(None)

    assert healthy.status == 200
    assert stale.status == 503
    assert "stale" in stale.text


@pytest.mark.asyncio
async def test_supervisor_restarts_failed_task_with_bounded_backoff():
    health = HealthRegistry("2026-01-01T00:00:00+00:00")
    calls = 0

    async def task_factory():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError(f"failure {calls}")
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await supervise(
            "source:HL",
            task_factory,
            health,
            initial_backoff=0,
            max_backoff=0,
        )

    assert calls == 3
    snapshot = health.snapshot()
    component = next(row for row in snapshot["components"] if row["component"] == "source:HL")
    assert component["status"] == "up"
    assert snapshot["ready"] is True


def test_recap_format_is_signed_and_contains_window_summary():
    recap = Recap(window="day", pnl_usd=-1234.5, pnl_pct=-2.25, trades=7, wins=3, losses=4)

    text = format_recap(recap, "https://pool.example")

    assert "Daily recap" in text
    assert "P&L: -$1,234 (-2.25%)" in text
    assert "Trades: 7  W/L: 3/4" in text
    assert text.endswith("https://pool.example")
