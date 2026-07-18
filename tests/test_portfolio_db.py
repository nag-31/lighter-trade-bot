import asyncio
import sqlite3

from src.portfolio_db import (
    aggregate_history,
    disable_address,
    get_address,
    init_portfolio_db,
    latest_snapshots,
    list_addresses,
    save_snapshot,
    set_address_fields,
    upsert_address,
)


def _run(coro):
    return asyncio.run(coro)


def test_address_upsert_persists_and_updates_label(tmp_path):
    db = tmp_path / "portfolio.db"
    _run(init_portfolio_db(db))

    row1 = _run(upsert_address(db, "0x2222222222222222222222222222222222222222", "main"))
    row2 = _run(upsert_address(db, "0x2222222222222222222222222222222222222222", "wallet"))

    assert row1["id"] == row2["id"]
    rows = _run(list_addresses(db))
    assert len(rows) == 1
    assert rows[0]["label"] == "wallet"


def test_disable_address_hides_then_upsert_reenables(tmp_path):
    db = tmp_path / "portfolio.db"
    _run(init_portfolio_db(db))

    row = _run(upsert_address(db, "0x2222222222222222222222222222222222222222", None))
    assert _run(disable_address(db, row["id"])) is True
    assert _run(list_addresses(db)) == []

    _run(upsert_address(db, "0x2222222222222222222222222222222222222222", "back"))
    rows = _run(list_addresses(db))
    assert len(rows) == 1
    assert rows[0]["enabled"] == 1
    assert rows[0]["label"] == "back"


def test_latest_snapshots_returns_newest_payload(tmp_path):
    db = tmp_path / "portfolio.db"
    _run(init_portfolio_db(db))
    row = _run(upsert_address(db, "0x2222222222222222222222222222222222222222", None))

    _run(save_snapshot(
        db,
        address_id=row["id"],
        ts="2026-01-01T00:00:00+00:00",
        status="ok",
        total_usd=10.0,
        payload={"totals": {"total_usd": 10.0}, "status": "ok"},
    ))
    _run(save_snapshot(
        db,
        address_id=row["id"],
        ts="2026-01-01T00:01:00+00:00",
        status="degraded",
        total_usd=12.5,
        payload={"totals": {"total_usd": 12.5}, "status": "degraded"},
        error="rpc slow",
    ))

    latest = _run(latest_snapshots(db))
    assert latest[row["id"]]["status"] == "degraded"
    assert latest[row["id"]]["total_usd"] == 12.5
    assert latest[row["id"]]["payload"]["totals"]["total_usd"] == 12.5


# --- excluded column + migration ------------------------------------------


def _make_old_schema_db(path):
    """Create a portfolio.db with the OLD schema (no `excluded` column) and one row."""
    con = sqlite3.connect(path)
    try:
        con.execute("""
            CREATE TABLE portfolio_addresses (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                address    TEXT    NOT NULL UNIQUE,
                label      TEXT,
                enabled    INTEGER NOT NULL DEFAULT 1,
                created_at TEXT    NOT NULL,
                updated_at TEXT    NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE portfolio_snapshots (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                address_id INTEGER NOT NULL,
                ts         TEXT    NOT NULL,
                status     TEXT    NOT NULL,
                total_usd  REAL,
                payload    TEXT    NOT NULL,
                error      TEXT
            )
        """)
        con.execute(
            "INSERT INTO portfolio_addresses (address, label, enabled, created_at, updated_at)"
            " VALUES (?, ?, 1, ?, ?)",
            ("0x2222222222222222222222222222222222222222", "legacy",
             "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        con.commit()
    finally:
        con.close()


def test_migration_adds_excluded_column_to_preexisting_db(tmp_path):
    db = tmp_path / "portfolio.db"
    _make_old_schema_db(db)

    # Column absent before migration.
    con = sqlite3.connect(db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(portfolio_addresses)").fetchall()}
    con.close()
    assert "excluded" not in cols

    _run(init_portfolio_db(db))

    con = sqlite3.connect(db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(portfolio_addresses)").fetchall()}
    con.close()
    assert "excluded" in cols

    rows = _run(list_addresses(db))
    assert len(rows) == 1
    assert rows[0]["label"] == "legacy"
    assert rows[0]["excluded"] is False  # default 0 -> bool


def test_excluded_toggle_via_set_address_fields(tmp_path):
    db = tmp_path / "portfolio.db"
    _run(init_portfolio_db(db))
    row = _run(upsert_address(db, "0x2222222222222222222222222222222222222222", "main"))
    assert row["excluded"] is False

    updated = _run(set_address_fields(db, row["id"], excluded=True))
    assert updated["excluded"] is True
    assert updated["label"] == "main"  # unchanged
    assert _run(get_address(db, row["id"]))["excluded"] is True

    # Toggle label only, excluded stays True.
    updated = _run(set_address_fields(db, row["id"], label="renamed"))
    assert updated["label"] == "renamed"
    assert updated["excluded"] is True

    # Toggle back to included.
    updated = _run(set_address_fields(db, row["id"], excluded=False))
    assert updated["excluded"] is False

    # Unknown id returns None.
    assert _run(set_address_fields(db, 99999, excluded=True)) is None


def test_all_address_dicts_include_excluded_bool(tmp_path):
    db = tmp_path / "portfolio.db"
    _run(init_portfolio_db(db))
    row = _run(upsert_address(db, "0x2222222222222222222222222222222222222222", "x"))
    assert isinstance(row["excluded"], bool)
    assert isinstance(_run(get_address(db, row["id"]))["excluded"], bool)
    assert isinstance(_run(list_addresses(db))[0]["excluded"], bool)


# --- aggregate_history carry-forward math ---------------------------------


def _snap(db, address_id, ts, total):
    _run(save_snapshot(
        db,
        address_id=address_id,
        ts=ts,
        status="ok",
        total_usd=total,
        payload={"totals": {"total_usd": total}, "status": "ok"},
    ))


def test_aggregate_history_carry_forward_interleaved(tmp_path):
    db = tmp_path / "portfolio.db"
    _run(init_portfolio_db(db))
    a = _run(upsert_address(db, "0x1111111111111111111111111111111111111111", "A"))
    b = _run(upsert_address(db, "0x2222222222222222222222222222222222222222", "B"))

    # Interleaved timestamps across two addresses.
    _snap(db, a["id"], "2026-01-01T00:00:00+00:00", 100.0)   # A=100            -> 100
    _snap(db, b["id"], "2026-01-01T00:01:00+00:00", 50.0)    # A=100, B=50      -> 150
    _snap(db, a["id"], "2026-01-01T00:02:00+00:00", 120.0)   # A=120, B=50      -> 170
    _snap(db, b["id"], "2026-01-01T00:03:00+00:00", 40.0)    # A=120, B=40      -> 160

    hist = _run(aggregate_history(db, 1000))
    assert [p["total_usd"] for p in hist] == [100.0, 150.0, 170.0, 160.0]
    assert [p["ts"] for p in hist] == [
        "2026-01-01T00:00:00+00:00",
        "2026-01-01T00:01:00+00:00",
        "2026-01-01T00:02:00+00:00",
        "2026-01-01T00:03:00+00:00",
    ]


def test_aggregate_history_excludes_excluded_and_disabled(tmp_path):
    db = tmp_path / "portfolio.db"
    _run(init_portfolio_db(db))
    a = _run(upsert_address(db, "0x1111111111111111111111111111111111111111", "A"))
    b = _run(upsert_address(db, "0x2222222222222222222222222222222222222222", "B"))
    c = _run(upsert_address(db, "0x3333333333333333333333333333333333333333", "C"))

    _snap(db, a["id"], "2026-01-01T00:00:00+00:00", 100.0)
    _snap(db, b["id"], "2026-01-01T00:01:00+00:00", 50.0)   # will be excluded
    _snap(db, c["id"], "2026-01-01T00:02:00+00:00", 30.0)   # will be disabled

    _run(set_address_fields(db, b["id"], excluded=True))
    _run(disable_address(db, c["id"]))

    hist = _run(aggregate_history(db, 1000))
    # Only A's snapshot contributes.
    assert [p["total_usd"] for p in hist] == [100.0]


def test_aggregate_history_limit_returns_last_n(tmp_path):
    db = tmp_path / "portfolio.db"
    _run(init_portfolio_db(db))
    a = _run(upsert_address(db, "0x1111111111111111111111111111111111111111", "A"))
    for i in range(5):
        _snap(db, a["id"], f"2026-01-01T00:0{i}:00+00:00", float(i * 10))

    hist = _run(aggregate_history(db, 2))
    assert [p["total_usd"] for p in hist] == [30.0, 40.0]


def test_aggregate_history_can_scope_to_selected_wallets(tmp_path):
    db = tmp_path / "portfolio.db"
    _run(init_portfolio_db(db))
    a = _run(upsert_address(db, "0x1111111111111111111111111111111111111111", "A"))
    b = _run(upsert_address(db, "0x2222222222222222222222222222222222222222", "B"))

    _snap(db, a["id"], "2026-01-01T00:00:00+00:00", 100.0)
    _snap(db, b["id"], "2026-01-01T00:01:00+00:00", 50.0)
    _snap(db, a["id"], "2026-01-01T00:02:00+00:00", 120.0)

    hist = _run(aggregate_history(db, 1000, [a["id"]]))
    assert [point["total_usd"] for point in hist] == [100.0, 120.0]
