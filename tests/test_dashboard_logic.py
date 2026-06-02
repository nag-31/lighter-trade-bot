"""Tests for dashboard-level logic: dedup, seen_tids, aggregate buffer, position snapshot."""

import asyncio
import hashlib
import time
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.position_tracker import PositionTracker
from src.types import EventKind, Position
from tests.conftest import make_position, make_trade


# ---------------------------------------------------------------------------
# TG alert dedup hash logic (mirrors dashboard._tg_sent implementation)
# ---------------------------------------------------------------------------

class TestTgDedup:
    """Replicate the dedup dict logic extracted from _run() to verify behavior."""

    def _make_dedup(self):
        """Return a (send_fn, sent_dict) pair — same logic as dashboard.tg_send."""
        sent: dict[str, float] = {}
        WINDOW = 90.0

        def send(text: str) -> bool:
            """Returns True if message was NOT suppressed."""
            h = hashlib.md5(text.encode()).hexdigest()
            now = time.monotonic()
            expired = [k for k, t in sent.items() if now - t > WINDOW]
            for k in expired:
                del sent[k]
            if h in sent:
                return False
            sent[h] = now
            return True

        return send, sent

    def test_first_send_passes(self):
        send, _ = self._make_dedup()
        assert send("hello world") is True

    def test_immediate_duplicate_suppressed(self):
        send, _ = self._make_dedup()
        send("hello")
        assert send("hello") is False

    def test_different_text_not_suppressed(self):
        send, _ = self._make_dedup()
        send("message A")
        assert send("message B") is True

    def test_expired_entry_allows_resend(self):
        send, sent = self._make_dedup()
        h = hashlib.md5(b"hello").hexdigest()
        # Manually plant an old timestamp
        sent[h] = time.monotonic() - 200  # 200s ago — expired
        assert send("hello") is True

    def test_multiple_different_messages(self):
        send, _ = self._make_dedup()
        texts = [f"alert {i}" for i in range(20)]
        results = [send(t) for t in texts]
        assert all(results)  # all unique, all pass

    def test_duplicate_in_sequence_only_second_suppressed(self):
        send, _ = self._make_dedup()
        assert send("msg") is True
        assert send("msg") is False
        assert send("msg") is False  # still suppressed

    def test_position_difference_changes_hash(self):
        """The two SUI alerts in the bug had different positions — verify they'd both
        have been sent before the fix (different text → different hash)."""
        send, _ = self._make_dedup()
        alert1 = "Added 🟢 LONG SUI\n+$5,000 across 13 fills → position now $10,000\nAvg entry: $1.0371  |  10x"
        alert2 = "Added 🟢 LONG SUI\n+$4,856 across 12 fills → position now $5,000\nAvg entry: $1.0371  |  10x"
        assert send(alert1) is True
        assert send(alert2) is True  # different text → different hash → not suppressed

    def test_exact_duplicate_suppressed_including_same_position(self):
        """After the position-snapshot fix, both alerts should be identical → second suppressed."""
        send, _ = self._make_dedup()
        alert = "Added 🟢 LONG SUI\n+$5,000 across 13 fills → position now $10,000\nAvg entry: $1.0371  |  10x"
        assert send(alert) is True
        assert send(alert) is False  # exact duplicate — suppressed


# ---------------------------------------------------------------------------
# TG alert log — dashboard panel mirroring what was sent to the bot
# ---------------------------------------------------------------------------

class TestTgAlertLog:
    """Replicate dashboard._record_tg_alert: newest-first insert, capped length."""

    def _make_recorder(self, cap: int = 100):
        alerts: list[dict] = []

        def record(kind: str, text: str) -> None:
            alerts.insert(0, {"kind": kind, "text": text})
            del alerts[cap:]

        return record, alerts

    def test_first_alert_recorded(self):
        record, alerts = self._make_recorder()
        record("text", "Opened LONG HYPE")
        assert len(alerts) == 1
        assert alerts[0]["text"] == "Opened LONG HYPE"

    def test_newest_first_ordering(self):
        record, alerts = self._make_recorder()
        record("text", "first")
        record("text", "second")
        record("card", "third")
        assert [a["text"] for a in alerts] == ["third", "second", "first"]

    def test_kind_preserved(self):
        record, alerts = self._make_recorder()
        record("text", "an open alert")
        record("card", "🖼 PnL card · CLOSE LONG HYPE · +$1,200.00  [NK]")
        assert alerts[0]["kind"] == "card"
        assert alerts[1]["kind"] == "text"

    def test_capped_at_max(self):
        record, alerts = self._make_recorder(cap=100)
        for i in range(150):
            record("text", f"alert {i}")
        assert len(alerts) == 100
        # Newest retained, oldest dropped
        assert alerts[0]["text"] == "alert 149"
        assert alerts[-1]["text"] == "alert 50"

    def test_cap_boundary_exact(self):
        record, alerts = self._make_recorder(cap=3)
        for t in ["a", "b", "c", "d"]:
            record("text", t)
        assert [a["text"] for a in alerts] == ["d", "c", "b"]

    def test_multiline_text_preserved(self):
        """Full multi-line alert text is stored verbatim for dashboard pre-wrap."""
        record, alerts = self._make_recorder()
        msg = "📍 NK\nOpened 🟢 LONG HYPE\nEntry: $57.2000  |  Notional: $1,000  |  15x"
        record("text", msg)
        assert alerts[0]["text"] == msg
        assert "\n" in alerts[0]["text"]


# ---------------------------------------------------------------------------
# seen_tids dedup — prevents same fill processed twice
# ---------------------------------------------------------------------------

class TestSeenTids:
    def test_new_trade_id_not_in_seen(self):
        seen: set[int] = set()
        assert 1001 not in seen

    def test_add_and_check(self):
        seen: set[int] = set()
        seen.add(1001)
        assert 1001 in seen

    def test_second_instance_has_separate_seen(self):
        """Simulate two independent bot instances — each has own seen_tids.
        Both would process the same fill independently (root cause of double alert bug)."""
        seen_a: set[int] = set()
        seen_b: set[int] = set()
        fill_ids = {101, 102, 103}
        # Both instances process the same fills
        for tid in fill_ids:
            seen_a.add(tid)
            seen_b.add(tid)
        # Both sets fully populated — no cross-dedup
        assert seen_a == seen_b == fill_ids

    def test_duplicate_tid_in_same_set(self):
        seen: set[int] = set()
        seen.add(5)
        # Second time — skip processing
        assert 5 in seen  # would be skipped by consumer
        seen.add(5)       # set.add is idempotent
        assert len(seen) == 1


# ---------------------------------------------------------------------------
# Position snapshot in aggregate buffer
# ---------------------------------------------------------------------------

class TestPositionSnapshot:
    """Verify the buffer stores and uses the position captured at fill time,
    not the live tracker state at flush time."""

    def test_buffer_captures_position_at_fill_time(self):
        tracker = PositionTracker(source="test")
        # Open a position via two fills
        tracker.apply(make_trade(trade_id=1, side="long", size="50", price="1.0"))
        tracker.apply(make_trade(trade_id=2, side="long", size="50", price="1.0"))

        # Snapshot at fill time
        pos_at_fill_time = tracker.snapshot().get(0)
        assert pos_at_fill_time is not None
        notional_at_fill = pos_at_fill_time.notional_usd
        assert notional_at_fill == Decimal("100")

        # Simulate reconciler replacing tracker state with different value
        reconciled = make_position(
            market_id=0, market_symbol="BTC", side="long",
            size="50", avg_entry_price="1.0",
        )
        tracker.seed({0: reconciled})

        # If we naively re-read from tracker NOW, we'd get the reconciled (wrong) value
        pos_after_reconcile = tracker.snapshot().get(0)
        assert pos_after_reconcile.notional_usd == Decimal("50")  # different!

        # But using pos_at_fill_time (buffered) we still have the correct value
        assert pos_at_fill_time.notional_usd == Decimal("100")

    def test_buffer_refreshed_with_each_fill(self):
        """Each successive fill updates the buffer's position to the latest post-fill state."""
        tracker = PositionTracker(source="test")

        positions_captured = []
        for i in range(3):
            tracker.apply(make_trade(trade_id=i, side="long", size="10", price="1.0"))
            current_pos = tracker.snapshot().get(0)
            positions_captured.append(current_pos)

        # Last captured position should have size = 30
        assert positions_captured[-1].size == Decimal("30")
        # Intermediate ones were smaller
        assert positions_captured[0].size == Decimal("10")
        assert positions_captured[1].size == Decimal("20")

    def test_reconciler_does_not_affect_buffered_position(self):
        """Once position is stored in buffer, reconciler cannot change what gets sent."""
        tracker = PositionTracker(source="test")
        tracker.apply(make_trade(trade_id=1, side="long", size="100", price="1.0"))

        # Buffer stores snapshot
        buffered_pos = tracker.snapshot().get(0)

        # Reconciler fires and completely replaces positions
        tracker.seed({})

        # Buffer still has the old snapshot
        assert buffered_pos is not None
        assert buffered_pos.size == Decimal("100")


# ---------------------------------------------------------------------------
# Aggregate buffer accumulation (unit test of the business logic)
# ---------------------------------------------------------------------------

class TestAggregateAccumulation:
    """Test the SIZE_CHANGE aggregation logic in isolation."""

    def test_first_fill_creates_buffer_entry(self):
        pending: dict = {}
        tracker = PositionTracker(source="test")

        t = make_trade(trade_id=1, side="long", size="10", price="1.0")
        tracker.apply(t)

        fill_notional = t.size * t.price
        pos = tracker.snapshot().get(t.market_id)
        key = ("lighter:42", t.market_id)
        pending[key] = {
            "net_added": fill_notional,
            "n_fills": 1,
            "leverage": 10.0,
            "position": pos,
        }

        assert pending[key]["n_fills"] == 1
        assert pending[key]["net_added"] == Decimal("10")

    def test_second_fill_accumulates(self):
        pending: dict = {}
        tracker = PositionTracker(source="test")

        t1 = make_trade(trade_id=1, side="long", size="10", price="1.0")
        tracker.apply(t1)
        key = ("lighter:42", t1.market_id)
        pending[key] = {
            "net_added": t1.size * t1.price,
            "n_fills": 1,
            "leverage": 10.0,
            "position": tracker.snapshot().get(t1.market_id),
        }

        t2 = make_trade(trade_id=2, side="long", size="20", price="1.0")
        tracker.apply(t2)
        pos = tracker.snapshot().get(t2.market_id)
        pending[key]["net_added"] += t2.size * t2.price
        pending[key]["n_fills"] += 1
        pending[key]["position"] = pos

        assert pending[key]["n_fills"] == 2
        assert pending[key]["net_added"] == Decimal("30")
        assert pending[key]["position"].size == Decimal("30")

    def test_flush_uses_buffered_position_not_live_tracker(self):
        """Core fix: flush should use buf['position'], not re-read tracker."""
        tracker = PositionTracker(source="test")
        t = make_trade(trade_id=1, side="long", size="100", price="1.0")
        tracker.apply(t)

        buffered_pos = tracker.snapshot().get(t.market_id)
        pending = {
            ("src", t.market_id): {
                "net_added": Decimal("100"),
                "n_fills": 1,
                "leverage": 10.0,
                "position": buffered_pos,
            }
        }

        # Reconciler fires mid-window
        tracker.seed({})

        # Flush logic reads buf["position"]
        buf = pending.pop(("src", t.market_id))
        pos_used = buf.get("position") or tracker.snapshot().get(t.market_id)

        assert pos_used is not None
        assert pos_used.size == Decimal("100")  # buffered value, not reconciled None

    def test_flush_skips_if_position_is_none_after_close(self):
        tracker = PositionTracker(source="test")
        t_open  = make_trade(trade_id=1, side="long",  size="10", price="1.0")
        t_close = make_trade(trade_id=2, side="short", size="10", price="1.1")
        tracker.apply(t_open)
        tracker.apply(t_close)

        # Position is now gone
        pos = tracker.snapshot().get(t_open.market_id)
        assert pos is None

        # flush_aggregate would skip because pos is None (position closed)


# ---------------------------------------------------------------------------
# Debounce: timer resets on each new fill so cross-poll fills batch together
# ---------------------------------------------------------------------------

class TestAggregateDebounce:
    """Verify the debounce logic — timer replaced on every subsequent fill.

    The bug: with aggregate_window=60s and rest_poll=60s, fills from a second
    REST poll could arrive just after the first buffer flushed, creating a
    second alert for the same position-building session.

    The fix: cancel + restart the 60s timer on every new fill, so all fills
    that arrive within 60s of the LAST fill are batched together.
    """

    def test_debounce_replaces_task_in_buffer(self):
        """Simulate two successive fills updating the buffer task reference."""
        # We model the pending dict manually — the key insight is that the
        # 'task' field should be replaced on each subsequent fill.
        import asyncio
        from unittest.mock import MagicMock

        pending: dict = {}
        key = ("src", 0)

        # First fill: creates buffer with task_A
        task_a = MagicMock()
        pending[key] = {"net_added": Decimal("10"), "n_fills": 1, "task": task_a}

        # Second fill: should cancel task_A and replace with task_B
        task_b = MagicMock()
        task_a.cancel()   # simulates the cancel call in _accumulate_size_change
        pending[key]["net_added"] += Decimal("20")
        pending[key]["n_fills"] += 1
        pending[key]["task"] = task_b  # timer reset

        assert task_a.cancel.called
        assert pending[key]["n_fills"] == 2
        assert pending[key]["net_added"] == Decimal("30")
        assert pending[key]["task"] is task_b  # new timer, not original

    def test_debounce_accumulation_total(self):
        """Three fills from two different REST polls all end up in one buffer."""
        pending: dict = {}
        key = ("lighter:42", 0)

        fills = [
            Decimal("500"),   # REST poll 1 — fill 1
            Decimal("600"),   # REST poll 1 — fill 2
            Decimal("1000"),  # REST poll 2 — arrives after first batch (debounce resets timer)
        ]

        for notional in fills:
            if key in pending:
                pending[key]["net_added"] += notional
                pending[key]["n_fills"] += 1
                # Debounce: cancel old task, create new (mocked here as no-op)
            else:
                pending[key] = {"net_added": notional, "n_fills": 1}

        assert pending[key]["n_fills"] == 3
        assert pending[key]["net_added"] == Decimal("2100")

    def test_second_alert_prevented_when_fill_resets_timer(self):
        """After the first buffer flushes, a new fill would normally open a second buffer.
        With debounce, if the fill arrives BEFORE the flush fires, it resets the timer
        so no second buffer is needed and only one alert is sent."""
        # This models the state where key is still in _pending (timer not yet fired),
        # a new fill arrives, and the timer is reset rather than creating a new buffer.
        pending: dict = {}
        key = ("src", 1)

        from unittest.mock import MagicMock

        task_a = MagicMock()
        pending[key] = {"net_added": Decimal("1100"), "n_fills": 2, "task": task_a}

        # New fill arrives — key IS in pending, so we accumulate (not create new buffer)
        new_fill = Decimal("1000")
        task_b = MagicMock()
        task_a.cancel()
        pending[key]["net_added"] += new_fill
        pending[key]["n_fills"] += 1
        pending[key]["task"] = task_b

        # Result: still one buffer entry (no second buffer created)
        assert len(pending) == 1
        assert pending[key]["n_fills"] == 3
        assert pending[key]["net_added"] == Decimal("2100")


# ---------------------------------------------------------------------------
# PID lock logic (unit test — no filesystem)
# ---------------------------------------------------------------------------

class TestPidLock:
    def _is_pid_alive(self, pid: int) -> bool:
        """Cross-platform check: is a process with this PID alive?"""
        import os, sys
        if sys.platform == "win32":
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle == 0:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        else:
            try:
                os.kill(pid, 0)
                return True
            except ProcessLookupError:
                return False
            except PermissionError:
                return True  # exists but can't signal

    def test_stale_pid_returns_not_alive(self, tmp_path):
        """A PID file pointing to a non-existent process should be detected as dead."""
        pidfile = tmp_path / "lighterbot.pid"
        pidfile.write_text("99999999")  # almost certainly not a real process
        pid = int(pidfile.read_text().strip())
        # It's possible (though extremely unlikely) PID 99999999 exists.
        # We just verify our helper doesn't crash and returns a bool.
        alive = self._is_pid_alive(pid)
        assert isinstance(alive, bool)
        # On any real system this PID should be dead
        assert alive is False

    def test_own_pid_is_live(self):
        """Our own PID is always alive — foundation of the lock check."""
        import os
        alive = self._is_pid_alive(os.getpid())
        assert alive is True

    def test_pid_lock_logic_stale_file_overwritten(self, tmp_path):
        """Simulate the full _acquire_pid_lock flow with a stale PID file."""
        import os, sys
        pidfile = tmp_path / "lighterbot.pid"
        pidfile.write_text("99999999")  # stale

        # Replicate the lock logic
        if pidfile.exists():
            try:
                pid = int(pidfile.read_text().strip())
                if sys.platform == "win32":
                    alive = self._is_pid_alive(pid)
                    if alive:
                        acquired = False
                    else:
                        raise ProcessLookupError
                else:
                    os.kill(pid, 0)
                    acquired = False  # process is alive
            except (ProcessLookupError, PermissionError, ValueError, OSError):
                acquired = True  # stale file — we can start
        else:
            acquired = True

        assert acquired is True


# ---------------------------------------------------------------------------
# _sl_tp_alerted arm / dedup logic
# ---------------------------------------------------------------------------

class TestSlTpAlertedLogic:
    """Mirror the _sl_tp_alerted set logic from _run() to verify arm/dedup/discard behavior.

    The set tracks (source_id, market_id) keys for which a SL/TP TG alert has already
    fired this position lifecycle, preventing the reconciler from double-alerting.
    """

    # Helpers that replicate the in-_run() patterns -------------------------

    def _simulate_bootstrap(self, init_pos_market_ids, source_id="src:a"):
        """Returns an alerted set seeded as if bootstrap ran."""
        alerted: set[tuple[str, int]] = set()
        for mid in init_pos_market_ids:
            alerted.add((source_id, mid))
        return alerted

    def _reconciler_sl_tp_check(self, alerted, source_id, market_id, sl, tp,
                                 alerts_sent):
        """Simulate the reconciler SL/TP section for one market_id.
        Returns True if a TG alert would be sent."""
        key = (source_id, market_id)
        if (sl is not None or tp is not None) and key not in alerted:
            alerts_sent.append(key)
            alerted.add(key)
            return True
        return False

    def _simulate_close(self, alerted, source_id, market_id):
        alerted.discard((source_id, market_id))

    def _simulate_open_with_sl_tp(self, alerted, source_id, market_id, sl, tp):
        """Simulate fill-based OPEN handler pre-arming when SL/TP was shown."""
        if sl is not None or tp is not None:
            alerted.add((source_id, market_id))

    # Tests ------------------------------------------------------------------

    def test_bootstrap_suppresses_existing_positions(self):
        """Positions open at startup must never trigger a SL/TP alert."""
        alerted = self._simulate_bootstrap([10, 20, 30], source_id="src:a")
        alerts_sent = []
        # Reconciler runs and finds SL/TP for market 10
        self._reconciler_sl_tp_check(alerted, "src:a", 10, sl=1.0, tp=2.0, alerts_sent=alerts_sent)
        assert len(alerts_sent) == 0

    def test_new_position_alerts_once(self):
        """A brand-new position (not at bootstrap) should alert exactly once."""
        alerted: set[tuple[str, int]] = set()
        alerts_sent = []
        # First reconciler tick: SL/TP becomes known for market 5
        self._reconciler_sl_tp_check(alerted, "src:a", 5, sl=1.0, tp=None, alerts_sent=alerts_sent)
        assert len(alerts_sent) == 1

    def test_second_reconciler_tick_does_not_double_alert(self):
        """After the first SL/TP alert, subsequent ticks must be silent."""
        alerted: set[tuple[str, int]] = set()
        alerts_sent = []
        self._reconciler_sl_tp_check(alerted, "src:a", 5, sl=1.0, tp=2.0, alerts_sent=alerts_sent)
        # Second reconciler tick
        self._reconciler_sl_tp_check(alerted, "src:a", 5, sl=1.0, tp=2.0, alerts_sent=alerts_sent)
        assert len(alerts_sent) == 1  # still exactly one

    def test_neither_sl_nor_tp_never_alerts(self):
        """No alert when both SL and TP are None, even for a new position."""
        alerted: set[tuple[str, int]] = set()
        alerts_sent = []
        self._reconciler_sl_tp_check(alerted, "src:a", 5, sl=None, tp=None, alerts_sent=alerts_sent)
        assert len(alerts_sent) == 0
        # key must not be added either (so a future tick with real values can still alert)
        assert ("src:a", 5) not in alerted

    def test_discard_on_close_re_arms_for_reopen(self):
        """After a position closes, the key is removed so a reopen can alert again."""
        alerted: set[tuple[str, int]] = set()
        alerts_sent = []
        # Position opens, SL/TP alert fires
        self._reconciler_sl_tp_check(alerted, "src:a", 5, sl=1.0, tp=2.0, alerts_sent=alerts_sent)
        assert len(alerts_sent) == 1
        # Position closes
        self._simulate_close(alerted, "src:a", 5)
        assert ("src:a", 5) not in alerted
        # Position re-opens and SL/TP is discovered again — should alert
        self._reconciler_sl_tp_check(alerted, "src:a", 5, sl=1.5, tp=2.5, alerts_sent=alerts_sent)
        assert len(alerts_sent) == 2

    def test_open_with_sl_tp_pre_arms_suppresses_reconciler(self):
        """If fill-based OPEN already showed SL/TP, pre-arming prevents reconciler double-alert."""
        alerted: set[tuple[str, int]] = set()
        alerts_sent = []
        # Fill-based OPEN fires and pre-arms because SL/TP was included
        from decimal import Decimal
        self._simulate_open_with_sl_tp(alerted, "src:a", 7, sl=Decimal("45000"), tp=Decimal("60000"))
        assert ("src:a", 7) in alerted
        # Reconciler tick discovers same SL/TP — should NOT fire
        self._reconciler_sl_tp_check(alerted, "src:a", 7, sl=45000, tp=60000, alerts_sent=alerts_sent)
        assert len(alerts_sent) == 0

    def test_open_without_sl_tp_does_not_pre_arm(self):
        """Fill-based OPEN with no SL/TP must not pre-arm — reconciler can alert later."""
        alerted: set[tuple[str, int]] = set()
        alerts_sent = []
        # OPEN fill has no SL/TP
        self._simulate_open_with_sl_tp(alerted, "src:a", 8, sl=None, tp=None)
        assert ("src:a", 8) not in alerted
        # Reconciler later discovers SL/TP — should alert
        self._reconciler_sl_tp_check(alerted, "src:a", 8, sl=1.0, tp=None, alerts_sent=alerts_sent)
        assert len(alerts_sent) == 1

    def test_multiple_sources_independent(self):
        """Keys are (source_id, market_id) — same market_id on different sources are independent."""
        alerted: set[tuple[str, int]] = set()
        alerts_a = []
        alerts_b = []
        self._reconciler_sl_tp_check(alerted, "src:a", 1, sl=1.0, tp=2.0, alerts_sent=alerts_a)
        self._reconciler_sl_tp_check(alerted, "src:b", 1, sl=1.0, tp=2.0, alerts_sent=alerts_b)
        assert len(alerts_a) == 1
        assert len(alerts_b) == 1

    def test_purge_on_position_gone_also_discards(self):
        """When the reconciler purges an SL/TP cache key (position gone), _sl_tp_alerted is discarded too."""
        alerted: set[tuple[str, int]] = set()
        alerted.add(("src:a", 3))
        # Simulate the purge loop behaviour
        cache_key = ("src:a", 3)
        alerted.discard(cache_key)
        assert ("src:a", 3) not in alerted
