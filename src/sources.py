"""Plug-and-play source layer.

A "source" is one tracked pool or wallet. Add an entry to config.yaml, restart,
and the dashboard picks it up — no code change. Each source pairs an exchange
client with its own PositionTracker so market_id keys never collide.

config.yaml shape:

    sources:
      - type: lighter
        name: "My NK pool"
        pool_id: 281474976684763
      - type: lighter
        id: lighter-wallet
        name: "Lighter wallet"
        address_env: LIGHTER_ADDRESS
      - type: hyperliquid
        id: hl-whale-a
        name: "Whale A"
        address_env: HL_WHALE_A_ADDRESS
        min_notional_usd: 1000   # optional per-source override
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import AsyncIterator, Optional, Protocol

import yaml

from .binance_client import BinanceClient
from .hyperliquid_client import HyperliquidClient
from .lighter_client import LighterClient
from .position_tracker import PositionTracker
from .proxy_pool import ProxyPool
from .types import OpenOrder, Position, Trade

log = logging.getLogger(__name__)

LIGHTER_REST_BASE = "https://mainnet.zklighter.elliot.ai/api/v1"
LIGHTER_WS_URL = "wss://mainnet.zklighter.elliot.ai/stream"
DEFAULT_MIN_NOTIONAL = Decimal("1000")


@dataclass(frozen=True)
class BotSettings:
    """All runtime-tunable knobs loaded from config.yaml → settings: block.

    Every field has a safe default so the bot starts even if settings: is
    missing or partially filled in.
    """
    default_min_notional_usd: Decimal = DEFAULT_MIN_NOTIONAL

    # Alert toggles
    alert_on_open: bool        = True
    alert_on_close: bool       = True
    alert_on_size_change: bool = True
    alert_on_reduce: bool      = True

    # Timing (seconds)
    aggregate_window_seconds:    int = 30
    rest_poll_seconds:           int = 60
    reconciler_interval_seconds: int = 60
    tg_dedup_window_seconds:     int = 90
    stale_position_warning_seconds: int = 120
    stale_position_down_seconds: int = 600

    # Cross-coin session digest: add/reduce alerts that flush within this many
    # seconds of each other (per source) are combined into ONE Telegram message
    # instead of one message per coin. 0 ⇒ disabled (send each immediately).
    digest_window_seconds:       int = 20

    # Daily jobs (UTC midnight): channel recap of the day's closed trades, and
    # a private self-audit DM to TELEGRAM_OWNER_USER_ID when the DB's per-coin
    # PnL disagrees with the exchange's own figures.
    daily_recap_enabled:      bool = True
    daily_self_audit_enabled: bool = True

    # Dashboard
    dashboard_port:      int = 8080
    max_recent_events:   int = 200
    max_closed_trades:   int = 200

    # ── Privacy (HL display obfuscation) ─────────────────────────────────────
    # Master switch: when False, every alert/card/dashboard cell shows REAL
    # values (full rollback). Posture = "exact results, fuzzy fingerprint":
    # PnL $/%/win-rate stay EXACT; only price/size/notional/timestamps are fuzzed.
    # HL-only — Lighter is never transformed. The secret salt loads from the
    # PRIVACY_SECRET_KEY env var, NEVER from config.yaml.
    privacy_enabled:           bool  = True
    privacy_mag:               float = 0.003       # price jitter ±0.3% (0 < mag < 0.05)
    privacy_entry_quantum_pct: float = 0.005       # coarse seed bucket = 0.5% of price
    privacy_size_sigfigs:      int   = 2           # round displayed size to N sig figs
    privacy_notional_sigfigs:  int   = 2           # round displayed notional to N sig figs
    privacy_time_bucket:       str   = "relative"  # exact | hour | relative
    privacy_disclose_footnote: bool  = True
    privacy_secret_key:        str   = ""          # loaded from env, never config.yaml

    # Open orders dashboard panel
    open_orders_enabled: bool = True

    # Stats: compute over ALL closed trades, not just the last max_closed_trades
    stats_full_history: bool = True

    # ── Stats display window ─────────────────────────────────────────────────
    # Scope which closed trades appear in the analytics + history grid without
    # importing the entire HL history. Dates are ISO strings ("2026-05-01" or
    # "2026-05-01T00:00:00Z"). stats_end_date None ⇒ current date/time. A
    # date-only end is inclusive end-of-day. stats_symbols whitelists tickers;
    # empty ⇒ every coin currently in the DB (today = only the existing
    # dashboard coins, since no full-history import was done).
    stats_start_date: Optional[str] = None
    stats_end_date:   Optional[str] = None
    stats_symbols:    tuple         = ()

    # ── Binance proxy pool ────────────────────────────────────────────────────
    # List of proxy URLs used to bypass Binance geo-blocks.  A tuple (not list)
    # because BotSettings is frozen.  Parsed from the YAML list at load time.
    # Example YAML:
    #   binance_proxies:
    #     - socks5://1.2.3.4:1080
    #     - http://5.6.7.8:3128
    # Free public proxies die fast — refresh the list regularly.
    # US-based proxies will NOT bypass the Binance.com US ban.
    binance_proxies: tuple = ()
    binance_proxy_test_url: str = "https://fapi.binance.com/fapi/v1/ping"


def load_settings(path: str | Path = "config.yaml") -> BotSettings:
    """Read the optional 'settings:' block from config.yaml.

    Missing keys fall back to BotSettings defaults, so this is always safe.
    """
    p = Path(path)
    raw: dict = {}
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        raw = cfg.get("settings") or {}

    def _bool(key: str, default: bool) -> bool:
        v = raw.get(key, default)
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            normalized = v.strip().lower()
            if normalized in {"true", "yes", "1", "on"}:
                return True
            if normalized in {"false", "no", "0", "off"}:
                return False
        if isinstance(v, (int, float)) and v in {0, 1}:
            return bool(v)
        log.warning("settings.%s must be a boolean — using default %s", key, default)
        return default

    def _int(key: str, default: int) -> int:
        try:
            return int(raw.get(key, default))
        except (TypeError, ValueError):
            log.warning("settings.%s must be an integer — using default %d", key, default)
            return default

    def _decimal(key: str, default: Decimal) -> Decimal:
        try:
            return Decimal(str(raw.get(key, default)))
        except Exception:
            log.warning("settings.%s must be a number — using default %s", key, default)
            return default

    def _float(key: str, default: float) -> float:
        try:
            return float(raw.get(key, default))
        except (TypeError, ValueError):
            log.warning("settings.%s must be a number — using default %s", key, default)
            return default

    def _str(key: str, default: str) -> str:
        v = raw.get(key, default)
        return str(v) if v is not None else default

    def _str_or_none(key: str) -> Optional[str]:
        v = raw.get(key)
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    # Privacy salt is a SECRET — it loads from the PRIVACY_SECRET_KEY env var,
    # never from config.yaml. Falling back to a fixed dev salt keeps the bot
    # running, but production MUST set the env var (a known salt is recoverable).
    privacy_enabled = _bool("privacy_enabled", True)
    privacy_secret = os.getenv("PRIVACY_SECRET_KEY", "").strip()
    if privacy_enabled and not privacy_secret:
        log.warning(
            "PRIVACY_SECRET_KEY env var is unset — using an insecure default salt. "
            "Set PRIVACY_SECRET_KEY in .env for real privacy."
        )
        privacy_secret = "lighterbot-dev-privacy-salt-CHANGE-ME"

    # Parse binance_proxies: accept a YAML list of strings
    _raw_proxies = raw.get("binance_proxies") or []
    if not isinstance(_raw_proxies, list):
        log.warning("settings.binance_proxies must be a list — ignoring")
        _raw_proxies = []
    _binance_proxies: tuple = tuple(
        str(u).strip() for u in _raw_proxies if str(u).strip()
    )

    # Parse stats_symbols: accept a YAML list (or a single string) of tickers.
    _raw_symbols = raw.get("stats_symbols") or []
    if not isinstance(_raw_symbols, list):
        _raw_symbols = [_raw_symbols]
    _stats_symbols: tuple = tuple(
        str(s).strip() for s in _raw_symbols if str(s).strip()
    )

    settings = BotSettings(
        default_min_notional_usd    = _decimal("default_min_notional_usd", DEFAULT_MIN_NOTIONAL),
        alert_on_open               = _bool("alert_on_open",        True),
        alert_on_close              = _bool("alert_on_close",        True),
        alert_on_size_change        = _bool("alert_on_size_change",  True),
        alert_on_reduce             = _bool("alert_on_reduce",       True),
        aggregate_window_seconds    = _int("aggregate_window_seconds",    30),
        rest_poll_seconds           = _int("rest_poll_seconds",           60),
        reconciler_interval_seconds = _int("reconciler_interval_seconds", 60),
        tg_dedup_window_seconds     = _int("tg_dedup_window_seconds",     90),
        stale_position_warning_seconds = _int("stale_position_warning_seconds", 120),
        stale_position_down_seconds = _int("stale_position_down_seconds", 600),
        digest_window_seconds       = _int("digest_window_seconds",       20),
        daily_recap_enabled         = _bool("daily_recap_enabled",        True),
        daily_self_audit_enabled    = _bool("daily_self_audit_enabled",   True),
        dashboard_port              = _int("dashboard_port",              8080),
        max_recent_events           = _int("max_recent_events",           200),
        max_closed_trades           = _int("max_closed_trades",           200),
        privacy_enabled             = privacy_enabled,
        privacy_mag                 = _float("privacy_mag",               0.003),
        privacy_entry_quantum_pct   = _float("privacy_entry_quantum_pct", 0.005),
        privacy_size_sigfigs        = _int("privacy_size_sigfigs",        2),
        privacy_notional_sigfigs    = _int("privacy_notional_sigfigs",    2),
        privacy_time_bucket         = _str("privacy_time_bucket",         "relative"),
        privacy_disclose_footnote   = _bool("privacy_disclose_footnote",  True),
        privacy_secret_key          = privacy_secret,
        open_orders_enabled         = _bool("open_orders_enabled",        True),
        stats_full_history          = _bool("stats_full_history",         True),
        stats_start_date            = _str_or_none("stats_start_date"),
        stats_end_date              = _str_or_none("stats_end_date"),
        stats_symbols               = _stats_symbols,
        binance_proxies             = _binance_proxies,
        binance_proxy_test_url      = _str(
            "binance_proxy_test_url",
            "https://fapi.binance.com/fapi/v1/ping",
        ),
    )
    if not 1 <= settings.dashboard_port <= 65535:
        log.warning("settings.dashboard_port is out of range — using 8080")
        settings = replace(settings, dashboard_port=8080)
    nonnegative_defaults = {
        "aggregate_window_seconds": 30,
        "rest_poll_seconds": 60,
        "reconciler_interval_seconds": 60,
        "tg_dedup_window_seconds": 90,
        "stale_position_warning_seconds": 120,
        "stale_position_down_seconds": 600,
    }
    for field_name, default in nonnegative_defaults.items():
        if getattr(settings, field_name) < 0:
            log.warning("settings.%s must be non-negative — using %d", field_name, default)
            settings = replace(settings, **{field_name: default})
    if settings.stale_position_down_seconds < settings.stale_position_warning_seconds:
        log.warning(
            "settings.stale_position_down_seconds is below warning threshold — "
            "using warning threshold"
        )
        settings = replace(
            settings,
            stale_position_down_seconds=settings.stale_position_warning_seconds,
        )
    log.info(
        "settings loaded — min_notional=$%s  window=%ds  poll=%ds  dedup=%ds  port=%d",
        settings.default_min_notional_usd,
        settings.aggregate_window_seconds,
        settings.rest_poll_seconds,
        settings.tg_dedup_window_seconds,
        settings.dashboard_port,
    )
    return settings


class ExchangeClient(Protocol):
    """The interface the dashboard depends on. LighterClient and
    HyperliquidClient both satisfy this via duck typing."""

    source: str

    async def bootstrap_markets(self) -> dict[int, str]: ...
    async def current_positions(self) -> dict[int, Position]: ...
    async def fetch_trades_since(
        self, since_trade_id: Optional[int], limit: int = 100
    ) -> list[Trade]: ...
    def stream_trades(self) -> AsyncIterator[Trade]: ...
    async def fetch_leverage(self, market_id: int) -> Optional[float]: ...
    async def fetch_sl_tp(self, market_id: int) -> tuple[Optional[Decimal], Optional[Decimal]]: ...
    async def fetch_open_orders(self) -> list[OpenOrder]: ...
    async def close(self) -> None: ...


@dataclass
class Source:
    """One tracked pool/wallet plus its live tracking state."""

    id: str
    name: str
    client: ExchangeClient
    tracker: PositionTracker
    url: str
    min_notional: Decimal
    exchange: str = ""
    enabled: bool = True
    config_error: str = ""
    account_fingerprint: str = ""
    last_trade_id: Optional[int] = None
    # Set-based dedup: protects against WS replay and REST/WS overlap.
    # Using a set catches duplicates with any tid, not just the last one.
    seen_tids: set[str] = field(default_factory=set)
    # Normalized ticker symbols to hide entirely: no dashboard row, no TG
    # alert, no reconciler notification. Populated from config.yaml
    # `exclude_symbols`. Compared via _normalize_symbol (case-insensitive,
    # quote-suffix tolerant).
    exclude_symbols: frozenset[str] = field(default_factory=frozenset)

    def is_excluded(self, market_symbol: str) -> bool:
        """True if this ticker should be hidden from dashboard + alerts."""
        if not self.exclude_symbols:
            return False
        return _normalize_symbol(market_symbol) in self.exclude_symbols

    def cursor_for_trade(self, trade: Trade) -> int:
        """Return the protocol cursor represented by a normalized fill."""
        if self.exchange == "binance":
            return int(trade.timestamp.timestamp() * 1000)
        return int(trade.trade_id)

    @property
    def is_hyperliquid(self) -> bool:
        """Canonical HL predicate — the privacy transform is gated on this.

        The exchange type lives only in Source.id (built with a 'hyperliquid:'
        prefix); Trade.source / Position.source carry only the human name.
        """
        return self.exchange == "hyperliquid" or self.id.startswith("hyperliquid:")


@dataclass(frozen=True)
class SourceConfigIssue:
    """Redacted validation status for a disabled or invalid source."""

    source_id: str
    name: str
    exchange: str
    status: str
    detail: str


@dataclass
class SourceLoadReport:
    """Active sources plus disabled/invalid entries for health reporting."""

    sources: list[Source] = field(default_factory=list)
    issues: list[SourceConfigIssue] = field(default_factory=list)

    @property
    def active(self) -> list[Source]:
        return self.sources

    @property
    def ok(self) -> bool:
        return not any(i.status == "config_error" for i in self.issues)


_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,63}$")
_ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def _configured_id(raw: dict, exchange: str, legacy_suffix: str) -> str:
    explicit = str(raw.get("id", "")).strip().lower()
    if explicit:
        if not _ID_RE.fullmatch(explicit):
            raise ValueError(
                "id must be 2-64 lowercase letters, numbers, '.', '_' or '-'"
            )
        return explicit
    return f"{exchange}:{legacy_suffix}"


def _secret_env(raw: dict, key: str, legacy: str) -> tuple[str, str]:
    env_name = str(raw.get(key, legacy)).strip()
    if not _ENV_RE.fullmatch(env_name):
        raise ValueError(f"{key} must name an uppercase environment variable")
    return env_name, os.getenv(env_name, "").strip()


def _source_issue_detail(raw: dict) -> str:
    """Return a redacted validation error, or an empty string when usable."""
    stype = str(raw.get("type", "")).lower().strip()
    name = str(raw.get("name", "")).strip()
    if not name:
        return "missing name"
    if stype not in {"lighter", "hyperliquid", "binance"}:
        return "unsupported source type"
    try:
        _configured_id(raw, stype, "legacy")
    except ValueError as exc:
        return str(exc)
    if raw.get("min_notional_usd") is not None:
        try:
            value = Decimal(str(raw["min_notional_usd"]))
            if not value.is_finite() or value < 0:
                raise ValueError
        except Exception:
            return "min_notional_usd must be a finite non-negative number"
    if stype == "lighter":
        has_pool = raw.get("pool_id") is not None
        has_address = raw.get("address_env") is not None
        if has_pool == has_address:
            return "configure exactly one of pool_id or address_env"
        if has_pool:
            try:
                if int(raw["pool_id"]) < 0:
                    raise ValueError
            except (TypeError, ValueError):
                return "pool_id must be a non-negative integer"
        else:
            try:
                env_name, address = _secret_env(
                    raw, "address_env", "LIGHTER_ADDRESS"
                )
            except ValueError as exc:
                return str(exc)
            if not address:
                return f"missing environment variable {env_name}"
            if not re.fullmatch(r"0x[0-9a-fA-F]{40}", address):
                return f"{env_name} does not contain a valid address"
            try:
                account_slot = int(raw.get("account_slot", 0))
                if account_slot < 0:
                    raise ValueError
            except (TypeError, ValueError):
                return "account_slot must be a non-negative integer"
    elif stype == "hyperliquid":
        try:
            env_name, address = _secret_env(raw, "address_env", "HL_ADDRESS")
        except ValueError as exc:
            return str(exc)
        if not address:
            return f"missing environment variable {env_name}"
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", address):
            return f"{env_name} does not contain a valid address"
    elif stype == "binance":
        try:
            key_env, key = _secret_env(raw, "api_key_env", "BINANCE_API_KEY")
            secret_env, secret = _secret_env(
                raw, "api_secret_env", "BINANCE_API_SECRET"
            )
        except ValueError as exc:
            return str(exc)
        missing = [env for env, value in ((key_env, key), (secret_env, secret)) if not value]
        if missing:
            return "missing environment variable(s): " + ", ".join(missing)
    return ""


def _preview_source_identity(raw: dict) -> tuple[str, str]:
    """Compute stable source/account identities without constructing a client."""
    stype = str(raw.get("type", "")).lower().strip()
    if stype == "lighter":
        if raw.get("pool_id") is not None:
            pool_id = int(raw["pool_id"])
            return (
                _configured_id(raw, stype, str(pool_id)),
                f"lighter:{pool_id}",
            )
        _env_name, address = _secret_env(
            raw, "address_env", "LIGHTER_ADDRESS"
        )
        account_slot = int(raw.get("account_slot", 0))
        address_hash = hashlib.sha256(address.lower().encode()).hexdigest()[:12]
        return (
            _configured_id(raw, stype, f"{address_hash}-{account_slot}"),
            f"lighter-wallet:{address_hash}:{account_slot}",
        )
    if stype == "hyperliquid":
        _env_name, address = _secret_env(raw, "address_env", "HL_ADDRESS")
        address_hash = hashlib.sha256(address.lower().encode()).hexdigest()[:12]
        return (
            _configured_id(raw, stype, address_hash),
            f"hyperliquid:{address_hash}",
        )
    key_env, _key = _secret_env(raw, "api_key_env", "BINANCE_API_KEY")
    secret_env, _secret = _secret_env(
        raw, "api_secret_env", "BINANCE_API_SECRET"
    )
    env_hash = hashlib.sha256(key_env.encode()).hexdigest()[:12]
    account_hash = hashlib.sha256(
        f"{key_env}|{secret_env}".encode()
    ).hexdigest()[:12]
    return (
        _configured_id(raw, stype, env_hash),
        f"binance:{account_hash}",
    )


def _proxy_url() -> Optional[str]:
    """Return the SOCKS5 proxy URL from env, or None if not set.

    Set SOCKS_PROXY_URL=socks5h://host:1080 in .env to route Lighter and
    Binance traffic through a proxy in a non-restricted jurisdiction.
    Hyperliquid does NOT use this proxy (it is not geo-blocked).
    Use socks5h:// (not socks5://) so DNS resolves on the proxy side.
    """
    url = os.getenv("SOCKS_PROXY_URL", "").strip()
    return url if url else None


def _normalize_symbol(sym: str) -> str:
    """Canonicalize a ticker for exclusion matching: uppercase, strip a single
    trailing quote/perp suffix. So 'fartcoinusd', 'FARTCOIN', 'FARTCOIN-PERP'
    all collapse to 'FARTCOIN'."""
    s = str(sym or "").upper().strip()
    for suffix in ("USDT", "USDC", "USD", "-PERP", "PERP"):
        if s.endswith(suffix) and len(s) > len(suffix):
            return s[: -len(suffix)]
    return s


def _build_source(raw: dict, settings: "BotSettings | None" = None) -> Optional[Source]:
    stype = str(raw.get("type", "")).lower().strip()
    name = str(raw.get("name", "")).strip()
    if not name:
        log.warning("source entry missing 'name' — skipping: %r", raw)
        return None

    if raw.get("enabled") is False:
        log.info("source '%s' is disabled by configuration", name)
        return None

    global_min = (settings.default_min_notional_usd if settings else DEFAULT_MIN_NOTIONAL)
    try:
        min_notional = (
            Decimal(str(raw["min_notional_usd"]))
            if raw.get("min_notional_usd") is not None
            else global_min
        )
        if not min_notional.is_finite() or min_notional < 0:
            raise ValueError("must be a finite non-negative number")
    except Exception:
        log.warning("source '%s' has invalid min_notional_usd", name)
        return None

    # Per-source ticker exclusions (hidden from dashboard + all alerts).
    raw_excludes = raw.get("exclude_symbols") or []
    if not isinstance(raw_excludes, list):
        raw_excludes = [raw_excludes]
    exclude_symbols = frozenset(
        _normalize_symbol(x) for x in raw_excludes if str(x).strip()
    )
    if exclude_symbols:
        log.info("source '%s': excluding symbols %s", name, sorted(exclude_symbols))

    if stype == "lighter":
        pool_id = raw.get("pool_id")
        l1_address: Optional[str] = None
        account_slot = 0
        try:
            if pool_id is not None:
                pool_id = int(pool_id)
                if pool_id < 0:
                    raise ValueError("pool_id must be non-negative")
                identity_suffix = str(pool_id)
                account_fingerprint = f"lighter:{pool_id}"
            else:
                _env_name, l1_address = _secret_env(
                    raw, "address_env", "LIGHTER_ADDRESS"
                )
                if not re.fullmatch(r"0x[0-9a-fA-F]{40}", l1_address):
                    raise ValueError("address_env does not contain a valid address")
                account_slot = int(raw.get("account_slot", 0))
                if account_slot < 0:
                    raise ValueError("account_slot must be non-negative")
                address_hash = hashlib.sha256(
                    l1_address.lower().encode()
                ).hexdigest()[:12]
                identity_suffix = f"{address_hash}-{account_slot}"
                account_fingerprint = (
                    f"lighter-wallet:{address_hash}:{account_slot}"
                )
            source_id = _configured_id(raw, "lighter", identity_suffix)
        except (TypeError, ValueError) as exc:
            log.warning("lighter source '%s' has invalid configuration: %s", name, exc)
            return None
        # WS-only proxy: Lighter geo-blocks the /stream endpoint from some
        # regions while REST stays open. Set LIGHTER_WS_PROXY in .env (e.g.
        # socks5h://host:1080) to route ONLY the real-time WS through an
        # allowed region — REST stays direct.
        ws_proxy = os.getenv("LIGHTER_WS_PROXY", "").strip() or None
        client = LighterClient(
            pool_id, LIGHTER_REST_BASE, LIGHTER_WS_URL,
            source=name, proxy_url=_proxy_url(), ws_proxy_url=ws_proxy,
            l1_address=l1_address, account_slot=account_slot,
        )
        return Source(
            id=source_id,
            name=name,
            client=client,
            tracker=PositionTracker(source=name),
            url=(
                f"https://app.lighter.xyz/public-pools/{pool_id}"
                if pool_id is not None
                else ""
            ),
            min_notional=min_notional,
            exchange="lighter",
            account_fingerprint=account_fingerprint,
            exclude_symbols=exclude_symbols,
        )

    if stype == "hyperliquid":
        # Address is loaded from the HL_ADDRESS env var, not from config.yaml,
        # to keep the wallet address out of version control.
        try:
            address_env, address = _secret_env(raw, "address_env", "HL_ADDRESS")
        except ValueError as exc:
            log.warning("hyperliquid source '%s': %s", name, exc)
            return None
        if not address:
            log.warning(
                "hyperliquid source '%s': configured address env var is missing or empty — "
                "skipping HL source (Lighter continues unaffected)",
                name,
            )
            return None
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", address):
            log.warning(
                "hyperliquid source '%s': %s contains an invalid address",
                name,
                address_env,
            )
            return None
        # footer_url is an optional public website to append to HL alerts.
        # The HL explorer URL is intentionally NOT used here — it exposes the wallet address.
        footer_url = str(raw.get("footer_url", "")).strip()
        client = HyperliquidClient(address, source=name)
        # Source id must never embed the raw wallet address — it can surface in
        # logs (e.g. the duplicate-source warning). Use a non-reversible hash so
        # the id is stable and unique without exposing the address anywhere.
        addr_hash = hashlib.sha256(address.lower().encode()).hexdigest()[:12]
        try:
            source_id = _configured_id(raw, "hyperliquid", addr_hash)
        except ValueError as exc:
            log.warning("hyperliquid source '%s': %s", name, exc)
            return None
        return Source(
            id=source_id,
            name=name,
            client=client,
            tracker=PositionTracker(source=name),
            url=footer_url,   # wallet address is NEVER put here; only an explicit public footer_url
            min_notional=min_notional,
            exchange="hyperliquid",
            account_fingerprint=f"hyperliquid:{addr_hash}",
            exclude_symbols=exclude_symbols,
        )

    if stype == "binance":
        # API key + secret loaded from env vars — never put credentials in config.yaml.
        try:
            key_env, api_key = _secret_env(raw, "api_key_env", "BINANCE_API_KEY")
            secret_env, api_secret = _secret_env(
                raw, "api_secret_env", "BINANCE_API_SECRET"
            )
        except ValueError as exc:
            log.warning("binance source '%s': %s", name, exc)
            return None
        if not api_key or not api_secret:
            log.warning(
                "binance source '%s': BINANCE_API_KEY and/or BINANCE_API_SECRET "
                "env vars are missing or empty — skipping Binance source "
                "(other sources continue unaffected)",
                name,
            )
            return None
        try:
            source_id = _configured_id(
                raw,
                "binance",
                hashlib.sha256(key_env.encode()).hexdigest()[:12],
            )
        except ValueError as exc:
            log.warning("binance source '%s': %s", name, exc)
            return None
        footer_url = str(raw.get("footer_url", "")).strip()

        # Prefer the rotating proxy pool when binance_proxies is configured.
        # Fall back to a single SOCKS_PROXY_URL when no list is present.
        pool: Optional[ProxyPool] = None
        if settings is not None and settings.binance_proxies:
            pool = ProxyPool(
                list(settings.binance_proxies),
                settings.binance_proxy_test_url,
            )
            log.info(
                "binance source '%s': using rotating proxy pool (%d proxies)",
                name, pool.n_total,
            )
            client = BinanceClient(api_key, api_secret, source=name, proxy_pool=pool)
        else:
            # Legacy: single optional env-var proxy
            client = BinanceClient(api_key, api_secret, source=name, proxy_url=_proxy_url())

        return Source(
            id=source_id,
            name=name,
            client=client,
            tracker=PositionTracker(source=name),
            url=footer_url,  # API keys/account info are NEVER put here; only an explicit public footer_url
            min_notional=min_notional,
            exchange="binance",
            account_fingerprint=(
                "binance:"
                + hashlib.sha256(f"{key_env}|{secret_env}".encode()).hexdigest()[:12]
            ),
            exclude_symbols=exclude_symbols,
        )

    log.warning("unknown source type %r for '%s' — skipping", stype, name)
    return None


def _load_sources_legacy(
    path: str | Path = "config.yaml",
    settings: "BotSettings | None" = None,
) -> list[Source]:
    """Retired pre-v2 parser retained only for historical compatibility tests."""
    p = Path(path)
    if not p.exists():
        raise RuntimeError(f"config file not found: {p}")
    with open(p, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    raw_sources = cfg.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise RuntimeError(f"{p} has no 'sources' list")

    sources: list[Source] = []
    seen_ids: set[str] = set()
    for raw in raw_sources:
        if not isinstance(raw, dict):
            continue
        src = _build_source(raw, settings=settings)
        if src is None:
            continue
        if src.id in seen_ids:
            log.warning("duplicate source %s — skipping", src.id)
            continue
        seen_ids.add(src.id)
        sources.append(src)

    if not sources:
        raise RuntimeError(f"no valid sources in {p}")
    log.info("loaded %d source(s): %s", len(sources), ", ".join(s.name for s in sources))
    return sources


def load_source_report(
    path: str | Path = "config.yaml",
    settings: "BotSettings | None" = None,
) -> SourceLoadReport:
    """Load active sources while retaining redacted disabled/error entries.

    A missing file, empty list, or entirely invalid configuration is reported
    instead of raised so the dashboard can stay online in configuration-only
    mode.
    """
    p = Path(path)
    if not p.exists():
        return SourceLoadReport(
            issues=[
                SourceConfigIssue("", "", "", "config_error", "config file not found")
            ]
        )
    with open(p, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    raw_sources = cfg.get("sources")
    if raw_sources is None:
        raw_sources = []
    if not isinstance(raw_sources, list):
        return SourceLoadReport(
            issues=[
                SourceConfigIssue("", "", "", "config_error", "sources must be a list")
            ]
        )

    report = SourceLoadReport()
    seen_ids: set[str] = set()
    seen_accounts: set[str] = set()
    for index, raw in enumerate(raw_sources):
        if not isinstance(raw, dict):
            report.issues.append(
                SourceConfigIssue(
                    f"entry-{index}", "", "", "config_error", "source must be a mapping"
                )
            )
            continue
        stype = str(raw.get("type", "")).lower().strip()
        name = str(raw.get("name", "")).strip()
        display_id = str(raw.get("id", "")).strip().lower() or f"entry-{index}"
        if raw.get("enabled") is False:
            report.issues.append(
                SourceConfigIssue(
                    display_id, name, stype, "disabled", "disabled by configuration"
                )
            )
            continue
        issue_detail = _source_issue_detail(raw)
        if issue_detail:
            issue_status = (
                "disabled"
                if issue_detail.startswith("missing environment variable")
                else "config_error"
            )
            report.issues.append(
                SourceConfigIssue(
                    display_id, name, stype, issue_status, issue_detail
                )
            )
            continue
        preview_id, preview_account = _preview_source_identity(raw)
        if preview_id in seen_ids:
            report.issues.append(
                SourceConfigIssue(
                    preview_id, name, stype, "config_error", "duplicate source id"
                )
            )
            continue
        if preview_account in seen_accounts:
            report.issues.append(
                SourceConfigIssue(
                    preview_id,
                    name,
                    stype,
                    "config_error",
                    "duplicate account/pool configuration",
                )
            )
            continue
        src = _build_source(raw, settings=settings)
        if src is None:
            report.issues.append(
                SourceConfigIssue(
                    display_id,
                    name,
                    stype,
                    "config_error",
                    "missing or invalid required configuration",
                )
            )
            continue
        seen_ids.add(src.id)
        if src.account_fingerprint:
            seen_accounts.add(src.account_fingerprint)
        report.sources.append(src)

    log.info(
        "loaded %d active source(s), %d disabled/invalid",
        len(report.sources),
        len(report.issues),
    )
    return report


# Deliberately redefine the compatibility entry point after the legacy parser:
# callers still receive list[Source], but zero usable sources no longer aborts.
def load_sources(
    path: str | Path = "config.yaml",
    settings: "BotSettings | None" = None,
) -> list[Source]:
    return load_source_report(path, settings=settings).sources
