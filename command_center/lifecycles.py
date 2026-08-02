"""Reconstruct position-level trades from immutable execution events."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Iterable


BATCH_WINDOW_SECONDS = 120


def _number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _time(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed


def _action(payload: dict[str, Any]) -> str:
    trade = payload.get("trade") or {}
    kind = str(payload.get("kind") or "").upper()
    before = payload.get("position_before") or {}
    after = payload.get("position_after") or {}
    before_size = abs(_number(before.get("size")) or 0)
    after_size = abs(_number(after.get("size")) or 0)
    direction = str(trade.get("dir") or "").lower()
    start_position = _number(trade.get("start_position"))
    # Hyperliquid's own direction and signed starting position outrank the
    # locally normalized kind, which can be wrong in historical same-timestamp
    # bursts. Older sources without start_position retain kind-first behavior.
    if start_position is not None:
        if direction.startswith("close"):
            return "exit"
        if direction.startswith("open"):
            return "entry"
    if kind in {"CLOSE", "REDUCE"}:
        return "exit"
    if kind == "OPEN":
        return "entry"
    realized = _number(trade.get("realized_pnl"))
    if realized is None:
        realized = _number(trade.get("closed_pnl"))
    if realized not in (None, 0):
        return "exit"
    if direction.startswith("close"):
        return "exit"
    if direction.startswith("open"):
        return "entry"
    if not before and after:
        return "entry"
    if before and not after:
        return "exit"
    before_side = str(before.get("side") or "").lower()
    after_side = str(after.get("side") or "").lower()
    if before_side and after_side and before_side != after_side:
        return "exit" if kind != "OPEN" else "entry"
    if after_size > before_size:
        return "entry"
    if after_size < before_size:
        return "exit"
    return "unknown"


def _direction_sides(direction: Any) -> tuple[str | None, str | None]:
    text = str(direction or "").strip().lower()
    if ">" in text:
        before, after = (part.strip() for part in text.split(">", 1))
        return (
            before if before in {"long", "short"} else None,
            after if after in {"long", "short"} else None,
        )
    for prefix, side in (
        ("close long", "long"),
        ("close short", "short"),
        ("open long", "long"),
        ("open short", "short"),
    ):
        if text.startswith(prefix):
            return (side, None) if prefix.startswith("close") else (None, side)
    return None, None


def _execution(row: dict[str, Any]) -> dict[str, Any] | None:
    try:
        payload = json.loads(row.get("payload") or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    trade = payload.get("trade") or {}
    before = payload.get("position_before") or {}
    after = payload.get("position_after") or {}
    source = str(
        trade.get("source") or after.get("source") or before.get("source") or "unknown"
    )
    symbol = str(
        trade.get("market_symbol")
        or after.get("market_symbol")
        or before.get("market_symbol")
        or ""
    )
    if not symbol:
        return None
    occurred_at = str(trade.get("timestamp") or row.get("ts") or "")
    if not occurred_at:
        return None
    action = _action(payload)
    if action == "unknown":
        return None
    start_position = _number(trade.get("start_position"))
    before_size = abs(_number(before.get("size")) or 0)
    after_size = abs(_number(after.get("size")) or 0)
    raw_size = abs(_number(trade.get("size")) or 0)
    if action == "exit" and start_position not in (None, 0):
        before_size = abs(float(start_position))
        size = min(raw_size, before_size)
        after_size = max(0.0, before_size - size)
    elif action == "entry" and start_position is not None:
        if ">" in str(trade.get("dir") or ""):
            before_size = 0.0
            size = after_size or max(0.0, raw_size - abs(float(start_position)))
            after_size = size
        else:
            before_size = abs(float(start_position))
            size = raw_size
            after_size = before_size + size
    elif action == "entry" and after:
        size = max(0.0, after_size - before_size) or raw_size
    elif action == "exit" and before:
        size = max(0.0, before_size - after_size) or min(raw_size, before_size)
    else:
        size = raw_size
    if size <= 0:
        return None
    position = before if action == "exit" else after
    old_direction_side, new_direction_side = _direction_sides(trade.get("dir"))
    if action == "exit":
        if start_position not in (None, 0):
            side = "long" if float(start_position) > 0 else "short"
        elif old_direction_side:
            side = old_direction_side
        elif position.get("side"):
            side = str(position["side"]).lower()
        else:
            execution_side = str(trade.get("side") or "").lower()
            side = {"long": "short", "short": "long"}.get(execution_side, "unknown")
    else:
        side = str(
            new_direction_side
            or position.get("side")
            or trade.get("side")
            or "unknown"
        ).lower()
    native_id = trade.get("trade_id")
    raw_key = "|".join(
        str(value)
        for value in (
            source,
            native_id or "",
            trade.get("tx_hash") or "",
            symbol,
            occurred_at,
            trade.get("price") or "",
            raw_size,
        )
    )
    execution_key = (
        f"{source}:{native_id}"
        if native_id not in (None, "")
        else hashlib.sha256(raw_key.encode()).hexdigest()[:32]
    )
    if ">" in str(trade.get("dir") or ""):
        execution_key = f"{execution_key}:{action}"
    if action == "exit" and start_position not in (None, 0):
        # Hyperliquid's signed start position gives the exact remaining leg.
        # A historical row may be labelled CLOSE even when this particular
        # fill only reduced part of the position.
        final = after_size <= 1e-12
    else:
        final = (
            str(payload.get("kind") or "").upper() == "CLOSE"
            or (action == "exit" and not after)
            or (action == "exit" and after_size == 0)
        )
    realized = _number(trade.get("realized_pnl"))
    if realized is None:
        realized = _number(trade.get("closed_pnl"))
    price = _number(trade.get("price"))
    position_entry = _number(position.get("avg_entry_price"))
    pnl_basis = "exchange_reported" if realized is not None else "unavailable"
    if action == "entry":
        realized = 0.0
        pnl_basis = "entry"
    elif action == "exit" and realized is None and position_entry is not None:
        # Lighter does not send closedPnl. Its position snapshot immediately
        # before the fill is the authoritative cost basis, especially after
        # scale-ins change the average entry. Never substitute a lifecycle-wide
        # VWAP at this boundary; that basis can be wrong for every later exit.
        direction = -1 if side == "short" else 1
        realized = (price - position_entry) * size * direction if price is not None else None
        pnl_basis = "exchange_position_before" if realized is not None else "unavailable"
    implied_entry = None
    if action == "exit" and realized not in (None, 0) and price is not None:
        direction = -1 if side == "short" else 1
        implied_entry = price - (float(realized) / size) * direction
    return {
        "execution_key": execution_key,
        "event_id": int(row.get("id") or 0),
        "occurred_at": occurred_at,
        "source": source,
        "symbol": symbol,
        "side": side,
        "action": action,
        "final": final,
        "price": price,
        "size": size,
        "pnl": realized,
        "pnl_basis": pnl_basis,
        "implied_entry": implied_entry,
        "position_before": before_size,
        "position_after": after_size,
        "position_entry": position_entry,
        "native_trade_id": str(native_id) if native_id not in (None, "") else None,
        "transaction_id": trade.get("tx_hash"),
        "event_kind": payload.get("kind"),
    }


def _quality(item: dict[str, Any]) -> tuple[int, int]:
    score = 0
    if item["final"]:
        score += 8
    if item.get("pnl") not in (None, 0):
        score += 4
    if item.get("position_before") or item.get("position_after"):
        score += 2
    if item.get("side") not in (None, "unknown"):
        score += 1
    return score, -int(item.get("event_id") or 0)


def _weighted(items: Iterable[dict[str, Any]], field: str) -> float | None:
    usable = [
        item for item in items
        if item.get(field) is not None and (item.get("size") or 0) > 0
    ]
    total = sum(float(item["size"]) for item in usable)
    if not total:
        return None
    return sum(float(item[field]) * float(item["size"]) for item in usable) / total


def _batch_executions(items: list[dict[str, Any]], *, closed: bool) -> list[dict[str, Any]]:
    batches: list[dict[str, Any]] = []
    for item in items:
        family = item["action"]
        previous = batches[-1] if batches else None
        gap = (
            (_time(item["occurred_at"]) - _time(previous["ended_at"])).total_seconds()
            if previous else None
        )
        if previous and previous["family"] == family and gap is not None and gap <= BATCH_WINDOW_SECONDS:
            batch = previous
        else:
            batch = {
                "batch_key": hashlib.sha256(
                    f"{item['execution_key']}|{family}".encode()
                ).hexdigest()[:24],
                "family": family,
                "started_at": item["occurred_at"],
                "ended_at": item["occurred_at"],
                "executions": [],
            }
            batches.append(batch)
        batch["ended_at"] = item["occurred_at"]
        batch["executions"].append(item)

    entry_index = 0
    exit_batches = [batch for batch in batches if batch["family"] == "exit"]
    for batch in batches:
        executions = batch["executions"]
        batch["fill_count"] = len(executions)
        batch["size"] = sum(float(item.get("size") or 0) for item in executions)
        batch["price"] = _weighted(executions, "price")
        pnl_values = [item.get("pnl") for item in executions if item.get("pnl") is not None]
        batch["pnl"] = sum(float(value) for value in pnl_values) if pnl_values else None
        if batch["family"] == "entry":
            batch["label"] = "Entry" if entry_index == 0 else "Scale-in"
            entry_index += 1
        else:
            is_final = closed and batch is exit_batches[-1]
            if is_final:
                batch["label"] = "Final exit"
            elif (batch["pnl"] or 0) > 0:
                batch["label"] = "Partial profit"
            elif (batch["pnl"] or 0) < 0:
                batch["label"] = "Partial loss"
            else:
                batch["label"] = "Scale-out"
        for item in executions:
            item["batch_key"] = batch["batch_key"]
            item["batch_label"] = batch["label"]
        del batch["executions"]
    return batches


def reconstruct_lifecycles(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return stable position lifecycles built from deduplicated execution events."""
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = _execution(dict(row))
        if not item:
            continue
        current = unique.get(item["execution_key"])
        if current is None or _quality(item) > _quality(current):
            unique[item["execution_key"]] = item

    executions = sorted(
        unique.values(),
        key=lambda item: (
            _time(item["occurred_at"]),
            item["source"],
            item["symbol"],
            0 if item["action"] == "exit" else 1,
            -float(item.get("position_before") or 0)
            if item["action"] == "exit" else 0,
            item["event_id"],
        ),
    )
    active: dict[tuple[str, str], dict[str, Any]] = {}
    completed: list[dict[str, Any]] = []

    def start(item: dict[str, Any], inferred: bool = False) -> dict[str, Any]:
        lifecycle_key = hashlib.sha256(
            f"{item['source']}|{item['symbol']}|{item['execution_key']}".encode()
        ).hexdigest()[:32]
        lifecycle = {
            "lifecycle_key": lifecycle_key,
            "source": item["source"],
            "symbol": item["symbol"],
            "side": item["side"],
            "opened_at": item["occurred_at"],
            "closed_at": None,
            "status": "open",
            "inferred_open": inferred,
            "executions": [],
        }
        completed.append(lifecycle)
        active[(item["source"], item["symbol"])] = lifecycle
        return lifecycle

    for item in executions:
        key = (item["source"], item["symbol"])
        lifecycle = active.get(key)
        if (
            lifecycle is not None
            and item["side"] not in ("unknown", lifecycle["side"])
            and lifecycle["side"] != "unknown"
        ):
            lifecycle["status"] = "reversed"
            lifecycle["closed_at"] = item["occurred_at"]
            lifecycle["reversed_to"] = item["side"]
            lifecycle["reversal_price"] = item.get("price")
            active.pop(key, None)
            lifecycle = None
        if item["action"] == "entry":
            if lifecycle is None or lifecycle["status"] == "closed":
                lifecycle = start(item)
        elif lifecycle is None or lifecycle["status"] == "closed":
            lifecycle = start(item, inferred=True)
            inferred_entry = item.get("position_entry")
            if inferred_entry is not None:
                lifecycle["inferred_entry"] = inferred_entry
        lifecycle["executions"].append(item)
        if item["action"] == "exit" and item["final"]:
            lifecycle["status"] = "closed"
            lifecycle["closed_at"] = item["occurred_at"]
            active.pop(key, None)

    for lifecycle in completed:
        items = lifecycle["executions"]
        entries = [item for item in items if item["action"] == "entry"]
        exits = [item for item in items if item["action"] == "exit"]
        implied_entry = _weighted(exits, "implied_entry")
        if lifecycle.get("inferred_open") and implied_entry is not None:
            entry_vwap = implied_entry
        else:
            entry_vwap = _weighted(entries, "price") or lifecycle.get("inferred_entry")
        if entry_vwap is None and exits:
            entry_vwap = exits[0].get("position_entry")
        exit_vwap = _weighted(exits, "price")
        max_size = max(
            [
                float(item.get("position_before") or 0)
                for item in items
            ] + [
                float(item.get("position_after") or 0)
                for item in items
            ] + [sum(float(item.get("size") or 0) for item in entries)]
        )
        closed_size = sum(float(item.get("size") or 0) for item in exits)
        for item in exits:
            if item.get("pnl") is None and item.get("price") is not None:
                # Historical rows may lack the exchange's before-position
                # basis. Keep the fallback explicit and auditable rather than
                # silently presenting it as exchange truth.
                fallback_entry = entry_vwap
                if fallback_entry is not None:
                    direction = -1 if lifecycle["side"] == "short" else 1
                    item["pnl"] = (
                        (float(item["price"]) - float(fallback_entry))
                        * float(item["size"])
                        * direction
                    )
                    item["pnl_basis"] = "lifecycle_entry_fallback"
        pnl_values = [item.get("pnl") for item in exits if item.get("pnl") is not None]
        pnl = sum(float(value) for value in pnl_values) if pnl_values else None
        entry_repaired = False
        if (
            implied_entry is not None
            and entry_vwap is not None
            and exit_vwap is not None
            and pnl not in (None, 0)
        ):
            direction = -1 if lifecycle["side"] == "short" else 1
            expected_move = (float(exit_vwap) - float(entry_vwap)) * direction
            if expected_move * float(pnl) < 0:
                entry_vwap = implied_entry
                entry_repaired = True
        denominator = (entry_vwap or 0) * closed_size
        pnl_pct = (pnl / denominator * 100) if pnl is not None and denominator else None
        batches = _batch_executions(items, closed=lifecycle["status"] == "closed")
        if lifecycle["status"] == "reversed":
            batches.append(
                {
                    "batch_key": hashlib.sha256(
                        f"{lifecycle['lifecycle_key']}|reversal".encode()
                    ).hexdigest()[:24],
                    "family": "management",
                    "started_at": lifecycle["closed_at"],
                    "ended_at": lifecycle["closed_at"],
                    "fill_count": 0,
                    "size": 0,
                    "price": lifecycle.get("reversal_price"),
                    "pnl": None,
                    "label": f"Direction reversal → {lifecycle.get('reversed_to','unknown')}",
                }
            )
        entry_batches = [batch for batch in batches if batch["family"] == "entry"]
        exit_batches = [batch for batch in batches if batch["family"] == "exit"]
        partials = max(0, len(exit_batches) - (1 if lifecycle["status"] == "closed" else 0))
        scaled_in = len(entry_batches) > 1
        scaled_out = partials > 0
        if lifecycle["status"] == "reversed":
            style = "Direction reversed"
        elif scaled_in and scaled_out:
            style = "Scaled in & out"
        elif scaled_in:
            style = "Scaled in"
        elif scaled_out:
            style = "Scaled out"
        else:
            style = "Single entry / exit"
        lifecycle.update(
            {
                "entry_vwap": entry_vwap,
                "exit_vwap": exit_vwap,
                "max_size": max_size,
                "closed_size": closed_size,
                "notional": (entry_vwap * max_size) if entry_vwap is not None else None,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "is_win": None if pnl is None else int(pnl > 0),
                "fill_count": len(items),
                "entry_batch_count": len(entry_batches),
                "exit_batch_count": len(exit_batches),
                "partial_exit_count": partials,
                "management_style": style,
                "entry_repaired": entry_repaired,
                "batches": batches,
            }
        )
    return sorted(completed, key=lambda item: _time(item["opened_at"]))
