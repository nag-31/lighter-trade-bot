"""Rebuild per-account derived realizations from the preserved shared archive.

Raw account-ledger fills are never modified.  The command only replaces the
derived ``pnl_realizations`` rows at/after the reporting cutoff.  It is dry-run
by default and does not interact with Telegram.
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.account_ledger import account_db_path, init_account_ledger, replace_realizations
from src.sources import load_settings, load_source_report


CUTOFF = "2026-06-01T00:00:00+00:00"
SHARED_DB = ROOT / "data" / "events.db"


async def main(apply: bool) -> None:
    load_dotenv(ROOT / ".env")
    settings = load_settings(ROOT / "config.yaml")
    sources = load_source_report(ROOT / "config.yaml", settings=settings).sources
    con = sqlite3.connect(SHARED_DB)
    con.row_factory = sqlite3.Row
    try:
        for source in sources:
            rows = con.execute(
                "SELECT * FROM closed_trades WHERE source_id=? OR (source_id IS NULL AND source=?) ORDER BY ts, id",
                (source.id, source.name),
            ).fetchall()
            records = [dict(row) for row in rows]
            path = account_db_path(SHARED_DB.parent, source.id)
            print(f"{source.id}: {len(records)} shared realization rows -> {path}")
            if not apply:
                continue
            await init_account_ledger(
                path, account_id=source.id, exchange=source.exchange, display_name=source.name
            )
            result = await replace_realizations(
                path, records=records, cutoff_utc=CUTOFF,
                run_id=f"archive-rebuild:{source.id}:{CUTOFF}",
            )
            print(f"  projection deleted={result['deleted']} inserted={result['inserted']}")
    finally:
        con.close()
    if not apply:
        print("DRY-RUN: no projections changed. Use --apply to rebuild derived rows.")
    else:
        print("Applied derived projection rebuild. Raw exchange fills were not changed.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Replace derived rows at/after the cutoff.")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main(args.apply))
