from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from src.dashboard import (
    INDEX_HTML,
    _plain_telegram_text,
    _to_jsonable,
    _trade_dedup_key,
    _uses_telegram_html,
)
from src.types import Event, EventKind, Position


def test_jsonable_serializes_nested_event_values_for_api_payloads():
    position = Position(
        market_id=1,
        market_symbol="BTC",
        side="long",
        size=Decimal("1.25"),
        avg_entry_price=Decimal("100.50"),
        unrealized_pnl=Decimal("2.5"),
        stale_since=datetime(2026, 7, 24, tzinfo=timezone.utc),
    )
    event = Event(
        kind=EventKind.OPEN,
        trade=None,  # type: ignore[arg-type]
        position_before=None,
        position_after=position,
    )

    payload = _to_jsonable(event)

    assert payload["kind"] is EventKind.OPEN
    assert payload["position_after"]["size"] == "1.25"
    assert payload["position_after"]["stale_since"] == "2026-07-24T00:00:00+00:00"


def test_trade_dedup_key_is_scoped_to_source_market_side_and_native_id():
    from tests.conftest import make_trade

    trade = make_trade(trade_id=55, market_id=3)
    same_fill_other_source = trade

    assert _trade_dedup_key(trade, "source-a") != _trade_dedup_key(
        same_fill_other_source, "source-b"
    )
    assert _trade_dedup_key(trade, "source-a") == "source-a|3|BOTH|55"


def test_frontend_contract_keeps_truth_and_alert_views_present():
    required_fragments = (
        'id="positions"',
        'id="alerts"',
        'id="events"',
        'new WebSocket',
        '"positions"',
        "STALE",
        "Telegram alerts",
    )

    for fragment in required_fragments:
        assert fragment in INDEX_HTML


def test_every_tracker_table_header_supports_click_and_keyboard_sorting():
    assert "function initSortableTables()" in INDEX_HTML
    assert "function sortTrackerTable(header)" in INDEX_HTML
    assert 'event.target.closest("th[data-sort-index]")' in INDEX_HTML
    assert 'event.key !== "Enter" && event.key !== " "' in INDEX_HTML
    assert 'header.setAttribute("aria-sort", "none")' in INDEX_HTML
    assert 'applyTableSort(tb.closest("table"));' in INDEX_HTML
    assert 'data-sort-value="${new Date(t.timestamp).getTime()}"' in INDEX_HTML
    assert 'data-sort-value="${p.unrealized_pnl ?? ""}"' in INDEX_HTML


def test_address_tracker_shows_filter_aware_aggregate_live_pnl():
    assert 'id="live-pnl-total"' in INDEX_HTML
    assert 'id="live-notional-total"' in INDEX_HTML
    assert "function renderLivePnl(positions)" in INDEX_HTML
    assert "const rows = filtered.filter(p => !p.stale);" in INDEX_HTML
    assert "renderLivePnl(data.positions);" in INDEX_HTML
    assert 'String(identity.side || "").toLowerCase() === _sideSelection' in INDEX_HTML


def test_dashboard_wallet_filter_supports_multiple_accounts_and_cutoff_analytics():
    assert 'id="source-filter"' in INDEX_HTML
    assert 'data-wallet-action="all"' in INDEX_HTML
    assert 'data-wallet-action="clear"' in INDEX_HTML
    assert "let _walletSelection = null;" in INDEX_HTML
    assert "analytics_trades" in INDEX_HTML
    assert "stats_cutoff" in INDEX_HTML
    assert 'id="side-filter"' in INDEX_HTML
    assert 'data-side-filter="long"' in INDEX_HTML
    assert 'data-side-filter="short"' in INDEX_HTML
    assert 'id="live-pnl-long"' not in INDEX_HTML
    assert 'id="live-pnl-short"' not in INDEX_HTML
    assert 'let _sideSelection = "";' in INDEX_HTML


def test_telegram_alert_paths_never_append_configured_source_website():
    source = (
        Path(__file__).resolve().parents[1] / "src" / "dashboard.py"
    ).read_text(encoding="utf-8")

    assert "pool_url=src.url" not in source
    assert "ev, src.url, src.name" not in source
    assert "_fallback_event, src.url, src.name" not in source
    assert '_caption += f"\\n{src.url}"' not in source


def test_full_close_chart_alert_uses_media_group_with_card_fallback():
    source = (
        Path(__file__).resolve().parents[1] / "src" / "dashboard.py"
    ).read_text(encoding="utf-8")

    # The chart is an album companion to the existing PnL card. Telegram's
    # attach:// names must match the multipart file keys, and failures must
    # preserve the card alert through the existing photo sender.
    required_fragments = (
        'if cfg.execution_chart_enabled and kind == "FULL" and card_bytes:',
        'await tg_send_media_group(',
        '"sendMediaGroup"',
        '"attach://pnl_card"',
        '"attach://execution_chart"',
        '"pnl_card": ("pnl-card.png"',
        '"execution_chart": ("execution-chart.png"',
        'event_uid=f"{outbox_uid}:card-fallback"',
    )
    for fragment in required_fragments:
        assert fragment in source


def test_execution_chart_toggle_keeps_card_path_available():
    source = (
        Path(__file__).resolve().parents[1] / "src" / "dashboard.py"
    ).read_text(encoding="utf-8")

    assert 'if cfg.execution_chart_enabled and kind == "FULL" and card_bytes:' in source
    assert 'if kind == "FULL" and card_bytes:' not in source.split(
        'if cfg.execution_chart_enabled and kind == "FULL" and card_bytes:', 1
    )[0]
    assert 'if command in {"oi", "openinterest"}:' in source
    assert 'if command in {"upnl", "livepnl"}:' in source
    assert '{"command": "oi", "description": "Total open interest"}' in source


def test_discussion_commands_reply_in_place_with_shared_anti_spam_gate():
    source = (
        Path(__file__).resolve().parents[1] / "src" / "dashboard.py"
    ).read_text(encoding="utf-8")

    assert "_discussion_command_rate_seconds = 4.0" in source
    assert "_command_chat_rate_last" in source
    assert "community discussion commands reply in place" in source
    assert "owner discussion commands publish to channel" not in source
    assert "in_discussion = output_chat != origin_chat" not in source


def test_telegram_html_is_detected_and_plain_alert_log_remains_readable():
    rich = "🟢 <b>LONG</b> · P&amp;L: <b>+$12.50</b>"

    assert _uses_telegram_html(rich)
    assert _plain_telegram_text(rich) == "🟢 LONG · P&L: +$12.50"

def test_events_payload_serializes_per_event_disp_and_strips_real_hl_values():
    """The recent-events feed is internet-reachable: HL events must carry a
    per-row _disp dict and must NOT leave real price/size in the serialized
    trade dict (the JS falls back to t.price when _disp is missing)."""
    source = (
        Path(__file__).resolve().parents[1] / "src" / "dashboard.py"
    ).read_text(encoding="utf-8")

    # snapshot_payload builds each recent event via the privacy-aware helper.
    assert '[_recent_event_payload(e) for e in recent_events[:cfg.max_recent_events]]' in source
    # The helper strips the REAL numbers from the serialized trade dict.
    assert "trade[key] = d[\"_disp\"][disp_key]" in source
    # Fail-closed path removes real values too.
    assert "trade.pop(key, None)" in source
    # The JS prefers _disp and only falls back to t.price when absent.
    assert "disp.price ?? t.price" in source
    # The event broadcast uses the same serialized (safe) event.
    assert '{"event": _recent_event_payload(ev)}' in source


def test_close_alert_uses_record_realization_card_path_not_shared_list_head():
    """The reconciler's silent-close backstop runs concurrently with the fill
    consumer; reading closed_trades[0] after an await can pick up another
    coin's card. The close alert must use the card path returned by
    record_realization instead."""
    source = (
        Path(__file__).resolve().parents[1] / "src" / "dashboard.py"
    ).read_text(encoding="utf-8")

    assert '(_close_result or {}).get("card_path")' in source
    assert "_close_record = closed_trades[0] if closed_trades else {}" not in source


def test_record_realization_claims_dedup_keys_before_any_await():
    """record_realization runs in concurrent tasks (fill consumer + reconciler
    silent-close backstop). The dedup keys must be claimed synchronously before
    any await so two tasks cannot both pass the guard and insert a duplicate
    in-memory row (double-counted PnL until restart)."""
    source = (
        Path(__file__).resolve().parents[1] / "src" / "dashboard.py"
    ).read_text(encoding="utf-8")

    assert "_recorded_realizations.update(realization_keys)" in source
    # The claim must happen at the top (dedup guard) AND after the DB write;
    # the key property is that no await sits between the membership check and
    # the claim. We assert the claim appears in the guard block by checking
    # the guard is immediately followed by an update call.
    guard_start = source.index("if all(key in _recorded_realizations for key in realization_keys):")
    claim_end = source.index("_recorded_realizations.update(realization_keys)", guard_start)
    segment = source[guard_start:claim_end]
    # No async/await allowed between the membership check and the claim.
    assert "await " not in segment

def test_record_realization_claims_dedup_keys_before_any_await():
    """record_realization runs in concurrent tasks (fill consumer + reconciler
    silent-close backstop). The dedup keys must be claimed synchronously before
    any await so two tasks cannot both pass the guard and insert a duplicate
    in-memory row (double-counted PnL until restart)."""
    source = (
        Path(__file__).resolve().parents[1] / "src" / "dashboard.py"
    ).read_text(encoding="utf-8")

    assert "_recorded_realizations.update(realization_keys)" in source
    guard_start = source.index("if all(key in _recorded_realizations for key in realization_keys):")
    # The FIRST update call after the guard must be the synchronous claim (in
    # the dedup-guard block), not the one after the DB write. Between the
    # membership check and that claim there must be no real await statement.
    first_update = source.index("_recorded_realizations.update(realization_keys)", guard_start)
    segment = source[guard_start:first_update]
    assert "return None" in segment  # guard still skips when all recorded
    assert not any(line.strip().startswith("await ") for line in segment.splitlines())
