"""Read-only adapters for the existing Crypto Scientist applications."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

from .lifecycles import reconstruct_lifecycles
from .store import CommandStore, fingerprint, iso, parse_time


def _direction(text: str) -> str:
    lower = text.lower()
    short_terms = (" down ", "drop", "breakdown", "bearish", "sell", "short")
    long_terms = (" up ", "surge", "breakout", "bullish", "buy", "long")
    if any(term in f" {lower} " for term in short_terms):
        return "short"
    if any(term in f" {lower} " for term in long_terms):
        return "long"
    return "neutral"


def _severity(value: str | None) -> str:
    value = (value or "medium").lower()
    return value if value in {"low", "medium", "high", "critical"} else "medium"


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _read_rows(path: Path, sql: str) -> list[sqlite3.Row]:
    if not path.exists():
        return []
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _pool_names(config_path: Path) -> dict[str, dict[str, str]]:
    if not config_path.exists():
        return {}
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    names: dict[str, dict[str, str]] = {}
    for protocol, group in (config.get("protocols") or {}).items():
        for pool in group.get("pools", []) or []:
            pool_id = pool.get("llama_pool")
            if pool_id:
                names[str(pool_id)] = {
                    "name": str(pool.get("name") or pool_id),
                    "protocol": str(protocol),
                    "chain": str((pool.get("aave") or {}).get("chain") or ""),
                }
    return names


class WorkspaceIngestor:
    def __init__(self, store: CommandStore, workspace: Path):
        self.store = store
        self.workspace = workspace
        self.lighter = workspace / "lighter-trade-bot"
        self.speculation = workspace / "speculation-alert-bot"
        self.hack = workspace / "hack-alert-bot"

    def sync(self) -> dict[str, int]:
        run_id = self.store.start_sync()
        signal_count = trade_count = outcome_count = 0
        position_count = 0
        try:
            # TVL/protocol-risk alerts are owned by the standalone TVL Monitor
            # (the deployed hack-alert app). Command Center no longer stores a
            # second copy or evaluates those outcomes.
            tvl_removed = self.store.purge_signal_source("hack")
            signal_count += self._speculation_signals()
            outcome_count += self._market_outcomes()
            self.store.finish_sync(
                run_id, signals=signal_count, trades=trade_count, outcomes=outcome_count
            )
            return {
                "signals": signal_count,
                "trades": trade_count,
                "positions": position_count,
                "outcomes": outcome_count,
                "tvl_removed": tvl_removed,
            }
        except Exception as exc:
            self.store.finish_sync(
                run_id, signals=signal_count, trades=trade_count,
                outcomes=outcome_count, error=str(exc),
            )
            raise

    def sync_trading(self) -> dict[str, int]:
        """Refresh only the standalone Trade Journal's derived trade state."""
        run_id = self.store.start_journal_sync()
        trade_count = position_count = 0
        try:
            self._trades()
            trade_count = self._lifecycles()
            position_count = self._positions()
            self.store.finish_journal_sync(
                run_id, trades=trade_count, positions=position_count
            )
            return {"trades": trade_count, "positions": position_count}
        except Exception as exc:
            self.store.finish_journal_sync(
                run_id, trades=trade_count, positions=position_count,
                error=str(exc),
            )
            raise

    def _speculation_signals(self) -> int:
        rows = _read_rows(
            self.speculation / "data" / "state.db",
            "SELECT * FROM alert_history ORDER BY id",
        )
        imported = 0
        for row in rows:
            title = str(row["title"] or "Market anomaly")
            detector = str(row["detector"] or "")
            is_simulation = detector.lower().startswith(("sample-", "test-")) or detector.lower() in {
                "sample", "test", "test-fire"
            }
            _, created = self.store.upsert_signal(
                {
                    "fingerprint": fingerprint("speculation", row["id"], row["ts"]),
                    "source": "speculation",
                    "source_ref": str(row["id"]),
                    "detector": detector,
                    "event_type": "market_signal",
                    "occurred_at": iso(parse_time(row["ts"])),
                    "symbol": row["symbol"],
                    "direction": _direction(title + " " + str(row["message"] or "")),
                    "title": title,
                    "summary": str(row["message"] or ""),
                    "severity": _severity(row["severity"]),
                    "confidence": min(1.0, abs(float(row["value"] or 0)) / 5),
                    "is_simulation": is_simulation,
                    "observed_value": _float(row["value"]),
                    "metadata": {"raw_value": row["value"]},
                }
            )
            imported += int(created)
        return imported

    def _hack_signals(self) -> int:
        state = self.hack / "alertbot_state.db"
        names = _pool_names(self.hack / "config.yaml")
        alerts = _read_rows(state, "SELECT rowid AS source_id, * FROM alerts ORDER BY ts")
        imported = 0
        for row in alerts:
            detail = names.get(str(row["pool_id"]), {})
            name = detail.get("name", str(row["pool_id"]))
            baseline = _float(row["baseline_usd"])
            value = _float(row["value_usd"])
            drop = ((1 - value / baseline) * 100) if baseline and value is not None else None
            severity = "critical" if drop is not None and drop >= 50 else "high"
            title = f"{name}: {str(row['rule']).replace('_', ' ')}"
            summary = (
                f"Observed ${value:,.0f} versus ${baseline:,.0f} baseline "
                f"({drop:.1f}% lower)." if drop is not None else "Protocol risk threshold fired."
            )
            _, created = self.store.upsert_signal(
                {
                    "fingerprint": fingerprint(
                        "hack", row["pool_id"], row["rule"], row["ts"]
                    ),
                    "source": "hack",
                    "source_ref": str(row["source_id"]),
                    "detector": row["rule"],
                    "event_type": "defi_risk",
                    "occurred_at": iso(parse_time(row["ts"])),
                    "symbol": name,
                    "direction": "short",
                    "title": title,
                    "summary": summary,
                    "severity": severity,
                    "confidence": 0.9 if severity == "critical" else 0.7,
                    "baseline_value": baseline,
                    "observed_value": value,
                    "metadata": {
                        "pool_id": row["pool_id"], "protocol": detail.get("protocol"),
                        "chain": detail.get("chain"), "rule": row["rule"],
                        "cleared": bool(row["cleared"]),
                    },
                }
            )
            imported += int(created)

        correlations = _read_rows(
            state, "SELECT rowid AS source_id, * FROM correlation_alerts ORDER BY ts"
        )
        for row in correlations:
            title = f"Correlated {row['scope']} incident: {row['scope_key']}"
            _, created = self.store.upsert_signal(
                {
                    "fingerprint": fingerprint("hack-correlation", row["fingerprint"]),
                    "source": "hack",
                    "source_ref": f"correlation:{row['source_id']}",
                    "detector": "incident_correlation",
                    "event_type": "defi_incident",
                    "occurred_at": iso(parse_time(row["ts"])),
                    "symbol": row["scope_key"],
                    "direction": "short",
                    "title": title,
                    "summary": f"{row['pool_count']} pools corroborate this incident.",
                    "severity": _severity(row["severity"]),
                    "confidence": _float(row["confidence"]),
                    "metadata": {
                        "scope": row["scope"], "pool_ids": json.loads(row["pool_ids"] or "[]")
                    },
                }
            )
            imported += int(created)
        return imported

    def _trades(self) -> int:
        rows = _read_rows(
            self.lighter / "data" / "events.db",
            "SELECT * FROM closed_trades ORDER BY id",
        )
        imported = 0
        for row in rows:
            native = row["trade_id"] if "trade_id" in row.keys() else None
            _, created = self.store.upsert_trade(
                {
                    "fingerprint": fingerprint(
                        "trade", row["source"], native or row["id"], row["ts"]
                    ),
                    "source": str(row["source"] or "unknown"),
                    "native_trade_id": str(native) if native is not None else None,
                    "occurred_at": iso(parse_time(row["ts"])),
                    "symbol": str(row["market_symbol"] or "UNKNOWN"),
                    "side": str(row["side"] or "unknown").lower(),
                    "entry": _float(row["entry"]),
                    "exit": _float(row["exit"]),
                    "size": _float(row["size"]),
                    "notional": _float(row["notional"]),
                    "pnl": _float(row["pnl"]),
                    "pnl_pct": _float(row["pct"]),
                    "is_win": row["is_win"],
                    "metadata": {
                        "card_path": row["card_path"],
                        "fill_ids": (
                            json.loads(row["fill_ids"] or "[]")
                            if "fill_ids" in row.keys() else []
                        ),
                        "realization_kind": (
                            row["realization_kind"]
                            if "realization_kind" in row.keys() else None
                        ),
                    },
                }
            )
            imported += int(created)
        return imported

    def _lifecycles(self) -> int:
        rows = _read_rows(
            self.lighter / "data" / "events.db",
            "SELECT id, ts, payload FROM events ORDER BY id",
        )
        lifecycles = reconstruct_lifecycles(dict(row) for row in rows)
        return self.store.replace_trade_lifecycles(lifecycles)

    def _positions(self) -> int:
        """Reconstruct current positions from the last event for each market."""
        rows = _read_rows(
            self.lighter / "data" / "events.db",
            "SELECT id, ts, payload FROM events ORDER BY id",
        )
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                continue
            trade = payload.get("trade") or {}
            before = payload.get("position_before") or {}
            after = payload.get("position_after")
            source = str(
                trade.get("source") or (after or {}).get("source")
                or before.get("source") or "unknown"
            )
            symbol = str(
                trade.get("market_symbol") or (after or {}).get("market_symbol")
                or before.get("market_symbol") or ""
            )
            if not symbol:
                continue
            key = f"{source}:{symbol}"
            if not after or _float(after.get("size")) in (None, 0):
                latest.pop(key, None)
                continue
            latest[key] = {
                "position_key": key,
                "source": source,
                "symbol": symbol,
                "side": str(after.get("side") or "unknown").lower(),
                "size": abs(float(after["size"])),
                "entry": _float(after.get("avg_entry_price")),
                "unrealized_pnl": _float(after.get("unrealized_pnl")),
                "liquidation_price": _float(after.get("liquidation_px")),
                "updated_at": iso(parse_time(row["ts"])),
                "metadata": {"event_id": row["id"], "event_kind": payload.get("kind")},
            }
        return self.store.replace_positions(latest.values())

    def _market_outcomes(self) -> int:
        db = self.speculation / "data" / "candles.db"
        if not db.exists():
            return 0
        pending = [
            item for item in self.store.pending_outcomes()
            if item["source"] == "speculation" and item.get("symbol")
            and item["symbol"] != "MARKET"
        ]
        if not pending:
            return 0
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        completed = 0
        try:
            for item in pending:
                symbol = str(item["symbol"])
                occurred_ms = int(parse_time(item["occurred_at"]).timestamp() * 1000)
                due_ms = int(parse_time(item["due_at"]).timestamp() * 1000)
                candidates = [symbol]
                if not symbol.endswith(":USDT"):
                    candidates.append(symbol + ":USDT")
                baseline = outcome = None
                highs: list[float] = []
                lows: list[float] = []
                used_symbol = None
                for candidate in candidates:
                    baseline = con.execute(
                        """
                        SELECT close FROM candles WHERE symbol=? AND timestamp<=?
                        ORDER BY timestamp DESC LIMIT 1
                        """,
                        (candidate, occurred_ms),
                    ).fetchone()
                    outcome = con.execute(
                        """
                        SELECT timestamp, close FROM candles
                        WHERE symbol=? AND timestamp>=?
                        ORDER BY timestamp LIMIT 1
                        """,
                        (candidate, due_ms),
                    ).fetchone()
                    if baseline and outcome:
                        used_symbol = candidate
                        interval = con.execute(
                            """
                            SELECT high, low FROM candles WHERE symbol=?
                              AND timestamp BETWEEN ? AND ?
                            """,
                            (candidate, occurred_ms, int(outcome["timestamp"])),
                        ).fetchall()
                        highs = [float(row["high"]) for row in interval]
                        lows = [float(row["low"]) for row in interval]
                        break
                if not baseline or not outcome:
                    continue
                base = float(baseline["close"])
                direction = item["direction"]
                high_return = ((max(highs or [base]) / base) - 1) * 100
                low_return = ((min(lows or [base]) / base) - 1) * 100
                if direction == "short":
                    mfe, mae = -low_return, -high_return
                else:
                    mfe, mae = high_return, low_return
                completed += int(
                    self.store.complete_outcome(
                        int(item["signal_id"]), int(item["horizon_minutes"]),
                        baseline=base, outcome=float(outcome["close"]),
                        mfe=mfe, mae=mae,
                        captured_at=iso(parse_time(int(outcome["timestamp"]) / 1000)),
                        metadata={"market_symbol": used_symbol, "source": "candles.db"},
                    )
                )
        finally:
            con.close()
        return completed

    def _hack_outcomes(self) -> int:
        state = self.hack / "alertbot_state.db"
        if not state.exists():
            return 0
        pending = [
            item for item in self.store.pending_outcomes()
            if item["source"] == "hack" and item["signal_metadata"].get("pool_id")
        ]
        if not pending:
            return 0
        con = sqlite3.connect(f"file:{state.as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        completed = 0
        try:
            for item in pending:
                pool_id = item["signal_metadata"]["pool_id"]
                signal = self.store.get_signal(int(item["signal_id"]))
                baseline = _float(signal.get("baseline_value") if signal else None)
                if not baseline:
                    continue
                due_ts = int(parse_time(item["due_at"]).timestamp())
                reading = con.execute(
                    """
                    SELECT ts, value_usd FROM readings
                    WHERE pool_id=? AND ts>=? ORDER BY ts LIMIT 1
                    """,
                    (pool_id, due_ts),
                ).fetchone()
                if not reading:
                    continue
                completed += int(
                    self.store.complete_outcome(
                        int(item["signal_id"]), int(item["horizon_minutes"]),
                        baseline=baseline, outcome=float(reading["value_usd"]),
                        mfe=None, mae=None,
                        captured_at=iso(parse_time(reading["ts"])),
                        metadata={"source": "alertbot readings", "pool_id": pool_id},
                    )
                )
        finally:
            con.close()
        return completed
