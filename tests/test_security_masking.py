"""Security: the HL wallet address must never be exposed.

These tests lock in two guarantees:
  1. The address is only ever logged in masked form (last 4 chars).
  2. The Source id derived from an HL address does not embed the raw
     address (it can surface in the duplicate-source log warning).

The logic is mirrored here (same pattern as TestTgDedup / TestPidLock)
so the tests stay fast and require no network / SDK construction.
"""

import hashlib

import pytest


# ---------------------------------------------------------------------------
# Address masking — mirrors HyperliquidClient.address_masked
# ---------------------------------------------------------------------------

def _mask(address: str) -> str:
    addr = address.lower()
    return f"0x…{addr[-4:]}" if len(addr) >= 4 else "0x…"


class TestAddressMasking:
    def test_shows_only_last_four(self):
        addr = "0xde95b007e425cacd9c1aeddb8415f936b4d84c74"
        masked = _mask(addr)
        assert masked == "0x…4c74"

    def test_full_address_not_in_masked(self):
        addr = "0xde95b007e425cacd9c1aeddb8415f936b4d84c74"
        masked = _mask(addr)
        # The body of the address must not appear anywhere in the masked form.
        assert addr not in masked
        assert addr[2:] not in masked          # without 0x prefix either
        assert "de95b007" not in masked         # leading bytes gone

    def test_only_four_hex_chars_revealed(self):
        addr = "0xabcdef0123456789abcdef0123456789abcdef01"
        masked = _mask(addr)
        revealed = masked.replace("0x…", "")
        assert len(revealed) == 4
        assert revealed == "ef01"

    def test_lowercased(self):
        addr = "0xDE95B007E425CACD9C1AEDDB8415F936B4D84C74"
        masked = _mask(addr)
        assert masked == "0x…4c74"   # last 4 lowercased

    def test_short_or_garbage_input_does_not_crash(self):
        assert _mask("0x") == "0x…"
        assert _mask("") == "0x…"
        assert _mask("ab") == "0x…"


# ---------------------------------------------------------------------------
# Source id derivation — mirrors sources.build_source (hyperliquid branch)
# ---------------------------------------------------------------------------

def _hl_source_id(address: str) -> str:
    addr_hash = hashlib.sha256(address.lower().encode()).hexdigest()[:12]
    return f"hyperliquid:{addr_hash}"


class TestSourceIdDoesNotLeakAddress:
    def test_id_does_not_contain_raw_address(self):
        addr = "0xde95b007e425cacd9c1aeddb8415f936b4d84c74"
        sid = _hl_source_id(addr)
        assert addr not in sid
        assert addr[2:] not in sid
        assert "de95b007" not in sid

    def test_id_is_stable_for_same_address(self):
        addr = "0xde95b007e425cacd9c1aeddb8415f936b4d84c74"
        assert _hl_source_id(addr) == _hl_source_id(addr)

    def test_id_case_insensitive(self):
        lower = "0xde95b007e425cacd9c1aeddb8415f936b4d84c74"
        upper = lower.upper()
        assert _hl_source_id(lower) == _hl_source_id(upper)

    def test_different_addresses_get_different_ids(self):
        a = "0xde95b007e425cacd9c1aeddb8415f936b4d84c74"
        b = "0x1111111111111111111111111111111111111111"
        assert _hl_source_id(a) != _hl_source_id(b)

    def test_id_prefix_and_length(self):
        addr = "0xde95b007e425cacd9c1aeddb8415f936b4d84c74"
        sid = _hl_source_id(addr)
        assert sid.startswith("hyperliquid:")
        # 12-char hex hash suffix
        suffix = sid.split(":", 1)[1]
        assert len(suffix) == 12
        assert all(c in "0123456789abcdef" for c in suffix)


# ---------------------------------------------------------------------------
# Guard against regression: the real source files must use the masked/hashed
# forms in their log lines (not the raw self.address / raw address).
# ---------------------------------------------------------------------------

class TestSourceFilesDoNotLogRawAddress:
    def test_hyperliquid_client_log_lines_use_masked(self):
        import pathlib
        src = pathlib.Path(__file__).resolve().parent.parent / "src" / "hyperliquid_client.py"
        text = src.read_text(encoding="utf-8")
        # Every log.info/.warning/.error line referencing the address must use
        # address_masked, never the bare self.address.
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("log.") or "log.info" in stripped or "log.warning" in stripped:
                if "self.address" in stripped:
                    assert "address_masked" in stripped, (
                        f"log line leaks raw address: {stripped}"
                    )

    def test_sources_hl_id_uses_hash_not_raw_address(self):
        import pathlib
        src = pathlib.Path(__file__).resolve().parent.parent / "src" / "sources.py"
        text = src.read_text(encoding="utf-8")
        # The old leaky form must be gone.
        assert 'id=f"hyperliquid:{address.lower()}"' not in text
        assert 'id=f"hyperliquid:{addr_hash}"' in text
