"""One-time HL PnL reconciliation script.

Rebuilds every Hyperliquid closed-trade record from HL's authoritative per-fill
``closedPnl`` data.  Recovers scale-outs (partial closes) that the bot
historically merged or dropped.

Usage (from repo root)
----------------------
Dry-run (default — NO writes):
    python scripts/reconcile_hl_pnl.py

Apply (writes to DB, regenerates cards):
    python scripts/reconcile_hl_pnl.py --apply

Only look back N days:
    python scripts/reconcile_hl_pnl.py --days 30 --apply

Skip card regeneration:
    python scripts/reconcile_hl_pnl.py --apply --no-cards

After a successful --apply run, restart the bot to reload corrected stats:
    sudo systemctl restart lighterbot

Safety guarantees
-----------------
- DRY-RUN by default; nothing is written without --apply.
- Back up the DB before any delete: data/events.db.bak-<unix_ts>
- Only rows WHERE source = <hl.name> are deleted; Lighter rows are NEVER touched.
- Idempotent: re-running with --apply produces the same result (delete+insert).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import sys
import time
from decimal import Decimal
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — allow running as  python scripts/reconcile_hl_pnl.py
# from repo root without installing the package.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv

from src.db import (
    delete_closed_trades_by_source,
    init_db,
    query_closed_trades_by_source,
    save_closed_trade,
)
from src.display_transform import PrivacyParams
from src.sources import load_settings, load_sources
from src.types import Event, EventKind, Position, Trade
from src.pnl_card import generate_pnl_card
from scripts.hl_pnl_logic import reconstruct_all

# DB path matches dashboard.py: repo_root/data/events.db
DB_PATH = _REPO_ROOT / "data" / "events.db"
CARDS_DIR = DB_PATH.parent / "cards"

log = logging.getLogger("reconcile_hl_pnl")


# ---------------------------------------------------------------------------
# Privacy params builder
# ---------------------------------------------------------------------------

def _build_privacy(settings) -> PrivacyParams:
    """Build PrivacyParams from BotSettings, mirroring the dashboard mapping."""
    secret_key = os.getenv("PRIVACY_SECRET_KEY", "").strip()
    if settings.privacy_enabled and not secret_key:
        log.warning(
            "PRIVACY_SECRET_KEY env var unset — using insecure default salt "
            "(same as bot). Cards will display consistent-but-dev-only obfuscation."
        )
        secret_key = "lighterbot-dev-privacy-salt-CHANGE-ME"
    return PrivacyParams(
        enabled=settings.privacy_enabled,
        secret=secret_key,
        mag=settings.privacy_mag,
        entry_quantum_pct=settings.privacy_entry_quantum_pct,
        size_sigfigs=settings.privacy_size_sigfigs,
        notional_sigfigs=settings.privacy_notional_sigfigs,
        time_bucket=settings.privacy_time_bucket,
        disclose_footnote=settings.privacy_disclose_footnote,
    )


# ---------------------------------------------------------------------------
# Card regeneration helper
# ---------------------------------------------------------------------------

def _make_synthetic_event(rec: dict, fill_ts, market_symbol: str, side: str,
                           entry: Decimal, exit_px: Decimal, size: Decimal,
                           realized: Decimal, source: str) -> Event:
    """Build a synthetic Event suitable for generate_pnl_card."""
    from datetime import timezone
    # Build a minimal Trade for the card generator
    synthetic_trade = Trade(
        trade_id=rec.get("trade_id") or 0,
        timestamp=fill_ts,
        market_id=0,
        market_symbol=market_symbol,
        side=side,
        size=size,
        price=exit_px,
        source=source,
        realized_pnl=realized,
    )
    pos_before = Position(
        market_id=0,
        market_symbol=market_symbol,
        side=side,
        size=size,
        avg_entry_price=entry,
        source=source,
    )
    return Event(
        kind=EventKind.CLOSE,
        trade=synthetic_trade,
        position_before=pos_before,
        position_after=None,
        leverage=None,
    )


def _generate_card(
    rec: dict,
    hl_source_name: str,
    hl_source_id: str,
    privacy: PrivacyParams,
    cards_dir: Path,
) -> str | None:
    """Generate a PnL card PNG for a single record. Returns card_path or None."""
    try:
        from datetime import datetime, timezone
        ts_raw = rec.get("ts") or ""
        try:
            fill_ts = datetime.fromisoformat(ts_raw)
        except (ValueError, TypeError):
            fill_ts = datetime.now(tz=timezone.utc)

        entry = Decimal(rec["entry"])
        exit_px = Decimal(rec["exit"])
        size = Decimal(rec["size"])
        realized = Decimal(rec["pnl"])
        market_symbol = rec["market_symbol"]
        side = rec["side"]
        is_partial = rec.get("realization_kind") == "PARTIAL"

        event = _make_synthetic_event(
            rec, fill_ts, market_symbol, side, entry, exit_px, size, realized,
            hl_source_name,
        )

        png_bytes = generate_pnl_card(
            event,
            hl_source_name,
            rec["wins"],
            rec["total"],
            pnl_override=realized,
            is_partial=is_partial,
            privacy=privacy,
            is_hl=True,
            anchor_entry=entry,
            source_id=hl_source_id,
        )
        if png_bytes is None:
            log.debug("generate_pnl_card returned None (Pillow not installed?)")
            return None

        cards_dir.mkdir(parents=True, exist_ok=True)
        trade_id = rec.get("trade_id") or int(time.time() * 1000)
        card_filename = f"hl_recon_{trade_id}.png"
        card_path = cards_dir / card_filename
        card_path.write_bytes(png_bytes)
        return str(card_path)

    except Exception as exc:
        log.warning("card generation failed for trade_id=%s: %s", rec.get("trade_id"), exc)
        return None


# ---------------------------------------------------------------------------
# Report helpers
# ---------------------------------------------------------------------------

def _net_pnl(rows: list[dict]) -> Decimal:
    total = Decimal("0")
    for r in rows:
        v = r.get("pnl")
        if v is not None:
            try:
                total += Decimal(str(v))
            except Exception:
                pass
    return total


def _print_report(
    existing_rows: list[dict],
    rebuilt_records: list[dict],
    hl_name: str,
    dry_run: bool,
) -> None:
    existing_count = len(existing_rows)
    existing_pnl = _net_pnl(existing_rows)
    rebuilt_count = len(rebuilt_records)
    rebuilt_pnl = _net_pnl(rebuilt_records)
    delta = rebuilt_pnl - existing_pnl

    mode = "DRY-RUN" if dry_run else "APPLY"
    print(f"\n{'='*60}")
    print(f"  HL PnL Reconciliation Report  [{mode}]")
    print(f"{'='*60}")
    print(f"  Source             : {hl_name}")
    print(f"  DB path            : {DB_PATH}")
    print()
    print(f"  Existing HL rows   : {existing_count:>6}")
    print(f"  Existing net PnL   : ${existing_pnl:>+,.2f}")
    print()
    print(f"  Rebuilt rows       : {rebuilt_count:>6}")
    print(f"  Rebuilt net PnL    : ${rebuilt_pnl:>+,.2f}")
    print()
    print(f"  Delta (recovered)  : ${delta:>+,.2f}  ({rebuilt_count - existing_count:+d} rows)")
    print()

    # Show up to 5 sample rebuilt records
    samples = rebuilt_records[:5]
    if samples:
        print("  Sample rebuilt trades (oldest 5):")
        for r in samples:
            ts_short = str(r.get("ts", ""))[:19]
            sym = r.get("market_symbol", "?")
            side = r.get("side", "?")
            pnl_val = r.get("pnl", "0")
            kind = r.get("realization_kind", "?")
            try:
                pnl_f = float(pnl_val)
                pnl_str = f"${pnl_f:>+,.2f}"
            except (ValueError, TypeError):
                pnl_str = str(pnl_val)
            tid = r.get("trade_id", "?")
            print(f"    [{ts_short}] {sym:>6} {side:<5} {pnl_str:>12}  {kind:<7}  tid={tid}")

    print()
    if dry_run:
        print("  [DRY-RUN] No changes written.  Re-run with --apply to apply.")
    else:
        print("  Changes applied.  Restart the bot to reload corrected stats:")
        print("    sudo systemctl restart lighterbot")
    print(f"{'='*60}\n")


# ---------------------------------------------------------------------------
# Main async entrypoint
# ---------------------------------------------------------------------------

async def main(
    apply: bool,
    days: int | None,
    no_cards: bool,
) -> None:
    # 1. Load config + find HL source
    load_dotenv()

    settings = load_settings(_REPO_ROOT / "config.yaml")

    try:
        sources = load_sources(_REPO_ROOT / "config.yaml", settings=settings)
    except RuntimeError as exc:
        log.error("Could not load sources: %s", exc)
        sys.exit(1)

    hl_source = next((s for s in sources if s.is_hyperliquid), None)
    if hl_source is None:
        print(
            "ERROR: No Hyperliquid source found in config.yaml, or HL_ADDRESS env var is unset.\n"
            "Set HL_ADDRESS in .env and ensure config.yaml has a 'type: hyperliquid' source."
        )
        sys.exit(1)

    log.info("Using HL source: name=%r  id=%s", hl_source.name, hl_source.id)

    # 2. Build PrivacyParams
    privacy = _build_privacy(settings)

    # 3. Initialise DB
    await init_db(DB_PATH)

    # 4. Fetch realizing fills from HL
    start_time_ms: int | None = None
    if days is not None:
        start_time_ms = int(time.time() * 1000) - days * 86_400_000
        log.info("Fetching fills for the last %d days (start_ms=%d)", days, start_time_ms)
    else:
        log.info("Fetching last ~2000 fills (no --days specified)")

    fills = await hl_source.client.fetch_realizing_fills(
        start_time_ms=start_time_ms,
        limit=2000,
    )
    log.info("Fetched %d realizing fills", len(fills))

    # 5. Reconstruct records (pure logic)
    rebuilt_records = reconstruct_all(fills)
    log.info("Reconstructed %d records", len(rebuilt_records))

    # 6. Query existing HL rows for the report
    existing_rows = await query_closed_trades_by_source(DB_PATH, hl_source.name)

    # 7. Print report
    _print_report(existing_rows, rebuilt_records, hl_source.name, dry_run=not apply)

    if not apply:
        return

    # 8. Apply: backup → delete → insert → (optionally) regenerate cards

    # 8a. Backup DB
    ts_stamp = int(time.time())
    backup_path = DB_PATH.with_name(f"events.db.bak-{ts_stamp}")
    try:
        shutil.copy2(DB_PATH, backup_path)
        log.info("DB backed up to %s", backup_path)
        print(f"  Backup created: {backup_path}")
    except Exception as exc:
        log.error("Could not back up DB: %s", exc)
        print(f"ERROR: Could not back up DB: {exc}\nAborting — no changes made.")
        sys.exit(1)

    # 8b. Delete existing HL rows
    deleted = await delete_closed_trades_by_source(DB_PATH, hl_source.name)
    log.info("Deleted %d existing HL rows (source=%r)", deleted, hl_source.name)
    print(f"  Deleted {deleted} existing HL rows.")

    # 8c. Insert rebuilt records (with optional card generation)
    cards_written = 0
    cards_failed = 0
    for rec in rebuilt_records:
        if not no_cards:
            card_path = _generate_card(
                rec,
                hl_source_name=hl_source.name,
                hl_source_id=hl_source.id,
                privacy=privacy,
                cards_dir=CARDS_DIR,
            )
            if card_path:
                rec["card_path"] = card_path
                cards_written += 1
            else:
                cards_failed += 1

        await save_closed_trade(DB_PATH, rec)

    log.info("Inserted %d records", len(rebuilt_records))
    if not no_cards:
        log.info("Cards: %d written, %d failed", cards_written, cards_failed)
        print(f"  Inserted {len(rebuilt_records)} records  "
              f"({cards_written} cards written, {cards_failed} failed).")
    else:
        print(f"  Inserted {len(rebuilt_records)} records (cards skipped via --no-cards).")

    # 8d. Close HL client cleanly
    try:
        await hl_source.client.close()
    except Exception:
        pass

    print("\n  Done.  Restart the bot to reload corrected stats:")
    print("    sudo systemctl restart lighterbot\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild all Hyperliquid closed-trade records from HL's authoritative fills.\n\n"
            "Defaults to DRY-RUN — pass --apply to write changes.\n"
            "Always backs up the DB before any mutation."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Write changes to the DB (default: dry-run, no writes).",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Restrict to fills from the last N days.  "
            "Without this flag, uses the last ~2000 fills (HL default)."
        ),
    )
    parser.add_argument(
        "--no-cards",
        action="store_true",
        default=False,
        help="Skip PNG card regeneration (faster; cards remain as-is).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    args = _parse_args()
    asyncio.run(main(apply=args.apply, days=args.days, no_cards=args.no_cards))
