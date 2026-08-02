from __future__ import annotations

import inspect
from pathlib import Path

from trade_journal.app import bootstrap, create_app
from trade_journal.v2_consumer import build_v2_lifecycles


ROOT = Path(__file__).parents[1]
STATIC = ROOT / "trade_journal" / "static"


def test_trade_journal_bootstrap_is_read_only() -> None:
    source = inspect.getsource(bootstrap)
    assert "await _sync(request)" not in source
    assert "store.list_trades()" in source
    assert "store.list_decisions()" in source


def test_trade_journal_is_a_standalone_app_with_explicit_sync(tmp_path: Path) -> None:
    app = create_app(
        command_db=tmp_path / "command_center.db",
        journal_db=tmp_path / "trading_journal.db",
        workspace_root=tmp_path,
    )
    routes = {
        (route.method, route.resource.canonical)
        for route in app.router.routes()
        if route.method != "HEAD"
    }
    assert ("GET", "/api/bootstrap") in routes
    assert ("POST", "/api/sync") in routes
    assert ("POST", "/api/decisions") in routes
    assert ("PATCH", "/api/decisions/{decision_id}") in routes
    assert ("GET", "/api/v2/bootstrap") in routes


def test_trade_journal_ui_supports_status_filters_sorting_and_reason_edits() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    style = (STATIC / "style.css").read_text(encoding="utf-8")

    assert 'value="open">Active<' in html
    assert 'id="tradeJournalState"' in html
    for key in ("updated", "asset", "thesis", "side", "status", "result"):
        assert f'data-sort="{key}"' in html
    assert "Edit reasons &amp; notes" in script
    assert 'method: "PATCH"' in script
    assert "page load is read-only" in script
    assert ".trade-card.long::before" in style
    assert ".trade-card.short::before" in style


def test_trade_journal_perp_cards_show_usd_mark_and_position_value_not_size() -> None:
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "CURRENT" in script
    assert "POSITION VALUE" in script
    assert "MAX SIZE" not in script
    assert 'marker.quantity)} qty' not in script
    assert 'number.format(numeric(row.size))' not in script


def test_execution_tape_labels_transaction_side_and_size_effect() -> None:
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "executionEffect" in script
    assert "executionTransactionSide" in script
    assert '"increased size"' in script
    assert '"decreased size"' in script
    assert "executionLabel(batch, trade, index)" in script
    assert "executionLabel(marker, selected)" in script


def test_v2_read_model_preserves_long_and_short_transaction_semantics() -> None:
    rows = build_v2_lifecycles([
        {
            "id": 7,
            "is_lifecycle": True,
            "source": "HL",
            "symbol": "BTC",
            "side": "long",
            "status": "closed",
            "opened_at": "2026-08-02T12:00:00+00:00",
            "closed_at": "2026-08-02T12:05:00+00:00",
            "entry_vwap": "100",
            "exit_vwap": "110",
            "max_size": "1",
            "pnl": "10",
            "executions": [
                {"execution_key": "open", "occurred_at": "2026-08-02T12:00:00+00:00", "action": "OPEN_LONG", "price": "100", "size": "1"},
                {"execution_key": "close", "occurred_at": "2026-08-02T12:05:00+00:00", "action": "CLOSE_LONG", "price": "110", "size": "1"},
            ],
        },
    ])

    assert rows[0]["chart"]["markers"][0]["side"] == "BUY"
    assert rows[0]["chart"]["markers"][1]["side"] == "SELL"
    assert rows[0]["chart"]["candle_provenance"] == "execution-only:journal"


def test_v2_ui_contract_is_present_in_static_assets() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    style = (STATIC / "style.css").read_text(encoding="utf-8")

    assert 'id="v2Workspace"' in html
    assert 'id="v2ModeButton"' in html
    assert '"/api/v2/bootstrap"' in script
    assert ".v2-layout" in style
    assert ".v2-execution-strip" in style


def test_hub_keeps_tracker_journal_and_tvl_as_separate_links() -> None:
    source = (ROOT / "apps_hub" / "access_page.py").read_text(encoding="utf-8")
    assert 'AppLink("trade-journal"' in source
    assert '_app_url("journal", 8811)' in source
    assert 'AppLink("tracker"' in source
    assert '_app_url("dashboard", 8080)' in source
    assert '"TVL & Protocol Monitor"' in source
