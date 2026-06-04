"""Tests for src/display_transform.py — HL display-privacy transforms.

Style mirrors tests/test_settings_max_closed.py: plain pytest classes,
no fixtures beyond what's built inline, stdlib only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.display_transform import (
    PrivacyParams,
    disp_notional,
    disp_price,
    disp_size,
    disp_time,
    disp_view,
    footnote,
    price_factor,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _params(
    *,
    enabled: bool = True,
    secret: bytes | str = b"testsecret",
    mag: float = 0.003,
    entry_quantum_pct: float = 0.005,
    size_sigfigs: int = 2,
    notional_sigfigs: int = 2,
    time_bucket: str = "exact",
    disclose_footnote: bool = True,
    footnote_text: str = "Prices/sizes approx for privacy · PnL & % exact",
) -> PrivacyParams:
    return PrivacyParams(
        enabled=enabled,
        secret=secret,
        mag=mag,
        entry_quantum_pct=entry_quantum_pct,
        size_sigfigs=size_sigfigs,
        notional_sigfigs=notional_sigfigs,
        time_bucket=time_bucket,
        disclose_footnote=disclose_footnote,
        footnote_text=footnote_text,
    )


ANCHOR = Decimal("50000")
SOURCE = "hyperliquid:abc123"
SYMBOL = "BTC"
SIDE = "long"


# ---------------------------------------------------------------------------
# TestPrivacyParams
# ---------------------------------------------------------------------------

class TestPrivacyParams:
    def test_valid_params_construct(self):
        p = _params(mag=0.003)
        assert p.enabled is True
        assert p.mag == 0.003

    def test_str_secret_encoded_to_bytes(self):
        p = _params(secret="mystring")
        assert isinstance(p.secret, bytes)
        assert p.secret == b"mystring"

    def test_bytes_secret_unchanged(self):
        p = _params(secret=b"byteskey")
        assert p.secret == b"byteskey"

    def test_mag_zero_raises_when_enabled(self):
        with pytest.raises(ValueError, match="privacy_mag"):
            _params(enabled=True, mag=0.0)

    def test_mag_ge_005_raises_when_enabled(self):
        with pytest.raises(ValueError, match="privacy_mag"):
            _params(enabled=True, mag=0.05)

    def test_mag_exactly_005_raises_when_enabled(self):
        with pytest.raises(ValueError, match="privacy_mag"):
            _params(enabled=True, mag=0.05)

    def test_mag_ge_005_does_NOT_raise_when_disabled(self):
        # No exception — disabled, so mag is not validated
        p = _params(enabled=False, mag=0.05)
        assert p.enabled is False

    def test_mag_zero_does_NOT_raise_when_disabled(self):
        p = _params(enabled=False, mag=0.0)
        assert p.mag == 0.0

    def test_footnote_text_default(self):
        p = _params()
        assert "PnL" in p.footnote_text


# ---------------------------------------------------------------------------
# TestPriceFactor
# ---------------------------------------------------------------------------

class TestPriceFactor:
    def test_factor_within_range(self):
        p = _params(mag=0.003)
        for i in range(50):
            anchor = Decimal(str(50000 + i * 17))
            f = price_factor(p, f"src:{i}", SYMBOL, SIDE, anchor)
            assert Decimal("1") - Decimal("0.003") <= f <= Decimal("1") + Decimal("0.003"), (
                f"factor {f} out of [0.997, 1.003] for seed {i}"
            )

    def test_factor_deterministic_same_inputs(self):
        p = _params()
        f1 = price_factor(p, SOURCE, SYMBOL, SIDE, ANCHOR)
        f2 = price_factor(p, SOURCE, SYMBOL, SIDE, ANCHOR)
        assert f1 == f2

    def test_different_secret_different_factor(self):
        p1 = _params(secret=b"secret-A")
        p2 = _params(secret=b"secret-B")
        f1 = price_factor(p1, SOURCE, SYMBOL, SIDE, ANCHOR)
        f2 = price_factor(p2, SOURCE, SYMBOL, SIDE, ANCHOR)
        assert f1 != f2

    def test_different_anchor_bucket_different_factor(self):
        """Two anchors in clearly different buckets should produce different f."""
        p = _params(entry_quantum_pct=0.005)
        # 50000 vs 52000 → well apart (>0.5% quantisation)
        f1 = price_factor(p, SOURCE, SYMBOL, SIDE, Decimal("50000"))
        f2 = price_factor(p, SOURCE, SYMBOL, SIDE, Decimal("52000"))
        assert f1 != f2

    def test_disabled_returns_one(self):
        p = _params(enabled=False, mag=0.003)
        f = price_factor(p, SOURCE, SYMBOL, SIDE, ANCHOR)
        assert f == Decimal("1")

    def test_anchor_zero_returns_one(self):
        p = _params()
        f = price_factor(p, SOURCE, SYMBOL, SIDE, Decimal("0"))
        assert f == Decimal("1")

    def test_anchor_negative_returns_one(self):
        p = _params()
        f = price_factor(p, SOURCE, SYMBOL, SIDE, Decimal("-100"))
        assert f == Decimal("1")

    def test_all_prices_use_same_f_for_position(self):
        """open and close calls with the same anchor → identical f (stable for position life)."""
        p = _params()
        f_open = price_factor(p, SOURCE, SYMBOL, SIDE, ANCHOR)
        f_close = price_factor(p, SOURCE, SYMBOL, SIDE, ANCHOR)
        assert f_open == f_close


# ---------------------------------------------------------------------------
# TestDispPrice
# ---------------------------------------------------------------------------

class TestDispPrice:
    def test_applies_factor(self):
        p = _params()
        f = Decimal("1.002")
        real = Decimal("50000")
        result = disp_price(p, True, f, real)
        assert result == Decimal("50100")

    def test_disabled_returns_real(self):
        p = _params(enabled=False, mag=0.003)
        real = Decimal("50000")
        result = disp_price(p, True, Decimal("1.002"), real)
        assert result == real

    def test_non_hl_returns_real(self):
        p = _params()
        real = Decimal("50000")
        result = disp_price(p, False, Decimal("1.002"), real)
        assert result == real

    def test_real_price_zero_returns_zero(self):
        p = _params()
        result = disp_price(p, True, Decimal("1.002"), Decimal("0"))
        assert result == Decimal("0")

    def test_real_price_negative_returns_real(self):
        p = _params()
        real = Decimal("-50000")
        result = disp_price(p, True, Decimal("1.002"), real)
        assert result == real


# ---------------------------------------------------------------------------
# TestDispSize
# ---------------------------------------------------------------------------

class TestDispSize:
    def test_rounds_to_sigfigs(self):
        p = _params(size_sigfigs=2)
        assert disp_size(p, True, Decimal("9732")) == Decimal("9700")

    def test_small_decimal_sigfigs(self):
        p = _params(size_sigfigs=3)
        # 0.041236 rounded to 3 sig figs → 0.0412
        result = disp_size(p, True, Decimal("0.041236"))
        assert result == Decimal("0.0412")

    def test_negative_preserved(self):
        p = _params(size_sigfigs=2)
        result = disp_size(p, True, Decimal("-9732"))
        assert result == Decimal("-9700")

    def test_zero_returns_zero(self):
        p = _params(size_sigfigs=2)
        result = disp_size(p, True, Decimal("0"))
        assert result == Decimal("0")

    def test_disabled_returns_real(self):
        p = _params(enabled=False, mag=0.003)
        real = Decimal("9732")
        assert disp_size(p, True, real) == real

    def test_non_hl_returns_real(self):
        p = _params()
        real = Decimal("9732")
        assert disp_size(p, False, real) == real

    def test_one_sigfig(self):
        p = _params(size_sigfigs=1)
        assert disp_size(p, True, Decimal("9732")) == Decimal("10000")

    def test_exact_power_of_ten_unchanged(self):
        p = _params(size_sigfigs=2)
        assert disp_size(p, True, Decimal("1000")) == Decimal("1000")


# ---------------------------------------------------------------------------
# TestDispNotional
# ---------------------------------------------------------------------------

class TestDispNotional:
    def test_rounds_to_sigfigs(self):
        p = _params(notional_sigfigs=2)
        assert disp_notional(p, True, Decimal("9732")) == Decimal("9700")

    def test_disabled_returns_real(self):
        p = _params(enabled=False, mag=0.003)
        real = Decimal("99999")
        assert disp_notional(p, True, real) == real

    def test_non_hl_returns_real(self):
        p = _params()
        real = Decimal("99999")
        assert disp_notional(p, False, real) == real

    def test_three_sigfigs(self):
        p = _params(notional_sigfigs=3)
        assert disp_notional(p, True, Decimal("123456")) == Decimal("123000")


# ---------------------------------------------------------------------------
# TestDispTime
# ---------------------------------------------------------------------------

NOW = datetime(2026, 3, 15, 12, 30, 0, tzinfo=timezone.utc)


class TestDispTimeExact:
    def test_exact_string_passthrough(self):
        p = _params(time_bucket="exact")
        ts = "2026-03-15T10:00:00+00:00"
        assert disp_time(p, True, ts) == ts

    def test_exact_datetime_as_string(self):
        p = _params(time_bucket="exact")
        dt = datetime(2026, 3, 15, 10, 0, 0, tzinfo=timezone.utc)
        result = disp_time(p, True, dt)
        assert result == str(dt)

    def test_disabled_returns_original(self):
        p = _params(enabled=False, time_bucket="hour", mag=0.003)
        ts = "2026-03-15T10:45:33+00:00"
        assert disp_time(p, True, ts) == ts

    def test_non_hl_returns_original(self):
        p = _params(time_bucket="hour")
        ts = "2026-03-15T10:45:33+00:00"
        assert disp_time(p, False, ts) == ts

    def test_unparseable_string_returned_unchanged(self):
        p = _params(time_bucket="hour")
        ts = "not-a-date"
        assert disp_time(p, True, ts) == ts


class TestDispTimeHour:
    def test_truncates_to_hour(self):
        p = _params(time_bucket="hour")
        ts = "2026-03-15T10:45:33+00:00"
        result = disp_time(p, True, ts)
        assert "10:00:00" in result
        assert "45" not in result
        assert "33" not in result

    def test_preserves_utc_offset(self):
        p = _params(time_bucket="hour")
        dt = datetime(2026, 3, 15, 10, 45, 33, tzinfo=timezone.utc)
        result = disp_time(p, True, dt)
        # tzinfo preserved
        parsed_back = datetime.fromisoformat(result)
        assert parsed_back.tzinfo is not None

    def test_already_on_hour_unchanged(self):
        p = _params(time_bucket="hour")
        ts = "2026-03-15T10:00:00+00:00"
        result = disp_time(p, True, ts)
        assert "10:00:00" in result


class TestDispTimeRelative:
    def test_just_now_under_5_minutes(self):
        p = _params(time_bucket="relative")
        ts = NOW - __import__("datetime").timedelta(minutes=3)
        assert disp_time(p, True, ts, now=NOW) == "just now"

    def test_minutes_ago(self):
        p = _params(time_bucket="relative")
        ts = NOW - __import__("datetime").timedelta(minutes=20)
        result = disp_time(p, True, ts, now=NOW)
        assert result == "~20m ago"

    def test_hours_ago(self):
        p = _params(time_bucket="relative")
        ts = NOW - __import__("datetime").timedelta(hours=3)
        result = disp_time(p, True, ts, now=NOW)
        assert result == "~3h ago"

    def test_days_ago(self):
        p = _params(time_bucket="relative")
        ts = NOW - __import__("datetime").timedelta(days=2)
        result = disp_time(p, True, ts, now=NOW)
        assert result == "~2d ago"

    def test_exactly_at_boundary_5m_is_minutes(self):
        p = _params(time_bucket="relative")
        ts = NOW - __import__("datetime").timedelta(minutes=5)
        result = disp_time(p, True, ts, now=NOW)
        assert result == "~5m ago"

    def test_no_now_falls_back_to_exact(self):
        p = _params(time_bucket="relative")
        ts = "2026-03-15T10:45:33+00:00"
        result = disp_time(p, True, ts, now=None)
        assert result == ts

    def test_future_ts_returns_original(self):
        p = _params(time_bucket="relative")
        ts = NOW + __import__("datetime").timedelta(hours=1)
        result = disp_time(p, True, ts, now=NOW)
        # future → return str(ts) unchanged
        assert result == str(ts)


# ---------------------------------------------------------------------------
# TestFootnote
# ---------------------------------------------------------------------------

class TestFootnote:
    def test_returns_footnote_text_when_enabled_hl_disclosed(self):
        p = _params(disclose_footnote=True)
        result = footnote(p, True)
        assert result == p.footnote_text

    def test_empty_when_disabled(self):
        p = _params(enabled=False, disclose_footnote=True, mag=0.003)
        assert footnote(p, True) == ""

    def test_empty_when_non_hl(self):
        p = _params(disclose_footnote=True)
        assert footnote(p, False) == ""

    def test_empty_when_disclose_false(self):
        p = _params(disclose_footnote=False)
        assert footnote(p, True) == ""

    def test_custom_footnote_text(self):
        p = _params(disclose_footnote=True, footnote_text="Custom text here")
        assert footnote(p, True) == "Custom text here"


# ---------------------------------------------------------------------------
# TestMasterSwitchOff — everything returns REAL unchanged
# ---------------------------------------------------------------------------

class TestMasterSwitchOff:
    """When params.enabled=False every function returns the raw value."""

    def setup_method(self):
        self.p = _params(enabled=False, mag=0.003)

    def test_price_factor_is_one(self):
        f = price_factor(self.p, SOURCE, SYMBOL, SIDE, ANCHOR)
        assert f == Decimal("1")

    def test_disp_price_real(self):
        real = Decimal("48500.25")
        f = Decimal("1.002")
        assert disp_price(self.p, True, f, real) == real

    def test_disp_size_real(self):
        real = Decimal("9732")
        assert disp_size(self.p, True, real) == real

    def test_disp_notional_real(self):
        real = Decimal("473000000")
        assert disp_notional(self.p, True, real) == real

    def test_disp_time_real(self):
        ts = "2026-03-15T10:45:33+00:00"
        assert disp_time(self.p, True, ts) == ts

    def test_footnote_empty(self):
        assert footnote(self.p, True) == ""

    def test_disp_view_all_real(self):
        real_entry = Decimal("50000")
        real_size = Decimal("9732")
        real_notional = Decimal("487000000")
        result = disp_view(
            self.p, True, SOURCE, SYMBOL, SIDE, ANCHOR,
            entry=real_entry,
            size=real_size,
            notional=real_notional,
        )
        assert result["entry"] == real_entry
        assert result["size"] == real_size
        assert result["notional"] == real_notional
        assert result["footnote"] == ""


# ---------------------------------------------------------------------------
# TestIsHlFalse — non-HL path also returns raw
# ---------------------------------------------------------------------------

class TestIsHlFalse:
    def setup_method(self):
        self.p = _params()  # enabled=True

    def test_disp_price_real(self):
        real = Decimal("48500.25")
        assert disp_price(self.p, False, Decimal("1.002"), real) == real

    def test_disp_size_real(self):
        real = Decimal("9732")
        assert disp_size(self.p, False, real) == real

    def test_disp_notional_real(self):
        real = Decimal("473000000")
        assert disp_notional(self.p, False, real) == real

    def test_disp_time_real(self):
        ts = "2026-03-15T10:45:33+00:00"
        assert disp_time(self.p, False, ts) == ts

    def test_footnote_empty(self):
        assert footnote(self.p, False) == ""


# ---------------------------------------------------------------------------
# TestDispView — integration
# ---------------------------------------------------------------------------

class TestDispView:
    def test_only_provided_keys_in_result(self):
        p = _params()
        result = disp_view(p, True, SOURCE, SYMBOL, SIDE, ANCHOR, entry=Decimal("50000"))
        assert "entry" in result
        assert "exit" not in result
        assert "size" not in result
        assert "footnote" in result  # always present

    def test_all_price_fields_use_same_f(self):
        """entry, sl, tp, mark, liq, exit all scaled by the same f."""
        p = _params()
        real_entry = Decimal("50000")
        real_exit = Decimal("51000")
        real_sl = Decimal("49000")
        real_tp = Decimal("53000")
        result = disp_view(
            p, True, SOURCE, SYMBOL, SIDE, ANCHOR,
            entry=real_entry,
            exit=real_exit,
            sl=real_sl,
            tp=real_tp,
        )
        # Compute expected f directly
        f = price_factor(p, SOURCE, SYMBOL, SIDE, ANCHOR)
        assert result["entry"] == real_entry * f
        assert result["exit"] == real_exit * f
        assert result["sl"] == real_sl * f
        assert result["tp"] == real_tp * f

    def test_size_and_notional_sig_fig_rounded(self):
        p = _params(size_sigfigs=2, notional_sigfigs=2)
        result = disp_view(
            p, True, SOURCE, SYMBOL, SIDE, ANCHOR,
            size=Decimal("9732"),
            notional=Decimal("486600000"),
        )
        assert result["size"] == Decimal("9700")
        assert result["notional"] == Decimal("490000000")

    def test_disabled_view_all_real(self):
        p = _params(enabled=False, mag=0.003)
        real_entry = Decimal("50000")
        real_size = Decimal("9732")
        result = disp_view(
            p, True, SOURCE, SYMBOL, SIDE, ANCHOR,
            entry=real_entry,
            size=real_size,
        )
        assert result["entry"] == real_entry
        assert result["size"] == real_size

    def test_footnote_in_view(self):
        p = _params(disclose_footnote=True)
        result = disp_view(p, True, SOURCE, SYMBOL, SIDE, ANCHOR)
        assert result["footnote"] == p.footnote_text

    def test_ts_in_view(self):
        p = _params(time_bucket="hour")
        ts = "2026-03-15T10:45:33+00:00"
        result = disp_view(p, True, SOURCE, SYMBOL, SIDE, ANCHOR, ts=ts)
        assert "ts" in result
        assert "10:00:00" in result["ts"]

    def test_non_hl_view_all_real(self):
        p = _params()
        real_entry = Decimal("50000")
        real_size = Decimal("9732")
        result = disp_view(
            p, False, SOURCE, SYMBOL, SIDE, ANCHOR,
            entry=real_entry,
            size=real_size,
        )
        assert result["entry"] == real_entry
        assert result["size"] == real_size
        assert result["footnote"] == ""


# ---------------------------------------------------------------------------
# TestSigfigRounding — edge cases
# ---------------------------------------------------------------------------

class TestSigfigRounding:
    def test_9732_to_2_sigfigs(self):
        p = _params(size_sigfigs=2)
        assert disp_size(p, True, Decimal("9732")) == Decimal("9700")

    def test_041236_to_3_sigfigs(self):
        p = _params(size_sigfigs=3)
        assert disp_size(p, True, Decimal("0.041236")) == Decimal("0.0412")

    def test_negative_9732_to_2_sigfigs(self):
        p = _params(size_sigfigs=2)
        assert disp_size(p, True, Decimal("-9732")) == Decimal("-9700")

    def test_zero_to_sigfigs(self):
        p = _params(size_sigfigs=2)
        assert disp_size(p, True, Decimal("0")) == Decimal("0")

    def test_1_to_2_sigfigs(self):
        p = _params(size_sigfigs=2)
        assert disp_size(p, True, Decimal("1")) == Decimal("1.0") or \
               disp_size(p, True, Decimal("1")) == Decimal("1")

    def test_rounding_up(self):
        # 9750 to 2 sigfigs → 9800 (rounds up)
        p = _params(size_sigfigs=2)
        assert disp_size(p, True, Decimal("9750")) == Decimal("9800")

    def test_notional_large(self):
        p = _params(notional_sigfigs=3)
        assert disp_notional(p, True, Decimal("123456789")) == Decimal("123000000")
