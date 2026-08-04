"""Deterministic projection evidence helpers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal

from .models import AccountProjection


def _primitive(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "value") and not isinstance(value, (str, bytes)):
        return value.value
    if isinstance(value, tuple):
        return [_primitive(item) for item in value]
    if isinstance(value, list):
        return [_primitive(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _primitive(item) for key, item in sorted(value.items())}
    return value


def projection_payload(projection: AccountProjection) -> dict:
    """Return a canonical, timestamp-free payload suitable for hashing."""
    return _primitive(asdict(projection))


def projection_hash(projection: AccountProjection) -> str:
    encoded = json.dumps(
        projection_payload(projection), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def execution_snapshot_hash(executions) -> str:
    rows = []
    for item in sorted(executions, key=lambda value: (value.occurred_at, value.execution_uid)):
        rows.append(
            [
                item.execution_uid,
                item.account_id,
                item.exchange,
                item.market_key,
                item.position_side.value,
                item.native_trade_id,
                item.occurred_at.isoformat(),
                item.side.value,
                str(item.quantity),
                str(item.price),
                str(item.fee),
            ]
        )
    encoded = json.dumps(rows, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
