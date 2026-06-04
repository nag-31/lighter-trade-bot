"""Tests for src/health.py — HealthRegistry."""

from __future__ import annotations

import pytest

from src.health import HealthRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

STARTED = "2026-01-01T00:00:00+00:00"


def _reg() -> HealthRegistry:
    return HealthRegistry(started_at_iso=STARTED)


# ---------------------------------------------------------------------------
# Basic transitions
# ---------------------------------------------------------------------------

class TestMarkUp:
    def test_sets_status_up(self):
        r = _reg()
        r.mark_up("database")
        snap = r.snapshot()
        comp = snap["components"][0]
        assert comp["status"] == "up"
        assert comp["component"] == "database"

    def test_clears_error_on_recovery(self):
        r = _reg()
        r.mark_down("database", error="connection refused")
        r.mark_up("database")
        snap = r.snapshot()
        comp = snap["components"][0]
        assert comp["status"] == "up"
        assert comp["error"] == ""

    def test_records_last_ok(self):
        r = _reg()
        r.mark_up("telegram", ok_ts="2026-06-01T12:00:00+00:00")
        snap = r.snapshot()
        comp = snap["components"][0]
        assert comp["last_ok"] == "2026-06-01T12:00:00+00:00"

    def test_auto_last_ok_when_not_provided(self):
        r = _reg()
        r.mark_up("database")
        snap = r.snapshot()
        comp = snap["components"][0]
        # Should have populated last_ok automatically
        assert comp["last_ok"] is not None
        assert comp["last_ok"] != ""


class TestMarkDown:
    def test_sets_status_down(self):
        r = _reg()
        r.mark_down("telegram", error="timeout")
        snap = r.snapshot()
        comp = snap["components"][0]
        assert comp["status"] == "down"
        assert comp["error"] == "timeout"

    def test_ok_is_false_when_any_down(self):
        r = _reg()
        r.mark_up("database")
        r.mark_down("telegram", error="timeout")
        assert r.snapshot()["ok"] is False


class TestMarkDegraded:
    def test_sets_status_degraded(self):
        r = _reg()
        r.mark_degraded("source:HL", error="WS reconnect loop")
        snap = r.snapshot()
        comp = snap["components"][0]
        assert comp["status"] == "degraded"
        assert comp["error"] == "WS reconnect loop"

    def test_ok_is_false_when_any_degraded(self):
        r = _reg()
        r.mark_up("database")
        r.mark_degraded("source:HL", error="WS reconnect loop")
        assert r.snapshot()["ok"] is False


class TestMarkDisabled:
    def test_sets_status_disabled(self):
        r = _reg()
        r.mark_disabled("source:Binance", detail="not configured")
        snap = r.snapshot()
        comp = snap["components"][0]
        assert comp["status"] == "disabled"
        assert comp["detail"] == "not configured"

    def test_disabled_does_not_count_as_problem(self):
        r = _reg()
        r.mark_up("database")
        r.mark_disabled("source:Binance")
        assert r.snapshot()["ok"] is True

    def test_all_up_plus_disabled_is_ok(self):
        r = _reg()
        r.mark_up("database")
        r.mark_up("telegram")
        r.mark_up("source:HL")
        r.mark_disabled("source:Binance")
        assert r.snapshot()["ok"] is True


# ---------------------------------------------------------------------------
# snapshot() structure
# ---------------------------------------------------------------------------

class TestSnapshot:
    def test_started_at_preserved(self):
        r = _reg()
        assert r.snapshot()["started_at"] == STARTED

    def test_ok_true_when_all_up(self):
        r = _reg()
        r.mark_up("database")
        r.mark_up("telegram")
        r.mark_up("source:HL")
        assert r.snapshot()["ok"] is True

    def test_ok_true_with_no_components(self):
        r = _reg()
        assert r.snapshot()["ok"] is True

    def test_components_sorted_by_name(self):
        r = _reg()
        r.mark_up("source:HL")
        r.mark_up("database")
        r.mark_up("telegram")
        names = [c["component"] for c in r.snapshot()["components"]]
        assert names == sorted(names)

    def test_now_field_present(self):
        r = _reg()
        snap = r.snapshot()
        assert "now" in snap
        assert snap["now"]

    def test_down_and_degraded_both_make_ok_false(self):
        r = _reg()
        r.mark_down("telegram", error="err")
        r.mark_degraded("source:HL", error="err2")
        assert r.snapshot()["ok"] is False

    def test_recovery_from_down_to_up_flips_ok(self):
        r = _reg()
        r.mark_down("telegram", error="err")
        assert r.snapshot()["ok"] is False
        r.mark_up("telegram")
        assert r.snapshot()["ok"] is True

    def test_last_ok_preserved_across_degraded(self):
        """last_ok from the previous 'up' state is retained when going degraded."""
        r = _reg()
        r.mark_up("source:HL", ok_ts="2026-06-01T10:00:00+00:00")
        r.mark_degraded("source:HL", error="blip")
        snap = r.snapshot()
        comp = next(c for c in snap["components"] if c["component"] == "source:HL")
        assert comp["last_ok"] == "2026-06-01T10:00:00+00:00"


# ---------------------------------------------------------------------------
# set() validation
# ---------------------------------------------------------------------------

class TestSetValidation:
    def test_invalid_status_raises(self):
        r = _reg()
        with pytest.raises(ValueError, match="Invalid status"):
            r.set("database", "unknown_status")
