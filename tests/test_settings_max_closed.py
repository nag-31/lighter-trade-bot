"""Tests for the max_closed_trades setting in BotSettings / load_settings."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.sources import BotSettings, load_settings


class TestMaxClosedTradesDefault:
    def test_default_value(self):
        s = BotSettings()
        assert s.max_closed_trades == 200

    def test_default_is_int(self):
        s = BotSettings()
        assert isinstance(s.max_closed_trades, int)


class TestMaxClosedTradesFromConfig:
    def test_parses_from_config(self, tmp_path: Path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "settings:\n  max_closed_trades: 500\n",
            encoding="utf-8",
        )
        s = load_settings(cfg)
        assert s.max_closed_trades == 500

    def test_parses_small_value(self, tmp_path: Path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "settings:\n  max_closed_trades: 50\n",
            encoding="utf-8",
        )
        s = load_settings(cfg)
        assert s.max_closed_trades == 50

    def test_falls_back_to_default_when_missing(self, tmp_path: Path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "settings:\n  dashboard_port: 8080\n",
            encoding="utf-8",
        )
        s = load_settings(cfg)
        assert s.max_closed_trades == 200

    def test_falls_back_when_no_settings_block(self, tmp_path: Path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "sources:\n  - type: lighter\n    name: test\n    pool_id: 1\n",
            encoding="utf-8",
        )
        s = load_settings(cfg)
        assert s.max_closed_trades == 200

    def test_falls_back_when_config_missing(self, tmp_path: Path):
        s = load_settings(tmp_path / "nonexistent.yaml")
        assert s.max_closed_trades == 200

    def test_invalid_value_uses_default(self, tmp_path: Path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "settings:\n  max_closed_trades: not_a_number\n",
            encoding="utf-8",
        )
        s = load_settings(cfg)
        assert s.max_closed_trades == 200

    def test_zero_is_accepted(self, tmp_path: Path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "settings:\n  max_closed_trades: 0\n",
            encoding="utf-8",
        )
        s = load_settings(cfg)
        assert s.max_closed_trades == 0


class TestExecutionChartToggle:
    def test_defaults_to_disabled(self):
        assert BotSettings().execution_chart_enabled is False

    def test_parses_explicit_true(self, tmp_path: Path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "settings:\n  execution_chart_enabled: true\n",
            encoding="utf-8",
        )
        assert load_settings(cfg).execution_chart_enabled is True
