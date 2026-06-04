"""Regression test: the privacy jitter factor must use the SAME seed across
every render path (TG formatter, PnL card, dashboard disp_view).

The bug this locks: the formatter/card used source_id="" when computing
price_factor, while dashboard.disp_view seeded with the real source id. Same
HL position therefore showed a DIFFERENT fuzzed price in the TG alert vs the
dashboard row vs the close card. They must all derive from one seed.

Style mirrors tests/test_display_transform.py: plain pytest, stdlib only.
"""

from __future__ import annotations

from decimal import Decimal

from src.display_transform import (
    PrivacyParams,
    disp_price,
    disp_view,
    price_factor,
)


def _params() -> PrivacyParams:
    return PrivacyParams(
        enabled=True,
        secret=b"k",
        mag=0.003,
        entry_quantum_pct=0.005,
        size_sigfigs=2,
        notional_sigfigs=2,
        time_bucket="exact",
        disclose_footnote=False,
    )


def test_formatter_and_disp_view_agree_on_displayed_entry():
    """price_factor(p, source_id, ...) → disp_price must equal the entry that
    disp_view produces for the same source_id. Same seed → identical factor →
    identical displayed price."""
    p = _params()
    source_id = "hyperliquid:abc123"
    symbol = "BTC"
    side = "long"
    anchor = Decimal("50000")
    real_price = Decimal("50000")

    # Formatter path: compute the factor with the real source id, then transform.
    f = price_factor(p, source_id, symbol, side, anchor)
    formatter_entry = disp_price(p, True, f, real_price)

    # disp_view path (dashboard row / closed-trade re-derivation).
    dv = disp_view(
        p, True, source_id, symbol, side, anchor, entry=real_price
    )

    assert formatter_entry == dv["entry"]


def test_different_source_id_yields_different_displayed_price():
    """The seed actually matters: a different source_id must change the factor
    (and therefore the displayed price), proving the consistency above is not a
    no-op coincidence."""
    p = _params()
    symbol = "BTC"
    side = "long"
    anchor = Decimal("50000")
    real_price = Decimal("50000")

    f_a = price_factor(p, "hyperliquid:abc123", symbol, side, anchor)
    f_b = price_factor(p, "hyperliquid:zzz999", symbol, side, anchor)

    price_a = disp_price(p, True, f_a, real_price)
    price_b = disp_price(p, True, f_b, real_price)

    assert price_a != price_b
