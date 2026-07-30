from __future__ import annotations

import inspect
from pathlib import Path

from trade_journal.app import bootstrap, create_app


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


def test_hub_keeps_tracker_journal_and_tvl_as_separate_links() -> None:
    source = (ROOT / "apps_hub" / "access_page.py").read_text(encoding="utf-8")
    assert 'AppLink("trade-journal"' in source
    assert '_app_url("journal", 8811)' in source
    assert 'AppLink("tracker"' in source
    assert '_app_url("dashboard", 8080)' in source
    assert '"TVL & Protocol Monitor"' in source
