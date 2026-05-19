import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import yaml
from dotenv import load_dotenv


@dataclass(frozen=True)
class ChartConfig:
    enabled: bool
    interval: str
    symbol_template: str
    symbol_overrides: dict[str, str]
    width: int
    height: int
    theme: str


@dataclass(frozen=True)
class LighterConfig:
    ws_url: str
    rest_base: str
    rest_safety_poll_seconds: int


@dataclass(frozen=True)
class Config:
    lighter_pool_id: int

    telegram_bot_token: str
    telegram_channel_id: str
    telegram_owner_user_id: int

    twitter_api_key: str
    twitter_api_secret: str
    twitter_access_token: str
    twitter_access_token_secret: str

    chart_api_key: str
    chart: ChartConfig

    lighter: LighterConfig

    min_notional_usd: Decimal
    daily_recap_utc_hour: int
    weekly_recap_day: str
    weekly_recap_utc_hour: int
    pool_url_template: str

    twitter_daily_soft_cap: int = 15
    state_path: Path = Path("state.json")
    healthz_port: int = 8080


def _require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise RuntimeError(f"missing required env var: {name}")
    return val


def load(env_path: str | None = None, config_path: str = "config.yaml") -> Config:
    load_dotenv(env_path or ".env")
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    chart_cfg_raw = cfg.get("chart", {})
    lighter_cfg_raw = cfg.get("lighter", {})

    return Config(
        lighter_pool_id=int(_require("LIGHTER_POOL_ID")),
        telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
        telegram_channel_id=_require("TELEGRAM_CHANNEL_ID"),
        telegram_owner_user_id=int(_require("TELEGRAM_OWNER_USER_ID")),
        twitter_api_key=_require("TWITTER_API_KEY"),
        twitter_api_secret=_require("TWITTER_API_SECRET"),
        twitter_access_token=_require("TWITTER_ACCESS_TOKEN"),
        twitter_access_token_secret=_require("TWITTER_ACCESS_TOKEN_SECRET"),
        chart_api_key=os.environ.get("CHART_API_KEY", ""),
        chart=ChartConfig(
            enabled=bool(chart_cfg_raw.get("enabled", False)),
            interval=str(chart_cfg_raw.get("interval", "1h")),
            symbol_template=str(chart_cfg_raw.get("symbol_template", "BINANCE:{symbol}USDT")),
            symbol_overrides=dict(chart_cfg_raw.get("symbol_overrides") or {}),
            width=int(chart_cfg_raw.get("width", 1280)),
            height=int(chart_cfg_raw.get("height", 720)),
            theme=str(chart_cfg_raw.get("theme", "dark")),
        ),
        lighter=LighterConfig(
            ws_url=str(lighter_cfg_raw.get("ws_url", "wss://mainnet.zklighter.elliot.ai/stream")),
            rest_base=str(lighter_cfg_raw.get("rest_base", "https://mainnet.zklighter.elliot.ai/api/v1")),
            rest_safety_poll_seconds=int(lighter_cfg_raw.get("rest_safety_poll_seconds", 60)),
        ),
        min_notional_usd=Decimal(str(cfg["filters"]["min_notional_usd"])),
        daily_recap_utc_hour=int(cfg["recaps"]["daily_utc_hour"]),
        weekly_recap_day=str(cfg["recaps"]["weekly_day"]).lower(),
        weekly_recap_utc_hour=int(cfg["recaps"]["weekly_utc_hour"]),
        pool_url_template=str(cfg["links"]["pool_url_template"]),
    )
