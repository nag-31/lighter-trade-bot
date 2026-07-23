import logging

from src.dashboard import _SecretRedactionFilter, _safe_telegram_error
from src.telegram_commands import (
    command_output_chat,
    format_fills,
    format_health,
    format_orders,
    format_positions,
    format_risk,
    format_sources,
    format_trades,
    parse_command,
    parse_count_and_source,
    split_message,
)


def test_command_output_chat_routes_owner_dm_privately():
    message = {
        "from": {"id": 42},
        "chat": {"id": 42, "type": "private"},
    }
    assert command_output_chat(
        message,
        owner_id=42,
        discussion_chat_id=-200,
        channel_id="-100",
    ) == "42"


def test_command_output_chat_routes_owner_discussion_to_channel():
    message = {
        "from": {"id": 42},
        "chat": {"id": -200, "type": "supergroup"},
    }
    assert command_output_chat(
        message,
        owner_id=42,
        discussion_chat_id=-200,
        channel_id="-100",
    ) == "-100"


def test_command_output_chat_rejects_other_users_chats_and_anonymous_admins():
    cases = [
        {"from": {"id": 99}, "chat": {"id": -200, "type": "supergroup"}},
        {"from": {"id": 42}, "chat": {"id": -201, "type": "supergroup"}},
        {
            "sender_chat": {"id": -100},
            "chat": {"id": -200, "type": "supergroup"},
        },
    ]
    for message in cases:
        assert command_output_chat(
            message,
            owner_id=42,
            discussion_chat_id=-200,
            channel_id="-100",
        ) is None


def test_telegram_token_is_redacted_from_http_logs():
    secret = "123456:ABC-secret"
    record = logging.LogRecord(
        "httpx",
        logging.INFO,
        __file__,
        1,
        "GET %s",
        (f"https://api.telegram.org/bot{secret}/getUpdates",),
        None,
    )
    assert _SecretRedactionFilter(secret).filter(record)
    assert secret not in record.getMessage()
    assert "<telegram-token>" in record.getMessage()


def test_telegram_exception_description_never_renders_request_url():
    secret = "123456:ABC-secret"
    exc = RuntimeError(
        f"403 for https://api.telegram.org/bot{secret}/setMyCommands"
    )
    result = _safe_telegram_error(exc)
    assert result == "RuntimeError: Telegram request failed"
    assert secret not in result
    assert "api.telegram.org" not in result


def test_parse_command_supports_bot_suffix_and_args():
    assert parse_command("/Trades@tracker_bot 12 HL 2") == (
        "trades",
        ["12", "HL", "2"],
    )
    assert parse_command("hello") is None


def test_parse_count_caps_and_preserves_source():
    assert parse_count_and_source(["999", "HL"]) == (25, "HL")
    assert parse_count_and_source(["lighter"]) == (10, "lighter")
    assert parse_count_and_source(["0"]) == (1, "")


def test_split_message_preserves_content_with_small_limit():
    text = "one\ntwo\nthree"
    chunks = split_message(text, limit=7)
    assert chunks == ["one\ntwo", "three"]


def test_positions_filter_and_stale_marker():
    rows = [
        {
            "source": "HL 2",
            "source_id": "hl-second",
            "exchange": "hyperliquid",
            "market_symbol": "BTC",
            "side": "long",
            "size": "1",
            "avg_entry_price": "100",
            "notional_usd": "100",
            "unrealized_pnl": "4.5",
            "liquidation_px": "80",
            "stale": True,
        },
        {
            "source": "Lighter",
            "market_symbol": "ETH",
            "side": "short",
        },
    ]
    result = format_positions(rows, "hl")
    assert "HL 2 · BTC LONG · STALE" in result
    assert "Lighter" not in result
    assert "uPnL +$4.50" in result


def test_orders_empty_explains_protocol_limitations():
    result = format_orders([], "")
    assert "No cached matching orders" in result
    assert "Lighter" in result
    assert "Binance" in result


def test_trades_use_privacy_display_fields():
    rows = [
        {
            "source": "HL",
            "market_symbol": "BTC",
            "side": "long",
            "entry": "100",
            "exit": "120",
            "entry_disp": "101",
            "exit_disp": "119",
            "pnl": "20",
            "ts": "real-time",
            "ts_disp": "display-time",
        }
    ]
    result = format_trades(rows, 10)
    assert "$101" in result
    assert "$119" in result
    assert "display-time" in result
    assert "real-time" not in result


def test_fills_and_risk_summaries():
    fills = [
        {
            "source": "Lighter",
            "market_symbol": "SOL",
            "side": "short",
            "kind": "REDUCE",
            "price": "150",
            "size": "2",
            "notional": "300",
            "ts": "now",
        }
    ]
    assert "SOL SHORT · REDUCE" in format_fills(fills, 10)

    positions = [
        {
            "source": "HL",
            "market_symbol": "BTC",
            "side": "long",
            "notional_usd": "700",
            "unrealized_pnl": "10",
        },
        {
            "source": "Lighter",
            "market_symbol": "ETH",
            "side": "short",
            "notional_usd": "300",
            "unrealized_pnl": "-2",
        },
    ]
    risk = format_risk(positions)
    assert "Gross exposure: $1,000.00" in risk
    assert "Combined uPnL: +$8.00" in risk
    assert "(70.0%)" in risk


def test_sources_and_health_show_status_without_secrets():
    health = {
        "ready": False,
        "started_at": "2026-01-01T00:00:00+00:00",
        "components": [
            {
                "component": "source:hl-main",
                "status": "up",
                "detail": "HL",
                "error": "",
            },
            {
                "component": "source:binance-second",
                "status": "disabled",
                "detail": "missing environment variable",
                "error": "",
            },
            {
                "component": "telegram",
                "status": "degraded",
                "detail": "",
                "error": "timeout",
            },
        ],
    }
    sources = format_sources(
        [{"id": "hl-main", "name": "HL", "exchange": "hyperliquid"}],
        health,
    )
    assert "HL [hyperliquid] — UP" in sources
    assert "binance-second — DISABLED" in sources

    summary = format_health(health)
    assert "DEGRADED" in summary
    assert "telegram — DEGRADED: timeout" in summary
