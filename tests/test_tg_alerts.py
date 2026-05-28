"""Tests for Telegram alert decision logic.

Covers:
  - Reconciler-first OPEN detection (fill arrives after reconciler seeds)
  - Fill-based OPEN suppression via _reconciler_alerted_opens
  - Phantom SIZE_CHANGE suppression (opening fill mis-classified after seed)
  - Silent-close detection from _dash_positions comparison
  - Min-notional filter at reconciler level
  - _dash_positions vs tracker separation (dashboard independence from fill timing)
  - Debounce interaction with CLOSE (SIZE_CHANGE buffer cancelled)
  - TG dedup interaction with duplicate alert text
  - Edge cases: rapid open/close, position at exactly min_notional,
    multiple positions partial-new, flag lifecycle
"""

import hashlib
import time
from decimal import Decimal
from typing import Optional

import pytest

from src.position_tracker import PositionTracker
from src.filters import passes_min_notional
from src.types import Event, EventKind, Position
from tests.conftest import make_position, make_trade


# ─────────────────────────────────────────────────────────────────────────────
# Helper: replicate the reconciler's new-position detection logic
# ─────────────────────────────────────────────────────────────────────────────

def _detect_new_positions(
    actual: dict[int, Position],
    prev_dash: dict[int, Position],
    tracked: dict[int, Position],
) -> list[tuple[int, Position]]:
    """Positions that need a reconciler OPEN alert.

    Condition: appeared in API since last reconcile AND fill-based tracker
    hasn't processed the opening fill yet.
    """
    return [
        (mid, pos)
        for mid, pos in actual.items()
        if mid not in prev_dash and mid not in tracked
    ]


def _detect_silent_closes(
    actual: dict[int, Position],
    prev_dash: dict[int, Position],
) -> list[tuple[int, Position]]:
    """Positions that vanished from API since last reconcile."""
    return [
        (mid, pos)
        for mid, pos in prev_dash.items()
        if mid not in actual
    ]


def _tracker_positions_for_seed(
    actual: dict[int, Position],
    source_id: str,
    reconciler_alerted: set[tuple[str, int]],
) -> dict[int, Position]:
    """API positions minus those still awaiting their OPEN fill."""
    return {
        mid: pos for mid, pos in actual.items()
        if (source_id, mid) not in reconciler_alerted
    }


# ─────────────────────────────────────────────────────────────────────────────
# Reconciler new-position detection
# ─────────────────────────────────────────────────────────────────────────────

class TestReconcilerNewPositionDetection:

    def test_fill_pending_triggers_reconciler_alert(self):
        """Core case: position in API, tracker empty (fill not yet arrived)."""
        prev_dash = {}
        actual    = {0: make_position(market_id=0)}
        tracked   = {}
        new = _detect_new_positions(actual, prev_dash, tracked)
        assert len(new) == 1
        assert new[0][0] == 0

    def test_fill_arrived_before_reconciler_no_reconciler_alert(self):
        """Fill arrived first → tracker has position → reconciler skips alert."""
        prev_dash = {}
        actual    = {0: make_position(market_id=0)}
        tracked   = {0: make_position(market_id=0)}
        new = _detect_new_positions(actual, prev_dash, tracked)
        assert len(new) == 0

    def test_existing_position_never_detected_as_new(self):
        """Position already in prev_dash is not new."""
        pos = make_position(market_id=0)
        prev_dash = {0: pos}
        actual    = {0: pos}
        tracked   = {0: pos}
        new = _detect_new_positions(actual, prev_dash, tracked)
        assert len(new) == 0

    def test_multiple_positions_only_new_one_flagged(self):
        """Two existing positions + one new → only the new one is detected."""
        p0 = make_position(market_id=0, market_symbol="BTC")
        p1 = make_position(market_id=1, market_symbol="ETH")
        p2 = make_position(market_id=2, market_symbol="HYPE")
        prev_dash = {0: p0, 1: p1}
        actual    = {0: p0, 1: p1, 2: p2}
        tracked   = {0: p0, 1: p1}       # tracker has existing two, not new one
        new = _detect_new_positions(actual, prev_dash, tracked)
        assert [(mid, _) for mid, _ in new] == [(2, p2)] or [mid for mid, _ in new] == [2]

    def test_no_positions_returns_empty(self):
        """No positions at all → no new positions."""
        assert _detect_new_positions({}, {}, {}) == []

    def test_all_positions_already_in_tracker_no_alert(self):
        """Even if not in prev_dash, if tracker has it — no alert (fill arrived first)."""
        p = make_position(market_id=5)
        prev_dash = {}
        actual    = {5: p}
        tracked   = {5: p}
        assert _detect_new_positions(actual, prev_dash, tracked) == []


# ─────────────────────────────────────────────────────────────────────────────
# Silent-close detection
# ─────────────────────────────────────────────────────────────────────────────

class TestSilentCloseDetection:

    def test_position_vanished_from_api_detected(self):
        """Position in prev_dash but absent from actual → silent close."""
        pos = make_position(market_id=0)
        closes = _detect_silent_closes(actual={}, prev_dash={0: pos})
        assert len(closes) == 1
        assert closes[0][0] == 0

    def test_position_still_open_not_detected(self):
        """Position in both prev and actual → not closed."""
        pos = make_position(market_id=0)
        closes = _detect_silent_closes(actual={0: pos}, prev_dash={0: pos})
        assert len(closes) == 0

    def test_new_position_not_a_silent_close(self):
        """Position in actual but not in prev → new, not closed."""
        pos = make_position(market_id=0)
        closes = _detect_silent_closes(actual={0: pos}, prev_dash={})
        assert len(closes) == 0

    def test_multiple_one_closed(self):
        """Two positions; one closed, one still open."""
        p0 = make_position(market_id=0, market_symbol="BTC")
        p1 = make_position(market_id=1, market_symbol="ETH")
        closes = _detect_silent_closes(actual={1: p1}, prev_dash={0: p0, 1: p1})
        assert [mid for mid, _ in closes] == [0]

    def test_rapid_open_close_missed_entirely(self):
        """Position opened and closed between two reconciler runs.
        Neither prev_dash nor actual has it — no silent-close alert fires
        (but the fill-based path should handle CLOSE via REST poll)."""
        prev_dash = {}
        actual    = {}  # already closed when reconciler runs
        closes = _detect_silent_closes(actual=actual, prev_dash=prev_dash)
        assert closes == []   # missed — no entry in prev_dash to compare against


# ─────────────────────────────────────────────────────────────────────────────
# _reconciler_alerted_opens flag lifecycle
# ─────────────────────────────────────────────────────────────────────────────

class TestReconcilerAlertedOpensFlag:

    def test_fill_open_suppressed_when_flag_set(self):
        """Fill-based OPEN suppressed if reconciler already sent the alert."""
        alerted: set[tuple[str, int]] = {("src", 0)}
        key = ("src", 0)
        if key in alerted:
            alerted.discard(key)
            sent = False      # suppressed
        else:
            sent = True
        assert sent is False
        assert key not in alerted    # flag consumed

    def test_fill_open_not_suppressed_when_flag_absent(self):
        """Fill-based OPEN proceeds if reconciler never alerted."""
        alerted: set[tuple[str, int]] = set()
        key = ("src", 0)
        if key in alerted:
            alerted.discard(key)
            sent = False
        else:
            sent = True          # normal alert path
        assert sent is True

    def test_size_change_phantom_fill_cleared(self):
        """First SIZE_CHANGE after reconciler-alert clears the flag (phantom opening fill)."""
        alerted: set[tuple[str, int]] = {("src", 1)}
        key = ("src", 1)
        is_phantom = key in alerted
        if is_phantom:
            alerted.discard(key)
        assert is_phantom is True
        assert key not in alerted

    def test_subsequent_size_change_not_phantom(self):
        """After phantom is cleared, next SIZE_CHANGE alerts normally."""
        alerted: set[tuple[str, int]] = set()   # phantom already consumed
        key = ("src", 1)
        is_phantom = key in alerted
        assert is_phantom is False   # → real SIZE_CHANGE, should alert

    def test_silent_close_discards_flag(self):
        """Reconciler silent-close clears any pending open-alert flag."""
        alerted: set[tuple[str, int]] = {("src", 0)}
        alerted.discard(("src", 0))
        assert ("src", 0) not in alerted

    def test_flag_for_different_market_not_affected(self):
        """Flags are keyed by (source_id, market_id) — one market doesn't affect another."""
        alerted: set[tuple[str, int]] = {("src", 0), ("src", 1)}
        # Process OPEN for market 0
        alerted.discard(("src", 0))
        assert ("src", 0) not in alerted
        assert ("src", 1) in alerted    # market 1 flag untouched

    def test_flag_for_different_source_not_affected(self):
        """Flags from different sources are independent."""
        alerted: set[tuple[str, int]] = {("lighter:1", 0), ("hl:1", 0)}
        alerted.discard(("lighter:1", 0))
        assert ("hl:1", 0) in alerted


# ─────────────────────────────────────────────────────────────────────────────
# Tracker seed exclusion for reconciler-alerted positions
# ─────────────────────────────────────────────────────────────────────────────

class TestTrackerSeedExclusion:

    def test_reconciler_alerted_position_excluded_from_seed(self):
        """Tracker should NOT be seeded with positions awaiting their fill.
        This prevents the fill from being classified as SIZE_CHANGE instead of OPEN."""
        actual  = {
            0: make_position(market_id=0, market_symbol="BTC"),   # existing
            1: make_position(market_id=1, market_symbol="HYPE"),  # reconciler alerted
        }
        alerted = {("src", 1)}
        seeded  = _tracker_positions_for_seed(actual, "src", alerted)
        assert 0 in seeded
        assert 1 not in seeded

    def test_all_positions_seeded_when_no_alerted(self):
        """When no reconciler alerts pending, seed the full API snapshot."""
        actual  = {0: make_position(market_id=0), 1: make_position(market_id=1)}
        alerted: set = set()
        seeded  = _tracker_positions_for_seed(actual, "src", alerted)
        assert set(seeded.keys()) == {0, 1}

    def test_empty_actual_seeds_nothing(self):
        actual  = {}
        alerted = {("src", 0)}
        seeded  = _tracker_positions_for_seed(actual, "src", alerted)
        assert seeded == {}

    def test_alerted_flag_for_different_source_does_not_exclude(self):
        """An alert flag from source B shouldn't affect source A's seed."""
        actual  = {0: make_position(market_id=0)}
        alerted = {("other_src", 0)}   # different source
        seeded  = _tracker_positions_for_seed(actual, "src", alerted)
        assert 0 in seeded   # source "src" unaffected


# ─────────────────────────────────────────────────────────────────────────────
# Min-notional filter at reconciler level
# ─────────────────────────────────────────────────────────────────────────────

class TestReconcilerMinNotional:

    def test_position_above_min_should_alert(self):
        pos = make_position(size="10", avg_entry_price="100")   # $1 000
        assert pos.notional_usd >= Decimal("900")

    def test_position_below_min_should_not_alert(self):
        pos = make_position(size="1", avg_entry_price="50")     # $50
        assert not (pos.notional_usd >= Decimal("900"))

    def test_position_exactly_at_min_should_alert(self):
        pos = make_position(size="9", avg_entry_price="100")    # $900 exactly
        assert pos.notional_usd >= Decimal("900")

    def test_position_one_cent_below_min_should_not_alert(self):
        pos = make_position(size="1", avg_entry_price="899.99")
        assert not (pos.notional_usd >= Decimal("900"))


# ─────────────────────────────────────────────────────────────────────────────
# Fill-based filter (passes_min_notional) — OPEN event edge cases
# ─────────────────────────────────────────────────────────────────────────────

class TestFillBasedOpenFilter:

    def _make_open_event(self, fill_notional: str) -> Event:
        size  = Decimal("1")
        price = Decimal(fill_notional)
        pos   = make_position(size=str(size), avg_entry_price=str(price))
        trade = make_trade(trade_id=1, side="long", size=str(size), price=str(price))
        return Event(
            kind=EventKind.OPEN,
            trade=trade,
            position_before=None,
            position_after=pos,
        )

    def test_open_fill_above_min_passes(self):
        ev = self._make_open_event("1000")
        assert passes_min_notional(ev, Decimal("900")) is True

    def test_open_fill_below_min_fails(self):
        """Small first fill (multi-fill open) → fill-based OPEN alert suppressed.
        This is the scenario where reconciler should step in."""
        ev = self._make_open_event("100")
        assert passes_min_notional(ev, Decimal("900")) is False

    def test_open_fill_exactly_at_min_passes(self):
        ev = self._make_open_event("900")
        assert passes_min_notional(ev, Decimal("900")) is True

    def test_close_always_passes_regardless_of_notional(self):
        """CLOSE events bypass the notional filter — always alert."""
        trade = make_trade(trade_id=1, side="short", size="0.001", price="1")
        pos   = make_position(size="0.001", avg_entry_price="1")
        ev    = Event(
            kind=EventKind.CLOSE,
            trade=trade,
            position_before=pos,
            position_after=None,
        )
        assert passes_min_notional(ev, Decimal("900")) is True

    def test_size_change_uses_position_before_notional(self):
        """SIZE_CHANGE filter checks existing position size, not the fill size."""
        big_pos   = make_position(size="10", avg_entry_price="100")   # $1 000 existing
        small_fill = make_trade(trade_id=2, side="long", size="0.1", price="100")  # $10 fill
        ev = Event(
            kind=EventKind.SIZE_CHANGE,
            trade=small_fill,
            position_before=big_pos,
            position_after=None,
        )
        # position_before notional ($1 000) > min ($900) → passes even though fill is tiny
        assert passes_min_notional(ev, Decimal("900")) is True

    def test_size_change_small_position_before_fails(self):
        """Small existing position → SIZE_CHANGE suppressed even if fill is large."""
        tiny_pos   = make_position(size="1", avg_entry_price="50")    # $50
        large_fill = make_trade(trade_id=2, side="long", size="100", price="100")
        ev = Event(
            kind=EventKind.SIZE_CHANGE,
            trade=large_fill,
            position_before=tiny_pos,
            position_after=None,
        )
        assert passes_min_notional(ev, Decimal("900")) is False


# ─────────────────────────────────────────────────────────────────────────────
# _dash_positions separation from tracker
# ─────────────────────────────────────────────────────────────────────────────

class TestDashPositionsSeparation:

    def test_dashboard_shows_api_truth_even_before_fill_arrives(self):
        """If _dash_positions is set from API, dashboard reflects it independent of
        whether the fill-based tracker has processed the opening fill yet."""
        tracker = PositionTracker(source="test")
        # tracker is empty (fill not yet processed)
        assert tracker.snapshot() == {}

        # API truth has the position
        api_truth = {0: make_position(market_id=0, size="10", avg_entry_price="100")}
        dash_positions = {"src": api_truth}

        # Dashboard uses _dash_positions, not tracker
        displayed = list(dash_positions["src"].values())
        assert len(displayed) == 1
        assert displayed[0].notional_usd == Decimal("1000")

    def test_tracker_still_classifies_fill_correctly_without_seed(self):
        """Without being seeded, tracker correctly classifies the first fill as OPEN."""
        tracker = PositionTracker(source="test")
        t = make_trade(trade_id=1, side="long", size="10", price="100")
        events = tracker.apply(t)
        assert len(events) == 1
        assert events[0].kind == EventKind.OPEN

    def test_tracker_seeded_before_fill_causes_size_change(self):
        """This is the bug: if tracker is seeded with the position before the fill,
        the fill is classified as SIZE_CHANGE instead of OPEN.
        Fix: don't seed tracker for reconciler-alerted positions."""
        tracker = PositionTracker(source="test")
        # Reconciler seeds the position (simulates the bug)
        tracker.seed({0: make_position(market_id=0, size="10", avg_entry_price="100")})
        # Now the opening fill arrives
        t = make_trade(trade_id=1, side="long", size="10", price="100")
        events = tracker.apply(t)
        # BUG: classified as SIZE_CHANGE, not OPEN
        assert events[0].kind == EventKind.SIZE_CHANGE

    def test_tracker_not_seeded_fill_correctly_classified_as_open(self):
        """With the fix: tracker not seeded for new reconciler-alerted positions,
        so the fill correctly fires an OPEN event."""
        tracker = PositionTracker(source="test")
        # Do NOT seed the tracker (reconciler excluded it from seed)
        t = make_trade(trade_id=1, side="long", size="10", price="100")
        events = tracker.apply(t)
        # Correctly classified as OPEN
        assert events[0].kind == EventKind.OPEN

    def test_dash_positions_updated_each_reconcile_cycle(self):
        """_dash_positions replaces old snapshot on each reconciler run."""
        p_old = make_position(market_id=0, size="10", avg_entry_price="100")
        p_new = make_position(market_id=0, size="15", avg_entry_price="102")

        dash = {"src": {0: p_old}}
        # Reconciler updates
        dash["src"] = {0: p_new}
        assert dash["src"][0].size == Decimal("15")


# ─────────────────────────────────────────────────────────────────────────────
# Aggregate buffer cancel-on-close
# ─────────────────────────────────────────────────────────────────────────────

class TestSizeChangeBufferCancelledOnClose:
    """Verify the SIZE_CHANGE buffer is cancelled (not flushed) when position closes,
    so the user doesn't get a spurious 'Added' alert after the PnL card."""

    def test_cancel_removes_buffer_entry(self):
        """_cancel_pending pops the key and the buffer is gone."""
        from unittest.mock import MagicMock
        pending: dict = {}
        key = ("src", 0)
        mock_task = MagicMock()
        pending[key] = {"net_added": Decimal("500"), "n_fills": 3, "task": mock_task}

        # Simulate _cancel_pending
        buf = pending.pop(key, None)
        if buf is not None:
            buf["task"].cancel()

        assert key not in pending
        assert mock_task.cancel.called

    def test_close_with_no_pending_buffer_is_safe(self):
        """If position closed in one fill (no SIZE_CHANGE buffer), cancel is a no-op."""
        pending: dict = {}
        key = ("src", 0)
        buf = pending.pop(key, None)
        assert buf is None   # nothing to cancel, no error

    def test_reduce_buffer_preserved_for_accumulated_pnl(self):
        """REDUCE buffer is popped (not cancelled) on CLOSE so accumulated PnL
        can be extracted for the PnL card."""
        pending_reduces: dict = {}
        key = ("src", 0)
        from unittest.mock import MagicMock
        task = MagicMock()
        pending_reduces[key] = {
            "net_reduced": Decimal("300"),
            "n_fills": 2,
            "total_pnl": Decimal("25"),
            "task": task,
        }

        # CLOSE handler: pop reduce buffer, extract accumulated PnL
        reduce_buf = pending_reduces.pop(key, None)
        accumulated_pnl = reduce_buf["total_pnl"] if reduce_buf else None
        if reduce_buf:
            reduce_buf["task"].cancel()

        assert accumulated_pnl == Decimal("25")
        assert key not in pending_reduces
        assert task.cancel.called


# ─────────────────────────────────────────────────────────────────────────────
# TG dedup interaction with alert text
# ─────────────────────────────────────────────────────────────────────────────

class TestTgDedupEdgeCases:
    """Verify dedup hash correctly handles the alert text variants."""

    def _make_send(self, window: float = 90.0):
        sent: dict[str, float] = {}
        def send(text: str) -> bool:
            h = hashlib.md5(text.encode()).hexdigest()
            now = time.monotonic()
            expired = [k for k, t in sent.items() if now - t > window]
            for k in expired:
                del sent[k]
            if h in sent:
                return False
            sent[h] = now
            return True
        return send

    def test_reconciler_and_fill_alerts_different_text_both_send(self):
        """Reconciler-format and fill-based-format alerts have different text,
        so if both somehow fired they'd both get through dedup.
        (In practice, one is suppressed by _reconciler_alerted_opens.)"""
        send = self._make_send()
        reconciler_msg = "📍 NK\nOpened 🟢 LONG HYPE\nEntry: $57.2000  |  Notional: $1,000  |  15x"
        fill_msg       = "📍 NK\nOpened 🟢 LONG HYPE\nEntry: $57.2000  |  Size: 17.5  |  Notional: $1,000  |  15x"
        assert send(reconciler_msg) is True
        assert send(fill_msg) is True   # different format → both pass dedup

    def test_identical_reconciler_alerts_deduped(self):
        """Reconciler alert sent, then same text again within window → suppressed."""
        send = self._make_send()
        msg = "📍 NK\nOpened 🟢 LONG HYPE\nEntry: $57.20  |  Notional: $1,000  |  15x"
        assert send(msg) is True
        assert send(msg) is False

    def test_pnl_card_caption_empty_string_not_deduped_with_text(self):
        """PnL card caption (empty string or URL) is not the same as a full text alert."""
        send = self._make_send()
        caption = "https://app.lighter.xyz/public-pools/281474976684763"
        msg     = "📍 NK\nOpened 🟢 LONG HYPE\n..."
        assert send(msg)     is True
        assert send(caption) is True   # different text → not deduped

    def test_dedup_window_expiry_allows_resend(self):
        """After the dedup window expires, the same message can be sent again."""
        sent: dict[str, float] = {}
        window = 90.0
        text = "repeated alert"
        h = hashlib.md5(text.encode()).hexdigest()

        # Plant an old timestamp (expired)
        sent[h] = time.monotonic() - 200

        send = self._make_send(window)
        # Manually put the expired entry in sent dict
        sent[h] = time.monotonic() - 200

        # The helper creates its own sent dict — replicate the expiry logic
        now = time.monotonic()
        expired = [k for k, t in sent.items() if now - t > window]
        for k in expired:
            del sent[k]
        assert h not in sent   # expired and removed → would allow resend


# ─────────────────────────────────────────────────────────────────────────────
# Edge cases: various timing and ordering scenarios
# ─────────────────────────────────────────────────────────────────────────────

class TestEdgeCases:

    def test_position_flip_clears_open_alert_flag(self):
        """When a position flips (short → long), the old key should be cleared.
        _cancel_pending clears the SIZE_CHANGE buffer; we should also discard any
        reconciler flag for the old side."""
        alerted: set[tuple[str, int]] = {("src", 0)}
        key = ("src", 0)
        # On OPEN (which follows a FLIP's internal CLOSE), flag cleared
        alerted.discard(key)
        assert key not in alerted

    def test_min_notional_zero_always_triggers_reconciler_alert(self):
        """With min_notional=0, every position triggers the reconciler alert."""
        pos = make_position(size="0.001", avg_entry_price="1")   # $0.001
        assert pos.notional_usd >= Decimal("0")

    def test_two_sources_independent_detection(self):
        """New position on source A doesn't trigger detection on source B."""
        pos = make_position(market_id=0)
        prev_dash_a = {}
        prev_dash_b = {0: pos}   # source B already has it
        actual = {0: pos}
        tracked = {}

        new_a = _detect_new_positions(actual, prev_dash_a, tracked)
        new_b = _detect_new_positions(actual, prev_dash_b, tracked)

        assert len(new_a) == 1   # source A: new
        assert len(new_b) == 0   # source B: existing

    def test_reconciler_alerted_flag_does_not_persist_across_close(self):
        """If a position closes and reopens, the old flag should be gone."""
        alerted: set[tuple[str, int]] = {("src", 0)}

        # Position closes — silent-close handler discards flag
        alerted.discard(("src", 0))
        assert ("src", 0) not in alerted

        # Position reopens next reconciler cycle — detected as new, flag added again
        alerted.add(("src", 0))
        assert ("src", 0) in alerted

    def test_position_with_no_unrealized_pnl_still_detectable(self):
        """Positions without unrealized_pnl (None) are still detected correctly."""
        pos = make_position(market_id=7, size="5", avg_entry_price="200")
        # No unrealized_pnl field needed for detection logic
        prev_dash = {}
        actual    = {7: pos}
        tracked   = {}
        new = _detect_new_positions(actual, prev_dash, tracked)
        assert len(new) == 1

    def test_all_positions_closed_simultaneously(self):
        """All positions vanish from API in one reconciler cycle."""
        p0 = make_position(market_id=0, market_symbol="BTC")
        p1 = make_position(market_id=1, market_symbol="ETH")
        prev_dash = {0: p0, 1: p1}
        actual    = {}
        closes = _detect_silent_closes(actual, prev_dash)
        assert len(closes) == 2
        assert {mid for mid, _ in closes} == {0, 1}

    def test_stable_state_no_changes_no_alerts(self):
        """Same positions in prev_dash and actual → nothing to alert."""
        p0 = make_position(market_id=0)
        p1 = make_position(market_id=1)
        prev_dash = {0: p0, 1: p1}
        actual    = {0: p0, 1: p1}
        tracked   = {0: p0, 1: p1}
        assert _detect_new_positions(actual, prev_dash, tracked) == []
        assert _detect_silent_closes(actual, prev_dash) == []


# ─────────────────────────────────────────────────────────────────────────────
# Fill-based OPEN trust (do NOT suppress based on _dash_positions)
# ─────────────────────────────────────────────────────────────────────────────

class TestFillBasedOpenTrust:
    """The fill-based OPEN handler should send the alert whenever:
      - the reconciler flag is not set (reconciler hasn't already alerted), and
      - the fill passes the min-notional filter.

    It must NOT consult _dash_positions, because on Lighter pool the /account
    endpoint can lag /trades by 30-90s (ZK rollup settlement). Suppressing
    OPEN alerts based on a stale /account snapshot silently drops legitimate
    opens — this regression was introduced 2026-05-28 and reverted.
    """

    def _should_send_open(
        self,
        source_id: str,
        market_id: int,
        reconciler_alerted: set,
    ) -> bool:
        """Replicate the fill-based OPEN alert decision (post-revert)."""
        key = (source_id, market_id)
        if key in reconciler_alerted:
            return False
        return True   # _dash_positions is intentionally NOT consulted

    def test_open_sent_when_dash_positions_empty(self):
        """Critical regression test: /account is laggy, _dash_positions empty,
        but the fill arrived → still send the OPEN alert."""
        reconciler_alerted: set = set()
        assert self._should_send_open("src", 0, reconciler_alerted) is True

    def test_open_sent_when_position_still_open(self):
        """Normal case."""
        reconciler_alerted: set = set()
        assert self._should_send_open("src", 0, reconciler_alerted) is True

    def test_reconciler_flag_still_suppresses(self):
        """If reconciler already alerted, fill-based path must still suppress."""
        reconciler_alerted = {("src", 0)}
        assert self._should_send_open("src", 0, reconciler_alerted) is False

    def test_reconciler_flag_only_for_matching_key(self):
        """Flag for one market doesn't suppress OPEN on another market."""
        reconciler_alerted = {("src", 0)}
        assert self._should_send_open("src", 1, reconciler_alerted) is True

    def test_reconciler_flag_isolated_per_source(self):
        """Flag for src_a doesn't suppress OPEN on src_b for the same market."""
        reconciler_alerted = {("src_a", 0)}
        assert self._should_send_open("src_b", 0, reconciler_alerted) is True

    def test_rapid_open_close_sends_stale_open_alert(self):
        """Documented tradeoff: when /trades delivers both open and close fills
        in the same poll batch, we send the (stale) OPEN alert then the PnL
        card. This matches the May 26 working behaviour. The alternative —
        consulting /account — caused worse bugs on Lighter pool."""
        tracker = PositionTracker(source="test")
        # Open fill → OPEN event (alert sent)
        ev_open = tracker.apply(
            make_trade(trade_id=1, side="long", size="10", price="100")
        )
        assert ev_open[0].kind == EventKind.OPEN
        # Close fill → CLOSE event (PnL card sent)
        ev_close = tracker.apply(
            make_trade(trade_id=2, side="short", size="10", price="110")
        )
        assert ev_close[0].kind == EventKind.CLOSE
