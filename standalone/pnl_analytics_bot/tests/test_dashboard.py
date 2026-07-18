from __future__ import annotations

from aiohttp.test_utils import TestClient, TestServer
import pytest

from standalone.pnl_analytics_bot.dashboard.server import build_payload, create_app
from standalone.pnl_analytics_bot.reports.fixtures import acceptance_fills, scenario_fills


def test_dashboard_payload_covers_human_review_scenarios():
    payload = build_payload(scenario_fills(), validate_scenarios=True)
    assert payload["analytics"]["closed_trades"] == 10
    assert payload["analytics"]["open_positions"] == 1
    assert payload["analytics"]["wins"] == 5
    assert payload["analytics"]["losses"] == 4
    assert payload["analytics"]["breakevens"] == 1
    assert payload["analytics"]["net_pnl"] == "45.00000000"
    assert payload["summary"]["closed_round_trips_reconstructed"] == 10
    assert len(payload["round_trips"]) == 10
    assert all(check["passed"] for check in payload["scenario_checks"])
    symbols = {(check["symbol"], check["direction"]) for check in payload["scenario_checks"]}
    assert ("LONGLOSS", "long") in symbols
    assert ("SHORTLOSS", "short") in symbols
    assert ("PARTIAL_TOTAL_LOSS", "long") in symbols
    assert ("PARTIAL_TOTAL_WIN", "long") in symbols
    assert ("FLIPCASE", "long") in symbols
    assert ("FLIPCASE", "short") in symbols
    scalein = next(rt for rt in payload["round_trips"] if rt["symbol"] == "SCALEIN")
    assert scalein["avg_entry"] == "175.00000000"
    assert scalein["net_pnl"] == "20.00000000"


def test_acceptance_payload_does_not_show_false_scenario_failures():
    payload = build_payload(acceptance_fills())
    assert payload["summary"]["closed_round_trips_reconstructed"] == 6
    assert payload["scenario_checks"] == []


def test_scenario_validation_is_explicitly_enabled():
    payload = build_payload(scenario_fills())
    assert payload["summary"]["closed_round_trips_reconstructed"] == 10
    assert payload["scenario_checks"] == []


def test_time_series_is_chronological_and_matches_closed_trades():
    payload = build_payload(scenario_fills(), validate_scenarios=True)
    bars = payload["time_series"]["pnl_bars"]
    curve = payload["time_series"]["equity_curve"]
    assert len(bars) == payload["analytics"]["closed_trades"]
    assert len(curve) == payload["analytics"]["closed_trades"]
    assert [point["index"] for point in curve] == list(range(1, len(curve) + 1))
    assert [bar["closed_at"] for bar in bars] == sorted(bar["closed_at"] for bar in bars)
    assert curve[-1]["equity"] == payload["analytics"]["net_pnl"]


@pytest.mark.asyncio
async def test_dashboard_http_endpoints():
    payload = build_payload(scenario_fills(), validate_scenarios=True)
    app = create_app(lambda: payload)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        index = await client.get("/")
        assert index.status == 200
        html = await index.text()
        assert "Standalone PnL Analytics" in html
        assert "Close Evidence" in html
        assert "timeSeriesRows" in html

        summary = await client.get("/api/summary")
        assert summary.status == 200
        body = await summary.json()
        assert body["analytics"]["closed_trades"] == 10
        assert body["analytics"]["breakevens"] == 1

        ts = await client.get("/api/time-series")
        assert ts.status == 200
        assert len((await ts.json())["equity_curve"]) == 10

        checks = await client.get("/api/scenarios")
        assert checks.status == 200
        assert all(c["passed"] for c in (await checks.json())["scenario_checks"])
    finally:
        await client.close()
