from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from command_center.app import bootstrap, create_app
from command_center.lifecycles import reconstruct_lifecycles
from command_center.store import CommandStore, fingerprint, iso


def test_dialog_cancel_controls_never_submit_forms() -> None:
    static = Path(__file__).parents[1] / "command_center" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    script = (static / "app.js").read_text(encoding="utf-8")
    assert html.count('type="button" class="secondary-button" data-dialog-close=') == 2
    assert html.count('type="button" class="icon-button" aria-label="Close" data-dialog-close=') == 2
    assert '$$("[data-dialog-close]")' in script
    assert 'id="reasonGroups"' in html
    assert 'data-journal-trade' not in script
    assert 'data-trade-journal-link' in html


def test_bootstrap_page_load_is_read_only() -> None:
    import inspect

    source = inspect.getsource(bootstrap)
    assert "await _sync(request)" not in source
    assert "store.summary()" in source


def test_signal_research_does_not_ship_duplicate_journal_surface() -> None:
    static = Path(__file__).parents[1] / "command_center" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    script = (static / "app.js").read_text(encoding="utf-8")
    assert 'id="journalView"' not in html
    assert 'id="linkDialog"' not in html
    assert "function renderJournal" not in script
    assert "function renderDecisionTable" not in script
    assert "data-decision-sort" not in html
    assert "Open Trade Journal" in html


def test_store_links_signal_decision_trade_and_reports_edge(tmp_path: Path) -> None:
    store = CommandStore(tmp_path / "command.db")
    store.init()
    signal_id, created = store.upsert_signal(
        {
            "fingerprint": fingerprint("test-signal"),
            "source": "speculation",
            "source_ref": "1",
            "detector": "range-break",
            "event_type": "market_signal",
            "occurred_at": "2026-07-20T00:00:00+00:00",
            "symbol": "BTC/USDT",
            "direction": "long",
            "title": "BTC breakout",
            "summary": "Range high cleared.",
            "severity": "high",
            "confidence": 0.8,
        }
    )
    assert created
    decision = store.create_decision(
        {
            "signal_id": signal_id,
            "thesis": "Momentum should continue.",
            "direction": "long",
            "invalidation": 60000,
            "target": 68000,
            "max_risk_usd": 100,
            "confidence": 70,
        }
    )
    trade_id, created = store.upsert_trade(
        {
            "fingerprint": fingerprint("test-trade"),
            "source": "HL",
            "native_trade_id": "abc",
            "occurred_at": "2026-07-20T01:00:00+00:00",
            "symbol": "BTC",
            "side": "long",
            "entry": 62000,
            "exit": 64000,
            "size": 0.1,
            "notional": 6200,
            "pnl": 200,
            "pnl_pct": 3.2,
            "is_win": 1,
        }
    )
    assert created
    linked = store.link_trade(decision["id"], trade_id)
    assert len(linked["trades"]) == 1
    assert store.complete_outcome(
        signal_id, 1440, baseline=62000, outcome=65000, mfe=6.0, mae=-1.0,
        captured_at="2026-07-21T00:00:00+00:00",
    )
    edge = store.edge_report()
    assert edge["strategies"][0]["samples"] == 1
    assert edge["strategies"][0]["hit_rate"] == 100
    summary = store.summary()
    assert summary["trades"]["realized_pnl"] == 200
    assert summary["risk"]["committed"] == 100
    store.replace_positions(
        [{
            "position_key": "HL:BTC", "source": "HL", "symbol": "BTC",
            "side": "long", "size": 0.1, "entry": 62000,
            "unrealized_pnl": 50, "liquidation_price": 40000,
            "updated_at": iso(), "metadata": {},
        }]
    )
    assert store.summary()["positions"]["fresh_count"] == 1


def test_execution_fills_become_one_lifecycle_with_partial_profit(
    tmp_path: Path,
) -> None:
    def event(
        event_id: int, ts: str, trade_id: int, *, kind: str, direction: str,
        size: float, price: float, before: float | None, after: float | None,
        pnl: float = 0,
    ) -> dict:
        side = "long"
        return {
            "id": event_id,
            "ts": ts,
            "payload": __import__("json").dumps(
                {
                    "kind": kind,
                    "trade": {
                        "trade_id": trade_id, "timestamp": ts, "source": "HL",
                        "market_symbol": "BTC", "side": side, "size": str(size),
                        "price": str(price), "dir": direction,
                        "realized_pnl": str(pnl),
                    },
                    "position_before": (
                        None if before is None else {
                            "source": "HL", "market_symbol": "BTC", "side": side,
                            "size": str(before), "avg_entry_price": "100",
                        }
                    ),
                    "position_after": (
                        None if after is None else {
                            "source": "HL", "market_symbol": "BTC", "side": side,
                            "size": str(after), "avg_entry_price": "100",
                        }
                    ),
                }
            ),
        }

    rows = [
        event(1, "2026-07-20T10:00:00+00:00", 11, kind="OPEN",
              direction="Open Long", size=1, price=100, before=None, after=1),
        event(2, "2026-07-20T10:00:01+00:00", 12, kind="SIZE_CHANGE",
              direction="Open Long", size=2, price=101, before=1, after=3),
        event(3, "2026-07-20T10:10:00+00:00", 13, kind="REDUCE",
              direction="Close Long", size=1, price=110, before=3, after=2, pnl=9.33),
        event(4, "2026-07-20T10:10:01+00:00", 14, kind="REDUCE",
              direction="Close Long", size=.5, price=111, before=2, after=1.5, pnl=5.17),
        event(5, "2026-07-20T10:40:00+00:00", 15, kind="CLOSE",
              direction="Close Long", size=1.5, price=108, before=1.5, after=None, pnl=11),
        # A replayed duplicate of fill 15 must not be counted twice.
        event(6, "2026-07-20T10:40:00+00:00", 15, kind="SIZE_CHANGE",
              direction="Close Long", size=1.5, price=108, before=0, after=1.5, pnl=11),
    ]
    lifecycles = reconstruct_lifecycles(rows)
    assert len(lifecycles) == 1
    trade = lifecycles[0]
    assert trade["fill_count"] == 5
    assert trade["entry_batch_count"] == 1
    assert trade["exit_batch_count"] == 2
    assert trade["partial_exit_count"] == 1
    assert trade["management_style"] == "Scaled out"
    assert [batch["label"] for batch in trade["batches"]] == [
        "Entry", "Partial profit", "Final exit"
    ]
    assert trade["pnl"] == pytest.approx(25.5)
    open_trade = reconstruct_lifecycles(rows[:4])[0]
    assert open_trade["status"] == "open"
    assert open_trade["partial_exit_count"] == 1
    assert open_trade["management_style"] == "Scaled out"
    readded = reconstruct_lifecycles(
        rows[:4] + [
            event(7, "2026-07-20T10:20:00+00:00", 17, kind="SIZE_CHANGE",
                  direction="Open Long", size=1, price=106,
                  before=1.5, after=2.5)
        ]
    )[0]
    assert readded["management_style"] == "Scaled in & out"

    store = CommandStore(tmp_path / "command.db")
    store.init()
    assert store.replace_trade_lifecycles(lifecycles) == 1
    listed = store.list_trades()
    assert len(listed) == 1
    assert listed[0]["fill_count"] == 5
    assert len(listed[0]["executions"]) == 5
    assert store.summary()["trades"]["count"] == 1
    decision = store.create_decision(
        {"thesis": "Lifecycle journal", "direction": "long", "status": "closed"}
    )
    linked = store.link_lifecycle(decision["id"], listed[0]["id"])
    assert len(linked["trades"]) == 1
    assert linked["trades"][0]["is_lifecycle"] is True
    evaluation = store.lifecycle_evaluation()
    assert evaluation["status"] == "pass"
    assert evaluation["score"] == 100
    assert evaluation["failed"] == 0


def test_reduce_and_close_state_override_stale_direction_text() -> None:
    def row(event_id: int, kind: str, before: float, after: float | None) -> dict:
        timestamp = f"2026-07-20T10:{event_id * 3:02d}:00+00:00"
        return {
            "id": event_id,
            "ts": timestamp,
            "payload": __import__("json").dumps(
                {
                    "kind": kind,
                    "trade": {
                        "trade_id": event_id,
                        "timestamp": timestamp,
                        "source": "HL", "market_symbol": "ETH",
                        "side": "short", "size": str(before - (after or 0)),
                        "price": "110", "dir": "Open Long",
                        "realized_pnl": "10",
                    },
                    "position_before": {
                        "source": "HL", "market_symbol": "ETH", "side": "long",
                        "size": str(before), "avg_entry_price": "100",
                    },
                    "position_after": (
                        None if after is None else {
                            "source": "HL", "market_symbol": "ETH", "side": "long",
                            "size": str(after), "avg_entry_price": "100",
                        }
                    ),
                }
            ),
        }

    trades = reconstruct_lifecycles(
        [row(1, "REDUCE", 2, 1), row(2, "CLOSE", 1, None)]
    )
    assert len(trades) == 1
    assert trades[0]["status"] == "closed"
    assert trades[0]["side"] == "long"
    assert [item["action"] for item in trades[0]["executions"]] == ["exit", "exit"]
    assert [batch["label"] for batch in trades[0]["batches"]] == [
        "Partial profit", "Final exit"
    ]

    reversal_rows = [
        {
            "id": 10,
            "ts": "2026-07-20T11:00:00+00:00",
            "payload": __import__("json").dumps(
                {
                    "kind": "OPEN",
                    "trade": {
                        "trade_id": 10, "timestamp": "2026-07-20T11:00:00+00:00",
                        "source": "HL", "market_symbol": "SOL", "side": "short",
                        "size": "2", "price": "100", "dir": "Open Short",
                    },
                    "position_before": None,
                    "position_after": {
                        "source": "HL", "market_symbol": "SOL", "side": "short",
                        "size": "2", "avg_entry_price": "100",
                    },
                }
            ),
        },
        {
            "id": 11,
            "ts": "2026-07-20T12:00:00+00:00",
            "payload": __import__("json").dumps(
                {
                    "kind": "OPEN",
                    "trade": {
                        "trade_id": 11, "timestamp": "2026-07-20T12:00:00+00:00",
                        "source": "HL", "market_symbol": "SOL", "side": "long",
                        "size": "3", "price": "105", "dir": "Open Long",
                    },
                    "position_before": {
                        "source": "HL", "market_symbol": "SOL", "side": "short",
                        "size": "2", "avg_entry_price": "100",
                    },
                    "position_after": {
                        "source": "HL", "market_symbol": "SOL", "side": "long",
                        "size": "1", "avg_entry_price": "105",
                    },
                }
            ),
        },
    ]
    reversed_trades = reconstruct_lifecycles(reversal_rows)
    assert [trade["status"] for trade in reversed_trades] == ["reversed", "open"]
    assert reversed_trades[0]["batches"][-1]["label"] == "Direction reversal → long"


def test_hyperliquid_start_position_repairs_corrupt_short_close_state() -> None:
    def row(
        event_id: int,
        trade_id: int,
        *,
        timestamp: str,
        kind: str,
        size: str,
        price: str,
        pnl: str,
        start: str,
        direction: str,
        cached_side: str,
        cached_before: str,
        cached_after: str | None,
        cached_entry: str,
    ) -> dict:
        before = {
            "source": "HL", "market_symbol": "XYZ:SNDK",
            "side": cached_side, "size": cached_before,
            "avg_entry_price": cached_entry,
        }
        after = None if cached_after is None else {
            "source": "HL", "market_symbol": "XYZ:SNDK",
            "side": cached_side, "size": cached_after,
            "avg_entry_price": cached_entry,
        }
        return {
            "id": event_id,
            "ts": timestamp,
            "payload": __import__("json").dumps(
                {
                    "kind": kind,
                    "trade": {
                        "trade_id": trade_id, "timestamp": timestamp,
                        "source": "HL", "market_symbol": "XYZ:SNDK",
                        "side": "long", "size": size, "price": price,
                        "dir": direction, "realized_pnl": pnl,
                        "closed_pnl": pnl, "start_position": start,
                    },
                    "position_before": before,
                    "position_after": after,
                }
            ),
        }

    first_ts = "2026-07-02T10:47:21.091000+00:00"
    final_ts = "2026-07-28T14:54:23.612000+00:00"
    rows = [
        row(
            273, 1071184034449342, timestamp=first_ts, kind="REDUCE",
            size="0.168", price="1963.1", pnl="42.539784", start="-0.464",
            direction="Close Short", cached_side="short",
            cached_before="0.210", cached_after="0.042", cached_entry="2216.5",
        ),
        # This replay has contradictory long state, but the signed
        # start_position and realized PnL remain authoritative.
        row(
            595, 572929433187849, timestamp=first_ts, kind="SIZE_CHANGE",
            size="0.118", price="1963.1", pnl="29.879134", start="-0.296",
            direction="Close Short", cached_side="long",
            cached_before="0.251", cached_after="0.369", cached_entry="1963.1",
        ),
        row(
            1430, 1029412882611334, timestamp=final_ts, kind="CLOSE",
            size="4.493", price="1110.3", pnl="196.870314", start="-0.178",
            direction="Short > Long", cached_side="short",
            cached_before="0.178", cached_after=None, cached_entry="2216.313",
        ),
        {
            "id": 1431,
            "ts": final_ts,
            "payload": __import__("json").dumps(
                {
                    "kind": "OPEN",
                    "trade": {
                        "trade_id": 1029412882611334, "timestamp": final_ts,
                        "source": "HL", "market_symbol": "XYZ:SNDK",
                        "side": "long", "size": "4.493", "price": "1110.3",
                        "dir": "Short > Long", "realized_pnl": "196.870314",
                        "start_position": "-0.178",
                    },
                    "position_before": None,
                    "position_after": {
                        "source": "HL", "market_symbol": "XYZ:SNDK",
                        "side": "long", "size": "4.315",
                        "avg_entry_price": "1110.3",
                    },
                }
            ),
        },
    ]

    trades = reconstruct_lifecycles(rows)
    assert len(trades) == 2
    closed, opened = trades
    assert closed["side"] == "short"
    assert closed["status"] == "closed"
    assert closed["inferred_open"] is True
    assert closed["entry_vwap"] == pytest.approx(2216.313)
    assert closed["max_size"] == pytest.approx(0.464)
    assert closed["closed_size"] == pytest.approx(0.464)
    assert closed["pnl"] == pytest.approx(269.289232)
    assert [item["position_before"] for item in closed["executions"]] == pytest.approx(
        [0.464, 0.296, 0.178]
    )
    assert [item["position_after"] for item in closed["executions"]] == pytest.approx(
        [0.296, 0.178, 0]
    )
    assert opened["side"] == "long"
    assert opened["status"] == "open"
    assert opened["max_size"] == pytest.approx(4.315)
    assert opened["pnl"] is None


def test_hyperliquid_direction_repairs_zero_pnl_scale_in_kind() -> None:
    def event(
        event_id: int, *, kind: str, direction: str, start: str,
        size: str, pnl: str,
    ) -> dict:
        timestamp = f"2026-07-20T13:0{event_id}:00+00:00"
        return {
            "id": event_id,
            "ts": timestamp,
            "payload": __import__("json").dumps(
                {
                    "kind": kind,
                    "trade": {
                        "trade_id": event_id, "timestamp": timestamp,
                        "source": "HL", "market_symbol": "ETH",
                        "side": "short" if direction.startswith("Open") else "long",
                        "size": size, "price": "100", "dir": direction,
                        "realized_pnl": pnl, "start_position": start,
                    },
                    # Deliberately contradictory historical cache.
                    "position_before": {
                        "source": "HL", "market_symbol": "ETH",
                        "side": "long", "size": "0.5", "avg_entry_price": "100",
                    },
                    "position_after": None,
                }
            ),
        }

    trades = reconstruct_lifecycles(
        [
            event(
                1, kind="REDUCE", direction="Open Short",
                start="-1", size="0.5", pnl="0",
            ),
            event(
                2, kind="CLOSE", direction="Close Short",
                start="-1.5", size="1.5", pnl="15",
            ),
        ]
    )
    assert len(trades) == 1
    assert trades[0]["side"] == "short"
    assert trades[0]["status"] == "closed"
    assert [item["action"] for item in trades[0]["executions"]] == ["entry", "exit"]
    assert trades[0]["max_size"] == pytest.approx(1.5)


def test_open_linked_trade_drives_active_journal_status_and_live_pnl(
    tmp_path: Path,
) -> None:
    store = CommandStore(tmp_path / "command.db")
    store.init()
    lifecycle = {
        "lifecycle_key": "open-btc",
        "source": "HL",
        "symbol": "BTC",
        "side": "long",
        "opened_at": "2026-07-20T10:00:00+00:00",
        "closed_at": None,
        "status": "open",
        "entry_vwap": 100.0,
        "exit_vwap": None,
        "max_size": 2.0,
        "closed_size": 0.0,
        "notional": 200.0,
        "pnl": None,
        "pnl_pct": None,
        "is_win": None,
        "fill_count": 1,
        "entry_batch_count": 1,
        "exit_batch_count": 0,
        "partial_exit_count": 0,
        "management_style": "Single entry / exit",
        "batches": [],
        "executions": [],
    }
    store.replace_trade_lifecycles([lifecycle])
    store.replace_positions(
        [
            {
                "position_key": "hl:btc:long",
                "source": "HL",
                "symbol": "BTC",
                "side": "long",
                "size": 2,
                "entry": 100,
                "unrealized_pnl": 37.5,
                "liquidation_price": 60,
                "updated_at": "2026-07-20T10:05:00+00:00",
            }
        ]
    )
    decision = store.create_decision(
        {"thesis": "Open BTC", "direction": "long", "status": "closed"}
    )
    trade = store.list_trades()[0]
    store.link_lifecycle(decision["id"], trade["id"])

    listed = store.list_decisions()
    assert listed[0]["status"] == "closed"
    assert listed[0]["effective_status"] == "active"
    assert listed[0]["unrealized_pnl"] == pytest.approx(37.5)
    assert listed[0]["display_pnl"] == pytest.approx(37.5)


def test_open_linked_trade_does_not_invent_zero_when_live_mark_is_missing(
    tmp_path: Path,
) -> None:
    store = CommandStore(tmp_path / "command.db")
    store.init()
    lifecycle = {
        "lifecycle_key": "open-unmarked",
        "source": "HL",
        "symbol": "ETH",
        "side": "short",
        "opened_at": "2026-07-20T10:00:00+00:00",
        "closed_at": None,
        "status": "open",
        "entry_vwap": 100.0,
        "exit_vwap": None,
        "max_size": 2.0,
        "closed_size": 0.0,
        "notional": 200.0,
        "pnl": None,
        "pnl_pct": None,
        "is_win": None,
        "fill_count": 1,
        "entry_batch_count": 1,
        "exit_batch_count": 0,
        "partial_exit_count": 0,
        "management_style": "Single entry / exit",
        "batches": [],
        "executions": [],
    }
    store.replace_trade_lifecycles([lifecycle])
    decision = store.create_decision(
        {"thesis": "Open ETH", "direction": "short", "status": "active"}
    )
    store.link_lifecycle(decision["id"], store.list_trades()[0]["id"])

    listed = store.list_decisions()[0]
    assert listed["effective_status"] == "active"
    assert listed["unrealized_pnl"] is None
    assert listed["display_pnl"] is None


def test_lifecycle_listing_has_one_row_even_with_multiple_journal_links(
    tmp_path: Path,
) -> None:
    store = CommandStore(tmp_path / "command.db")
    store.init()
    lifecycle = {
        "lifecycle_key": "one-card",
        "source": "HL",
        "symbol": "BTC",
        "side": "long",
        "opened_at": "2026-07-20T10:00:00+00:00",
        "closed_at": "2026-07-20T11:00:00+00:00",
        "status": "closed",
        "entry_vwap": 100.0,
        "exit_vwap": 110.0,
        "max_size": 1.0,
        "closed_size": 1.0,
        "notional": 100.0,
        "pnl": 10.0,
        "pnl_pct": 10.0,
        "is_win": 1,
        "fill_count": 2,
        "entry_batch_count": 1,
        "exit_batch_count": 1,
        "partial_exit_count": 0,
        "management_style": "Single entry / exit",
        "batches": [],
        "executions": [],
    }
    store.replace_trade_lifecycles([lifecycle])
    lifecycle_id = store.list_trades()[0]["id"]
    for index in range(2):
        decision = store.create_decision(
            {"thesis": f"Review {index}", "direction": "long"}
        )
        store.link_lifecycle(decision["id"], lifecycle_id)

    assert len(store.list_trades()) == 1


def test_journal_writes_use_a_physically_separate_database(tmp_path: Path) -> None:
    import sqlite3

    command_db = tmp_path / "command.db"
    journal_db = tmp_path / "trading_journal.db"
    store = CommandStore(command_db)
    store.init()
    store.create_decision({"thesis": "Separated ledger", "direction": "short"})

    assert journal_db.exists()
    with sqlite3.connect(journal_db) as con:
        assert con.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
    with sqlite3.connect(command_db) as con:
        # The legacy table remains as a frozen migration source, but new
        # journal writes no longer enter the market-intelligence database.
        assert con.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0


def test_tvl_signal_copies_are_removed_from_command_center(tmp_path: Path) -> None:
    store = CommandStore(tmp_path / "command.db")
    store.init()
    for source in ("hack", "speculation"):
        store.upsert_signal(
            {
                "fingerprint": fingerprint(source),
                "source": source,
                "event_type": "defi_risk" if source == "hack" else "market_signal",
                "occurred_at": "2026-07-20T00:00:00+00:00",
                "direction": "short",
                "title": source,
                "summary": "",
            }
        )

    assert store.purge_signal_source("hack") == 1
    assert [item["source"] for item in store.list_signals()] == ["speculation"]


def test_signal_research_sync_does_not_write_trade_journal() -> None:
    import inspect

    from command_center.ingest import WorkspaceIngestor

    source = inspect.getsource(WorkspaceIngestor.sync)
    assert "self._trades()" not in source
    assert "self._lifecycles()" not in source
    assert "self._positions()" not in source
    assert "self._speculation_signals()" in source

    trading_source = inspect.getsource(WorkspaceIngestor.sync_trading)
    assert "self._trades()" in trading_source
    assert "self._lifecycles()" in trading_source
    assert "self._positions()" in trading_source


def test_counterfactual_ignored_signal_is_in_weekly_review(tmp_path: Path) -> None:
    store = CommandStore(tmp_path / "command.db")
    store.init()
    signal_id, _ = store.upsert_signal(
        {
            "fingerprint": fingerprint("ignored"),
            "source": "speculation",
            "event_type": "market_signal",
            "occurred_at": iso(),
            "symbol": "ETH/USDT",
            "direction": "short",
            "title": "ETH breakdown",
            "summary": "Support failed.",
            "severity": "medium",
        }
    )
    store.set_signal_status(signal_id, "ignored")
    assert store.complete_outcome(
        signal_id, 1440, baseline=3000, outcome=2700, mfe=12, mae=-2,
        captured_at=iso(),
    )
    review = store.weekly_review()
    assert review["best_ignored"]["symbol"] == "ETH/USDT"
    assert review["best_ignored"]["signed_return_pct"] > 0


def test_simulations_are_visible_but_excluded_from_production_edge(tmp_path: Path) -> None:
    store = CommandStore(tmp_path / "command.db")
    store.init()
    production_id, _ = store.upsert_signal(
        {
            "fingerprint": fingerprint("production"),
            "source": "speculation",
            "detector": "return-zscore",
            "event_type": "market_signal",
            "occurred_at": iso(),
            "symbol": "BTC/USDT",
            "direction": "long",
            "title": "Production breakout",
            "summary": "Real detector.",
            "severity": "high",
        }
    )
    simulation_id, _ = store.upsert_signal(
        {
            "fingerprint": fingerprint("simulation"),
            "source": "speculation",
            "detector": "sample-breakout",
            "event_type": "market_signal",
            "occurred_at": iso(),
            "symbol": "ETH/USDT",
            "direction": "short",
            "title": "Sample breakdown",
            "summary": "Synthetic detector.",
            "severity": "critical",
            "is_simulation": True,
        }
    )
    assert store.complete_outcome(
        production_id, 1440, baseline=100, outcome=110, mfe=12, mae=-2,
        captured_at=iso(),
    )
    assert store.complete_outcome(
        simulation_id, 1440, baseline=100, outcome=50, mfe=50, mae=-1,
        captured_at=iso(),
    )
    signals = store.list_signals()
    simulated = next(item for item in signals if item["id"] == simulation_id)
    production = next(item for item in signals if item["id"] == production_id)
    assert simulated["freshness"] == "simulation"
    assert simulated["priority_score"] < production["priority_score"]
    summary = store.summary()
    assert summary["signal_quality"]["simulation_count"] == 1
    assert summary["signal_quality"]["production_count"] == 1
    assert summary["edge"]["sample_size"] == 1
    assert summary["edge"]["average_edge_24h"] == pytest.approx(10)
    edge = store.edge_report()
    assert edge["excluded_simulations"]["signals"] == 1
    assert all(item["strategy"] != "sample-breakout" for item in edge["strategies"])
    assert store.weekly_review()["signals"]["total"] == 1


def test_structured_journal_reasons_include_presets_and_custom_values(
    tmp_path: Path,
) -> None:
    store = CommandStore(tmp_path / "command.db")
    store.init()
    taxonomy = store.list_reasons()
    category_names = [group["name"] for group in taxonomy["categories"]]
    assert category_names[:5] == [
        "Setup", "Trigger", "Market context", "Execution", "Psychology"
    ]
    assert taxonomy["total"] >= 25
    breakout = next(
        reason
        for group in taxonomy["categories"]
        for reason in group["reasons"]
        if reason["label"] == "Breakout"
    )
    custom = store.create_reason("Trigger", "Whale wallet accumulation")
    assert custom["is_custom"] == 1
    assert store.create_reason("Trigger", "  whale wallet accumulation  ")["id"] == custom["id"]
    decision = store.create_decision(
        {
            "thesis": "BTC strength should continue",
            "direction": "long",
            "reason_ids": [breakout["id"], custom["id"]],
        }
    )
    assert {reason["label"] for reason in decision["reasons"]} == {
        "Breakout", "Whale wallet accumulation"
    }
    listed = store.list_decisions()
    assert "Breakout" in listed[0]["reason_labels"]
    assert "Whale wallet accumulation" in listed[0]["reason_labels"]
    execution = next(
        reason
        for group in store.list_reasons()["categories"]
        for reason in group["reasons"]
        if reason["label"] == "Planned entry"
    )
    updated = store.update_decision(
        decision["id"], {"reason_ids": [execution["id"]]}
    )
    assert [reason["label"] for reason in updated["reasons"]] == ["Planned entry"]


@pytest.mark.asyncio
async def test_command_center_api_bootstrap(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = create_app(db_path=tmp_path / "command.db", workspace_root=workspace)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/api/bootstrap")
        assert response.status == 200
        payload = await response.json()
        assert {"summary", "signals", "reasons", "edge", "weekly", "settings"} <= payload.keys()
        assert "decisions" not in payload
        assert "trades" not in payload
        custom = await client.post(
            "/api/reasons",
            json={"category": "Psychology", "label": "Patient execution"},
        )
        assert custom.status == 201
        created_reason = await custom.json()
        decision = await client.post(
            "/api/decisions",
            json={
                "thesis": "Standalone research thesis",
                "direction": "neutral",
                "reason_ids": [created_reason["id"]],
            },
        )
        assert decision.status == 201
        created = await decision.json()
        assert created["thesis"] == "Standalone research thesis"
        assert created["reasons"][0]["label"] == "Patient execution"
def test_positions_expose_current_mark_and_usd_value_without_changing_accounting_size(
    tmp_path: Path,
) -> None:
    store = CommandStore(tmp_path / "command.db")
    store.init()
    store.replace_positions([
        {
            "position_key": "hl:btc:long",
            "source": "HL",
            "symbol": "BTC",
            "side": "long",
            "size": 2,
            "entry": 100,
            "unrealized_pnl": 20,
            "liquidation_price": 60,
            "updated_at": "2026-07-20T10:05:00+00:00",
        }
    ])
    listed = store.list_positions()[0]
    assert listed["current_price"] == pytest.approx(110)
    assert listed["position_value"] == pytest.approx(220)


def test_journal_positions_merge_fresh_exchange_marks_from_dashboard_snapshot(
    tmp_path: Path,
) -> None:
    from command_center.ingest import WorkspaceIngestor

    workspace = tmp_path / "workspace"
    events_path = workspace / "lighter-trade-bot" / "data" / "events.db"
    events_path.parent.mkdir(parents=True)
    with sqlite3.connect(events_path) as con:
        con.execute("CREATE TABLE events (id INTEGER, ts TEXT, payload TEXT)")
        timestamp = iso()
        con.execute(
            "INSERT INTO events VALUES (?, ?, ?)",
            (
                1,
                timestamp,
                json.dumps(
                    {
                        "kind": "OPEN",
                        "trade": {"source": "HL", "market_symbol": "BTC"},
                        "position_after": {
                            "source": "HL", "market_symbol": "BTC",
                            "side": "long", "size": "1", "avg_entry_price": "100",
                        },
                    }
                ),
            ),
        )
        con.execute(
            "INSERT INTO events VALUES (?, ?, ?)",
            (
                2,
                timestamp,
                json.dumps(
                    {
                        "kind": "OPEN",
                        "trade": {"source": "HL", "market_symbol": "ETH"},
                        "position_after": {
                            "source": "HL", "market_symbol": "ETH",
                            "side": "short", "size": "2", "avg_entry_price": "2000",
                        },
                    }
                ),
            ),
        )

    (events_path.parent / "live_positions.json").write_text(
        json.dumps(
            {
                "version": 1,
                "captured_at": timestamp,
                "positions": [
                    {
                        "source": "HL", "symbol": "BTC", "side": "long",
                        "size": "1", "entry": "100", "unrealized_pnl": "10",
                        "updated_at": timestamp,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    store = CommandStore(tmp_path / "command.db")
    store.init()
    ingestor = WorkspaceIngestor(store, workspace)
    assert ingestor._positions() == 1
    listed = store.list_positions()[0]
    assert listed["unrealized_pnl"] == pytest.approx(10)
    assert listed["current_price"] == pytest.approx(110)
    assert listed["position_value"] == pytest.approx(110)
