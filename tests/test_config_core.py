from __future__ import annotations

from decimal import Decimal

import pytest

from src.config import load


REQUIRED_ENV = {
    "LIGHTER_POOL_ID": "42",
    "TELEGRAM_BOT_TOKEN": "token",
    "TELEGRAM_CHANNEL_ID": "-100123",
    "TELEGRAM_OWNER_USER_ID": "99",
    "TWITTER_API_KEY": "key",
    "TWITTER_API_SECRET": "secret",
    "TWITTER_ACCESS_TOKEN": "access",
    "TWITTER_ACCESS_TOKEN_SECRET": "access-secret",
}


def _set_required(monkeypatch):
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)


def test_load_parses_complete_config_and_optional_nested_blocks(tmp_path, monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("CHART_API_KEY", "chart-key")
    config = tmp_path / "config.yaml"
    config.write_text(
        """
chart:
  enabled: true
  interval: 15m
  symbol_template: BINANCE:{symbol}USDT
  symbol_overrides: {BTC: BTCUSDT}
  width: 900
  height: 500
  theme: light
lighter:
  ws_url: wss://lighter.example/stream
  rest_base: https://lighter.example/api
  rest_safety_poll_seconds: 17
filters:
  min_notional_usd: 123.45
recaps:
  daily_utc_hour: 4
  weekly_day: Sunday
  weekly_utc_hour: 5
links:
  pool_url_template: https://example/{pool_id}
""",
        encoding="utf-8",
    )

    cfg = load(config_path=str(config))

    assert cfg.lighter_pool_id == 42
    assert cfg.telegram_owner_user_id == 99
    assert cfg.chart.enabled is True
    assert cfg.chart.width == 900
    assert cfg.chart.symbol_overrides == {"BTC": "BTCUSDT"}
    assert cfg.lighter.rest_safety_poll_seconds == 17
    assert cfg.min_notional_usd == Decimal("123.45")
    assert cfg.weekly_recap_day == "sunday"
    assert cfg.chart_api_key == "chart-key"


def test_load_does_not_treat_string_false_as_true(tmp_path, monkeypatch):
    _set_required(monkeypatch)
    config = tmp_path / "config.yaml"
    config.write_text(
        """
chart:
  enabled: "false"
filters: {min_notional_usd: 0}
recaps: {daily_utc_hour: 0, weekly_day: monday, weekly_utc_hour: 0}
links: {pool_url_template: https://example}
""",
        encoding="utf-8",
    )

    assert load(config_path=str(config)).chart.enabled is False


def test_load_requires_each_secret_or_credential(monkeypatch, tmp_path):
    _set_required(monkeypatch)
    monkeypatch.delenv("TWITTER_API_SECRET")
    config = tmp_path / "config.yaml"
    config.write_text(
        "filters: {min_notional_usd: 0}\nrecaps: {}\nlinks: {}\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="TWITTER_API_SECRET"):
        load(config_path=str(config))


def test_load_uses_safe_defaults_for_optional_sections(tmp_path, monkeypatch):
    _set_required(monkeypatch)
    config = tmp_path / "config.yaml"
    config.write_text(
        "filters: {min_notional_usd: 0}\nrecaps: {}\nlinks: {}\n",
        encoding="utf-8",
    )

    cfg = load(config_path=str(config))

    assert cfg.chart.enabled is False
    assert cfg.lighter.rest_safety_poll_seconds == 60
    assert cfg.daily_recap_utc_hour == 0
    assert cfg.weekly_recap_day == ""
