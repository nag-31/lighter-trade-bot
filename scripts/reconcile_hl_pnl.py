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

Look back N days (default 180):
    python scripts/reconcile_hl_pnl.py --days 30 --apply

Use an explicit calculation cutoff (recommended for repairs):
    python scripts/reconcile_hl_pnl.py --from-date 2026-07-01 --apply

Skip card regeneration:
    python scripts/reconcile_hl_pnl.py --apply --no-cards

After a successful --apply run, restart the bot to reload corrected stats:
    sudo systemctl restart lighterbot

Safety guarantees
-----------------
- DRY-RUN by default; nothing is written without --apply.
- Back up the DB before any delete: data/events.db.bak-<unix_ts>-<source_id>
- SCOPED delete: only rows WHERE source=<hl name> and ts >= the authoritative
  window cutoff are removed. Rows older than the cutoff are preserved untouched
  (no data loss outside the requested calculation window).
- Lighter rows are NEVER touched (filter strictly on source == <hl name>).
- Idempotent: re-running with --apply produces the same result.
- bootstrap_markets() is called before any fetch so ALL coins are parsed
  (prevents the multi-coin cascade bug where only the first coin was kept).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
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

from src.account_ledger import (
    account_db_path,
    append_trade,
    init_account_ledger,
    replace_realizations,
)
from src.db import (
    delete_closed_trades_by_identity_since,
    init_db,
    query_closed_trades_by_identity,
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

_DEFAULT_DAYS = 180

log = logging.getLogger("reconcile_hl_pnl")


def resolve_start_time_ms(
    *, now_ms: int, days: int, from_date: str | None = None
) -> int:
    """Resolve the explicit exchange-history calculation boundary."""
    if not from_date:
        return now_ms - days * 86_400_000
    from datetime import datetime, timezone

    try:
        cutoff = datetime.fromisoformat(from_date.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"invalid --from-date {from_date!r}; use YYYY-MM-DD or ISO-8601"
        ) from exc
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    start_time_ms = int(cutoff.astimezone(timezone.utc).timestamp() * 1000)
    if start_time_ms > now_ms:
        raise ValueError("--from-date cannot be in the future")
    return start_time_ms


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
    card_pnl_override: "Decimal | None" = None,
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
            # FULL-close cards must show the whole round-trip's total (every
            # scale-out + the close), matching the live bot's card_pnl_override
            # behaviour — a per-fill figure on the close card disagreed with
            # the dashboard tile on 58 of 74 trips (some with flipped signs).
            pnl_override=(card_pnl_override if card_pnl_override is not None else realized),
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
        (cards_dir / card_filename).write_bytes(png_bytes)
        # Store the WEB path (same shape record_realization writes) — the
        # dashboard uses card_path directly as the <img> src. Storing the
        # filesystem path here broke every rebuilt card tile (blank images).
        return f"/cards/{card_filename}"

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


def _per_coin_table(records: list[dict]) -> list[tuple[str, int, Decimal]]:
    """Return list of (coin, fill_count, net_pnl) sorted by net PnL descending."""
    from collections import defaultdict
    coin_fills: dict[str, int] = defaultdict(int)
    coin_pnl: dict[str, Decimal] = defaultdict(Decimal)
    for r in records:
        sym = r.get("market_symbol") or "?"
        coin_fills[sym] += 1
        v = r.get("pnl")
        if v is not None:
            try:
                coin_pnl[sym] += Decimal(str(v))
            except Exception:
                pass
    result = [(sym, coin_fills[sym], coin_pnl[sym]) for sym in coin_fills]
    result.sort(key=lambda x: x[2], reverse=True)
    return result


def _print_report(
    existing_rows: list[dict],
    existing_window_rows: list[dict],
    rebuilt_records: list[dict],
    hl_name: str,
    dry_run: bool,
    t0_iso: str | None,
    now_iso: str,
    rows_replaced: int,
    rows_preserved: int,
) -> None:
    """Print the full reconciliation report including a per-coin breakdown."""
    rebuilt_pnl = _net_pnl(rebuilt_records)
    existing_total_pnl = _net_pnl(existing_rows)
    existing_window_pnl = _net_pnl(existing_window_rows)
    # Compare like with like: the rebuilt records cover only the authoritative
    # fetch window, so the delta must exclude older rows that will be preserved.
    delta = rebuilt_pnl - existing_window_pnl

    mode = "DRY-RUN" if dry_run else "APPLY"
    print(f"\n{'='*64}")
    print(f"  HL PnL Reconciliation Report  [{mode}]")
    print(f"{'='*64}")
    print(f"  Source             : {hl_name}")
    print(f"  DB path            : {DB_PATH}")
    print(f"  Time window        : [{t0_iso or 'N/A'} .. {now_iso}]")
    print()

    # Per-coin table
    coin_table = _per_coin_table(rebuilt_records)
    if coin_table:
        print(f"  {'Coin':<10}  {'#Fills':>7}  {'Net PnL':>14}")
        print(f"  {'-'*10}  {'-'*7}  {'-'*14}")
        for sym, cnt, pnl in coin_table:
            print(f"  {sym:<10}  {cnt:>7}  ${pnl:>+13,.2f}")
        print(f"  {'-'*10}  {'-'*7}  {'-'*14}")
        print(f"  {'TOTAL':<10}  {len(rebuilt_records):>7}  ${rebuilt_pnl:>+13,.2f}")
    else:
        print("  (no rebuilt records)")
    print()

    print(f"  Existing HL rows (total)   : {len(existing_rows):>6}")
    # Keep CLI output cp1252-safe on the Windows host used for local repairs.
    print(f"  Existing HL rows (ts>=T0)  : {rows_replaced:>6}  -> will be replaced")
    print(f"  Existing HL rows (ts<T0)   : {rows_preserved:>6}  -> preserved untouched")
    print()
    print(f"  Existing net PnL (all rows) : ${existing_total_pnl:>+,.2f}")
    print(f"  Existing net PnL (window)   : ${existing_window_pnl:>+,.2f}")
    print(f"  Rebuilt net PnL (window)    : ${rebuilt_pnl:>+,.2f}")
    print(f"  Window delta                : ${delta:>+,.2f}  ({len(rebuilt_records) - rows_replaced:+d} rows)")
    print()
    print(f"  Resulting row count        : {rows_preserved + len(rebuilt_records):>6}")
    print()

    if dry_run:
        print("  [DRY-RUN] No changes written.  Re-run with --apply to apply.")
    else:
        print("  Changes applied.  Restart the bot to reload corrected stats:")
        print("    sudo systemctl restart lighterbot")
    print(f"{'='*64}\n")


# ---------------------------------------------------------------------------
# Main async entrypoint
# ---------------------------------------------------------------------------

async def main(
    apply: bool,
    days: int,
    no_cards: bool,
    now_ms: int,
    source_id: str | None = None,
    from_date: str | None = None,
) -> None:
    # 1. Load config + find HL source
    load_dotenv()

    settings = load_settings(_REPO_ROOT / "config.yaml")

    try:
        sources = load_sources(_REPO_ROOT / "config.yaml", settings=settings)
    except RuntimeError as exc:
        log.error("Could not load sources: %s", exc)
        sys.exit(1)

    hl_sources = [s for s in sources if s.is_hyperliquid]
    if source_id:
        hl_source = next((s for s in hl_sources if s.id == source_id), None)
    elif len(hl_sources) == 1:
        hl_source = hl_sources[0]
    elif len(hl_sources) > 1:
        print(
            "ERROR: Multiple Hyperliquid sources are configured. "
            "Pass --source-id with one of: "
            + ", ".join(source.id for source in hl_sources)
        )
        sys.exit(2)
    else:
        hl_source = None
    if hl_source is None:
        print(
            "ERROR: No Hyperliquid source found in config.yaml, or HL_ADDRESS env var is unset.\n"
            "Set HL_ADDRESS in .env and ensure config.yaml has a 'type: hyperliquid' source."
        )
        sys.exit(1)

    log.info("Using HL source: name=%r  id=%s", hl_source.name, hl_source.id)
    account_ledger_path = account_db_path(DB_PATH.parent, hl_source.id)
    if apply:
        await init_account_ledger(
            account_ledger_path,
            account_id=hl_source.id,
            exchange=hl_source.exchange,
            display_name=hl_source.name,
        )

    # 2. Build PrivacyParams
    privacy = _build_privacy(settings)

    # 3. Initialise DB
    await init_db(DB_PATH)

    # 4. Bootstrap markets BEFORE fetching fills.
    #    Without this, _coin_to_id starts empty; _market_id() adds the first
    #    parsed coin making it non-empty, then every other coin fails the
    #    old "not in _coin_to_id" perp filter → only 1 coin survives.
    log.info("Bootstrapping HL markets (required for multi-coin fill parsing)…")
    await hl_source.client.bootstrap_markets()

    # 5. Compute the bounded source window and fetch fills.  A fixed cutoff is
    # preferable to an implicit "from the beginning" rebuild: rows before it
    # remain untouched, while the exchange is still queried authoritatively for
    # every fill at/after the cutoff.  The cutoff is a calculation boundary,
    # not a request to discard older ledger evidence.
    from datetime import datetime, timezone
    if from_date:
        try:
            start_time_ms = resolve_start_time_ms(
                now_ms=now_ms, days=days, from_date=from_date
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        cutoff = datetime.fromtimestamp(start_time_ms / 1000, tz=timezone.utc)
        log.info(
            "Fetching authoritative fills from cutoff %s (start_ms=%d)",
            cutoff.isoformat(), start_time_ms,
        )
    else:
        start_time_ms = now_ms - days * 86_400_000
        log.info(
            "Fetching fills for the last %d days (start_ms=%d)", days, start_time_ms
        )

    fills = await hl_source.client.fetch_realizing_fills(
        start_time_ms=start_time_ms,
        limit=10_000_000,   # effectively uncapped — reconcile the FULL window
    )
    log.info("Fetched %d realizing fills", len(fills))

    # A burst close can land several fills in the SAME millisecond, and the
    # API's intra-ms order is arbitrary — that scattered a 6-fill MEGA close
    # into a fake 2-fill "closed" + 4-fill "in progress". startPosition (size
    # before the fill) strictly SHRINKS through a close burst, so sorting
    # same-ts fills by descending |startPosition| restores the true sequence
    # (the flattening fill comes last).
    fills.sort(key=lambda f: (
        f.timestamp,
        -abs(f.start_position) if f.start_position is not None else Decimal(0),
    ))
    if apply:
        for fill in fills:
            await append_trade(
                account_ledger_path,
                account_id=hl_source.id,
                exchange=hl_source.exchange,
                trade=fill,
                raw_payload=fill.__dict__ if hasattr(fill, "__dict__") else None,
                observed_at=None,
            )

    # 6. Reconstruct records (pure logic in hl_pnl_logic.py)
    rebuilt_records = reconstruct_all(fills)
    for record in rebuilt_records:
        record["source_id"] = hl_source.id
        record["exchange"] = "hyperliquid"
        market_key = str(record.get("market_key") or "")
        record["event_uid"] = (
            f"{hl_source.id}|{market_key.replace(':', '|', 1)}|"
            f"{record.get('native_trade_id')}"
        )
    log.info("Reconstructed %d records", len(rebuilt_records))

    # 7. Determine T0 = min timestamp across all fetched fills.
    #    Only rows with ts >= T0 are within our authoritative window.
    from datetime import datetime, timezone
    now_iso = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).isoformat()

    t0_iso: str | None = None
    if rebuilt_records:
        t0_iso = min(r["ts"] for r in rebuilt_records)

    # 8. Query existing HL rows and compute scoped counts for the report.
    include_legacy = sum(1 for source in hl_sources if source.name == hl_source.name) == 1
    existing_rows = await query_closed_trades_by_identity(
        DB_PATH,
        hl_source.id,
        hl_source.name,
        include_legacy=include_legacy,
    )
    if t0_iso is not None:
        existing_window_rows = [
            r for r in existing_rows if (r.get("ts") or "") >= t0_iso
        ]
        rows_replaced = len(existing_window_rows)
        rows_preserved = len(existing_rows) - rows_replaced
    else:
        existing_window_rows = []
        rows_replaced = 0
        rows_preserved = len(existing_rows)

    # 9. Print per-coin report (always, dry-run and apply alike)
    _print_report(
        existing_rows,
        existing_window_rows,
        rebuilt_records,
        hl_source.name,
        dry_run=not apply,
        t0_iso=t0_iso,
        now_iso=now_iso,
        rows_replaced=rows_replaced,
        rows_preserved=rows_preserved,
    )

    if not apply:
        # Close client cleanly even in dry-run
        try:
            await hl_source.client.close()
        except Exception:
            pass
        return

    # 10. Apply: backup → scoped-delete → insert rebuilt rows → (optionally) cards

    # 10a. Backup DB
    ts_stamp = int(now_ms / 1000)
    # Include the account ID and avoid collisions when two account repairs run
    # in parallel; a timestamp-only backup can otherwise be overwritten by the
    # second scoped repair.
    safe_source_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", hl_source.id)
    backup_path = DB_PATH.with_name(f"events.db.bak-{ts_stamp}-{safe_source_id}")
    suffix = 1
    while backup_path.exists():
        backup_path = DB_PATH.with_name(
            f"events.db.bak-{ts_stamp}-{safe_source_id}-{suffix}"
        )
        suffix += 1
    try:
        shutil.copy2(DB_PATH, backup_path)
        log.info("DB backed up to %s", backup_path)
        print(f"  Backup created: {backup_path}")
    except Exception as exc:
        log.error("Could not back up DB: %s", exc)
        print(f"ERROR: Could not back up DB: {exc}\nAborting — no changes made.")
        sys.exit(1)

    # 10b. SCOPED delete: remove only rows with ts >= T0 (authoritative window).
    #      Rows older than T0 are outside the fetched window and are preserved.
    #      Blanket delete is intentionally NOT used here.
    if t0_iso is not None:
        deleted = await delete_closed_trades_by_identity_since(
            DB_PATH,
            hl_source.id,
            hl_source.name,
            t0_iso,
            include_legacy=include_legacy,
        )
        log.info(
            "Scoped-deleted %d existing HL rows (source=%r, ts>=%s)",
            deleted, hl_source.name, t0_iso,
        )
        print(
            f"  Scoped-deleted {deleted} existing HL rows "
            f"(ts >= {t0_iso}; {rows_preserved} older rows preserved)."
        )
    else:
        log.info("No rebuilt records — nothing to delete or insert.")
        print("  No fills fetched — nothing to delete or insert.")
        try:
            await hl_source.client.close()
        except Exception:
            pass
        return

    # 10c. Insert rebuilt records (with optional card generation)
    # Round-trip running totals (records are oldest-first): a FULL row's card
    # shows the SUM of its trip's partials + itself, same as the live bot.
    _trip_running: dict[str, Decimal] = {}
    cards_written = 0
    cards_failed = 0
    for rec in rebuilt_records:
        _sym = rec.get("market_symbol") or "?"
        _own_pnl = Decimal(str(rec.get("pnl") or "0"))
        _trip_total = _trip_running.get(_sym, Decimal(0)) + _own_pnl
        if rec.get("realization_kind") == "FULL":
            _trip_running[_sym] = Decimal(0)
            _card_override = _trip_total if _trip_total != _own_pnl else None
        else:
            _trip_running[_sym] = _trip_total
            _card_override = None  # PARTIAL cards show their own batch, like live
        if not no_cards:
            card_path = _generate_card(
                rec,
                hl_source_name=hl_source.name,
                hl_source_id=hl_source.id,
                privacy=privacy,
                cards_dir=CARDS_DIR,
                card_pnl_override=_card_override,
            )
            if card_path:
                rec["card_path"] = card_path
                cards_written += 1
            else:
                cards_failed += 1

        await save_closed_trade(DB_PATH, rec)

    projection_cutoff = datetime.fromtimestamp(
        start_time_ms / 1000, tz=timezone.utc
    ).isoformat()
    projection_result = await replace_realizations(
        account_ledger_path,
        records=rebuilt_records,
        cutoff_utc=projection_cutoff,
        run_id=f"{hl_source.id}:{projection_cutoff}",
    )
    log.info(
        "Account projection rebuilt: deleted=%d inserted=%d cutoff=%s",
        projection_result["deleted"], projection_result["inserted"], projection_cutoff,
    )

    log.info("Inserted %d records", len(rebuilt_records))
    if not no_cards:
        log.info("Cards: %d written, %d failed", cards_written, cards_failed)
        print(
            f"  Inserted {len(rebuilt_records)} records  "
            f"({cards_written} cards written, {cards_failed} failed)."
        )
    else:
        print(
            f"  Inserted {len(rebuilt_records)} records (cards skipped via --no-cards)."
        )

    # 10d. Close HL client cleanly
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
            "Rebuild Hyperliquid closed-trade records from HL's authoritative fills.\n\n"
            "Defaults to DRY-RUN — pass --apply to write changes.\n"
            "Always backs up the DB before any mutation.\n\n"
            "SCOPED delete: only rows with ts >= T0 (earliest fetched fill timestamp)\n"
            "are replaced; older rows are preserved untouched."
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
        default=_DEFAULT_DAYS,
        metavar="N",
        help=(
            f"Fetch fills from the last N days (default: {_DEFAULT_DAYS}). "
            "Sets start_time_ms for complete history paging."
        ),
    )
    parser.add_argument(
        "--from-date",
        default=None,
        metavar="DATE",
        help=(
            "Authoritative calculation cutoff in UTC (YYYY-MM-DD or ISO-8601). "
            "Only fills at/after this date are rebuilt; older rows are preserved. "
            "Overrides --days."
        ),
    )
    parser.add_argument(
        "--no-cards",
        action="store_true",
        default=False,
        help="Skip PNG card regeneration (faster; cards remain as-is).",
    )
    parser.add_argument(
        "--source-id",
        default=None,
        help="Stable Hyperliquid source id (required when multiple HL wallets are configured).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    args = _parse_args()
    # Capture now_ms at the top of main() — never inside a pure function.
    _now_ms = int(time.time() * 1000)
    asyncio.run(
        main(
            apply=args.apply,
            days=args.days,
            no_cards=args.no_cards,
            now_ms=_now_ms,
            source_id=args.source_id,
            from_date=args.from_date,
        )
    )
