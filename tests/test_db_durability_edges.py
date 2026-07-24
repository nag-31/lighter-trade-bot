from __future__ import annotations

import asyncio
import sqlite3

import pytest

from src.db import (
    enqueue_notification,
    init_db,
    load_recorded_trade_uids,
    load_source_cursor,
    load_tg_alerts,
    mark_notification,
    notification_status,
    save_source_cursor,
    save_tg_alert,
)


def _run(coro):
    return asyncio.run(coro)


def test_trade_uid_loader_handles_event_kinds_and_malformed_legacy_rows(tmp_path):
    db = tmp_path / "events.db"
    _run(init_db(db))
    con = sqlite3.connect(db)
    con.executemany(
        "INSERT INTO events (ts, payload, event_uid) VALUES (?, ?, ?)",
        [
            ("1", "{}", "source|1|BOTH|2|OPEN"),
            ("2", "{}", "source|1|BOTH|2|CLOSE"),
            ("3", "{}", "already-normalized"),
            ("4", "{}", "not-a-known-kind|OTHER"),
            ("5", "{}", ""),
        ],
    )
    con.commit()
    con.close()

    assert _run(load_recorded_trade_uids(db)) == {
        "source|1|BOTH|2",
        "already-normalized",
        "not-a-known-kind|OTHER",
    }


def test_source_cursor_upsert_is_scoped_by_source_and_key(tmp_path):
    db = tmp_path / "events.db"
    _run(init_db(db))

    _run(save_source_cursor(db, "hl-a", "trades", "10", "2026-01-01T00:00:00Z"))
    _run(save_source_cursor(db, "hl-a", "trades", "12", "2026-01-01T00:01:00Z"))
    _run(save_source_cursor(db, "hl-a", "positions", "7", "2026-01-01T00:01:00Z"))
    _run(save_source_cursor(db, "hl-b", "trades", "99", "2026-01-01T00:01:00Z"))

    assert _run(load_source_cursor(db, "hl-a", "trades")) == "12"
    assert _run(load_source_cursor(db, "hl-a", "positions")) == "7"
    assert _run(load_source_cursor(db, "hl-b", "trades")) == "99"
    assert _run(load_source_cursor(db, "missing", "trades")) is None


def test_notification_outbox_is_idempotent_and_counts_attempts(tmp_path):
    db = tmp_path / "events.db"
    _run(init_db(db))
    assert _run(enqueue_notification(db, "event-1", "telegram", "first", "t1")) is True
    assert _run(enqueue_notification(db, "event-1", "telegram", "second", "t2")) is False

    _run(mark_notification(db, "event-1", "failed", "t3", "timeout"))
    _run(mark_notification(db, "event-1", "sent", "t4"))

    assert _run(notification_status(db, "event-1")) == "sent"
    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT destination, payload, attempts, last_error FROM notification_outbox WHERE event_uid=?",
        ("event-1",),
    ).fetchone()
    con.close()
    assert row == ("telegram", "first", 2, None)
    assert _run(notification_status(db, "missing")) is None


def test_failed_notification_retry_is_atomically_reclaimed(tmp_path):
    db = tmp_path / "events.db"
    _run(init_db(db))
    assert _run(enqueue_notification(
        db, "event-1", "telegram", "first", "2026-01-01T00:00:00+00:00"
    )) is True
    _run(mark_notification(
        db, "event-1", "failed", "2026-01-01T00:00:01+00:00", "rejected"
    ))

    # A retry takes ownership and refreshes the payload/error state. A second
    # concurrent claimant sees pending and must not send another copy.
    assert _run(enqueue_notification(
        db, "event-1", "telegram", "retry", "2026-01-01T00:00:02+00:00"
    )) is True
    assert _run(enqueue_notification(
        db, "event-1", "telegram", "retry-again", "2026-01-01T00:00:03+00:00"
    )) is False

    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT payload, status, attempts, last_error FROM notification_outbox "
        "WHERE event_uid=?", ("event-1",)
    ).fetchone()
    con.close()
    assert row == ("retry", "pending", 1, None)


def test_stale_pending_notification_lease_can_be_reclaimed(tmp_path):
    db = tmp_path / "events.db"
    _run(init_db(db))
    assert _run(enqueue_notification(
        db, "event-1", "telegram", "first", "2026-01-01T00:00:00+00:00"
    )) is True
    assert _run(enqueue_notification(
        db, "event-1", "telegram", "retry", "2026-01-01T00:06:00+00:00"
    )) is True


def test_notification_status_rejects_unknown_values(tmp_path):
    db = tmp_path / "events.db"
    _run(init_db(db))

    with pytest.raises(ValueError, match="invalid notification status"):
        _run(mark_notification(db, "event-1", "delivered", "now"))


def test_tg_alerts_persist_newest_first_and_preserve_multiline_text(tmp_path):
    db = tmp_path / "events.db"
    _run(init_db(db))
    _run(save_tg_alert(db, "t1", "text", "first\nline"))
    _run(save_tg_alert(db, "t2", "card", "second"))

    alerts = _run(load_tg_alerts(db, limit=10))

    assert [alert["ts"] for alert in alerts] == ["t2", "t1"]
    assert alerts[1]["text"] == "first\nline"
