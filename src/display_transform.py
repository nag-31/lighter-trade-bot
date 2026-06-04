"""Display-value transforms for Hyperliquid (HL) privacy obfuscation.

Posture: "exact results, fuzzy fingerprint."
  - PnL / % / win-rate are handled elsewhere and remain EXACT.
  - This module fuzzes ONLY: price, size, notional, timestamps.
  - HL-only: when `is_hl` is False, or `params.enabled` is False,
    every function returns the real (un-transformed) value.
  - Pure stdlib + decimal.Decimal.  No I/O, no logging side-effects.
  - Never mutates inputs.
"""

from __future__ import annotations

import hashlib
import hmac
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional


# ---------------------------------------------------------------------------
# PrivacyParams
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PrivacyParams:
    """Frozen config for the display-transform layer.

    Field names map 1:1 to BotSettings privacy_* keys so callers can build
    a PrivacyParams with a simple attribute fan-out.
    """

    enabled: bool
    secret: bytes                  # HMAC salt; str accepted, encoded in __post_init__
    mag: float                     # price jitter magnitude, e.g. 0.003 = ±0.3%
    entry_quantum_pct: float       # coarse bucket = this fraction of price, e.g. 0.005
    size_sigfigs: int
    notional_sigfigs: int
    time_bucket: str               # "exact" | "hour" | "relative"
    disclose_footnote: bool
    footnote_text: str = "Prices/sizes approx for privacy · PnL & % exact"

    def __post_init__(self) -> None:
        # Accept str secret — encode to bytes
        if isinstance(self.secret, str):
            object.__setattr__(self, "secret", self.secret.encode("utf-8"))
        # Validate mag only when privacy is enabled
        if self.enabled and not (0 < self.mag < 0.05):
            raise ValueError(
                f"privacy_mag must be in (0, 0.05) when enabled; got {self.mag!r}"
            )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _sigfig_round(value: Decimal, sigfigs: int) -> Decimal:
    """Round *value* to *sigfigs* significant figures, preserving sign.

    Special cases:
        0  → Decimal("0")
        negative → round magnitude, then negate
    """
    if value == Decimal(0):
        return Decimal("0")
    sign = -1 if value < 0 else 1
    abs_val = abs(value)
    # magnitude: e.g. 9732 → mag=3, shift so first digit lands in units place
    magnitude = int(math.floor(math.log10(float(abs_val))))
    shift = sigfigs - 1 - magnitude
    factor = Decimal(10) ** shift
    rounded = (abs_val * factor).to_integral_value(ROUND_HALF_UP) / factor
    return Decimal(sign) * rounded


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def price_factor(
    params: PrivacyParams,
    source_id: str,
    symbol: str,
    side: str,
    anchor_entry: Decimal,
) -> Decimal:
    """Return a deterministic jitter factor f ∈ [1-mag, 1+mag].

    The factor is stable for the lifetime of a position because it is seeded
    by the OPEN-time anchor entry price (quantised to a bucket), not a live
    drifting average.

    Returns Decimal("1") when:
      - params.enabled is False
      - anchor_entry <= 0
      - division by zero would occur in bucket calculation
    """
    if not params.enabled:
        return Decimal("1")

    if anchor_entry <= 0:
        return Decimal("1")

    mag = params.mag
    entry_quantum_pct = params.entry_quantum_pct

    # q is the bucket-step in price units: prices within q of each other share
    # a bucket (coarse-grain the key so tiny drifts don't change f mid-position).
    # We treat entry_quantum_pct as a fraction of the anchor itself, so q = anchor * pct.
    # The bucket index is then round(anchor / q) = round(1/pct) — which would be
    # invariant.  Instead we want the ABSOLUTE bucket: which multiple of q does
    # anchor fall into?  Use floor(anchor / q) = floor(1/pct) is still invariant.
    # Correct discriminating formula: bucket the raw price into slots of size q.
    # q_abs = entry_quantum_pct (a fractional step applied to a unit price = 1).
    # bucket = round(anchor / q_abs) where q_abs = anchor * entry_quantum_pct gives
    # bucket = round(anchor / (anchor * pct)) = round(1/pct). Still invariant.
    #
    # The only way to make anchor-dependent buckets: use q as an absolute dollar
    # step (not percentage-of-anchor). Interpret entry_quantum_pct as meaning
    # "bucket width = entry_quantum_pct fraction of the anchor at open-time" —
    # i.e., two anchors that differ by <q are in the SAME bucket. We compute
    # which multiple-of-q the anchor falls on:
    #   q = anchor_entry * entry_quantum_pct  (absolute dollar width)
    #   bucket = round(anchor_entry / q)      (= 1/pct, invariant — BAD)
    #
    # CORRECT formula: anchor rounded DOWN to the nearest multiple of q gives the
    # bucket; since q itself depends on anchor, we use a fixed reference:
    #   bucket = floor(anchor / q_abs)  where q_abs = entry_quantum_pct (unit)
    # This makes bucket ~ 50000 / 0.005 = 10_000_000 for BTC, with neighbouring
    # prices 50000 and 50001 in the same bucket when pct=0.005 (q_abs=0.005),
    # but 50000 and 52000 in different buckets.
    q_abs = Decimal(str(entry_quantum_pct))
    if q_abs == 0:
        return Decimal("1")

    entry_bucket = int((anchor_entry / q_abs).to_integral_value(ROUND_HALF_UP))

    digest = hmac.new(
        params.secret,
        f"{source_id}|{symbol}|{side}|{entry_bucket}".encode(),
        hashlib.sha256,
    ).digest()

    u = int.from_bytes(digest[:8], "big") / (2 ** 64)   # uniform [0, 1)
    f = 1.0 + mag * (2 * u - 1.0)
    return Decimal(str(f))


def disp_price(
    params: PrivacyParams,
    is_hl: bool,
    f: Decimal,
    real_price: Decimal,
) -> Decimal:
    """Return the display price: real_price * f.

    Falls back to real_price when disabled, non-HL, or real_price <= 0.
    """
    if not params.enabled or not is_hl or real_price <= 0:
        return real_price
    return real_price * f


def disp_size(
    params: PrivacyParams,
    is_hl: bool,
    real_size: Decimal,
) -> Decimal:
    """Return real_size rounded to params.size_sigfigs significant figures.

    Falls back to real_size when disabled or non-HL.
    """
    if not params.enabled or not is_hl:
        return real_size
    return _sigfig_round(real_size, params.size_sigfigs)


def disp_notional(
    params: PrivacyParams,
    is_hl: bool,
    real_notional: Decimal,
) -> Decimal:
    """Return real_notional rounded to params.notional_sigfigs significant figures.

    Falls back to real_notional when disabled or non-HL.
    """
    if not params.enabled or not is_hl:
        return real_notional
    return _sigfig_round(real_notional, params.notional_sigfigs)


def disp_time(
    params: PrivacyParams,
    is_hl: bool,
    ts: str | datetime,
    now: Optional[datetime] = None,
) -> str:
    """Coarsen a timestamp according to params.time_bucket.

    Modes (only applied when enabled and is_hl):
        "exact"    — original string unchanged
        "hour"     — ISO timestamp with minutes/seconds zeroed, tz preserved
        "relative" — human string vs *now*: "just now", "~Nm ago", "~Nh ago",
                     "~Nd ago".  If *now* is None, behaves as "exact".

    *ts* may be an ISO string or a datetime.  When the string cannot be
    parsed, str(ts) is returned unchanged.
    """
    # Determine whether to apply transforms
    if not params.enabled or not is_hl:
        return str(ts)

    bucket = params.time_bucket
    if bucket == "exact":
        return str(ts)

    # Parse ts
    dt: Optional[datetime] = None
    if isinstance(ts, datetime):
        dt = ts
    else:
        try:
            dt = datetime.fromisoformat(str(ts))
        except (ValueError, TypeError):
            return str(ts)

    if bucket == "hour":
        truncated = dt.replace(minute=0, second=0, microsecond=0)
        return truncated.isoformat()

    if bucket == "relative":
        if now is None:
            # Fall back to exact behaviour
            return str(ts)
        delta = now - dt
        total_seconds = delta.total_seconds()
        if total_seconds < 0:
            # ts is in the future relative to now — treat as exact
            return str(ts)
        minutes = int(total_seconds / 60)
        if minutes < 5:
            return "just now"
        hours = int(total_seconds / 3600)
        if hours < 1:
            return f"~{minutes}m ago"
        days = int(total_seconds / 86400)
        if days < 1:
            return f"~{hours}h ago"
        return f"~{days}d ago"

    # Unknown bucket — exact
    return str(ts)


def footnote(params: PrivacyParams, is_hl: bool) -> str:
    """Return the privacy footnote string, or "" when not applicable."""
    if params.enabled and is_hl and params.disclose_footnote:
        return params.footnote_text
    return ""


def disp_view(
    params: PrivacyParams,
    is_hl: bool,
    source_id: str,
    symbol: str,
    side: str,
    anchor_entry: Decimal,
    *,
    entry: Optional[Decimal] = None,
    exit: Optional[Decimal] = None,
    size: Optional[Decimal] = None,
    notional: Optional[Decimal] = None,
    sl: Optional[Decimal] = None,
    tp: Optional[Decimal] = None,
    liq: Optional[Decimal] = None,
    mark: Optional[Decimal] = None,
    ts: Optional[str | datetime] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Build a display-value dict from a set of optional trade fields.

    ONE factor f is computed from anchor_entry and reused for all price-axis
    fields so their ratios are preserved.  size and notional are sig-fig
    rounded independently (they do NOT need to tie out — the footnote covers
    honesty).

    Returns only keys whose inputs were not None, plus always "footnote".
    """
    f = price_factor(params, source_id, symbol, side, anchor_entry)

    result: dict = {}

    if entry is not None:
        result["entry"] = disp_price(params, is_hl, f, entry)
    if exit is not None:
        result["exit"] = disp_price(params, is_hl, f, exit)
    if sl is not None:
        result["sl"] = disp_price(params, is_hl, f, sl)
    if tp is not None:
        result["tp"] = disp_price(params, is_hl, f, tp)
    if liq is not None:
        result["liq"] = disp_price(params, is_hl, f, liq)
    if mark is not None:
        result["mark"] = disp_price(params, is_hl, f, mark)

    if size is not None:
        result["size"] = disp_size(params, is_hl, size)
    if notional is not None:
        result["notional"] = disp_notional(params, is_hl, notional)

    if ts is not None:
        result["ts"] = disp_time(params, is_hl, ts, now=now)

    result["footnote"] = footnote(params, is_hl)

    return result
