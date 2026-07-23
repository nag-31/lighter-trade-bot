"""Check or apply the non-destructive tracker database v2 migration."""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.db import init_db

REQUIRED_V2_COLUMNS = {
    "source_id",
    "exchange",
    "market_key",
    "position_side",
    "native_trade_id",
    "event_uid",
}


def _status(path: Path) -> tuple[bool, set[str]]:
    if not path.exists():
        return False, set(REQUIRED_V2_COLUMNS)
    con = sqlite3.connect(path)
    try:
        columns = {row[1] for row in con.execute("PRAGMA table_info(closed_trades)")}
    finally:
        con.close()
    missing = REQUIRED_V2_COLUMNS - columns
    return not missing, missing


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("data/events.db"))
    parser.add_argument(
        "--apply",
        action="store_true",
        help="back up the DB and apply the idempotent v2 schema migration",
    )
    args = parser.parse_args()

    ready, missing = _status(args.db)
    if ready:
        print(f"{args.db}: schema v2 ready")
        return 0
    if not args.apply:
        print(f"{args.db}: migration required; missing: {', '.join(sorted(missing))}")
        return 2

    if args.db.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = args.db.with_name(f"{args.db.name}.bak-v1-{stamp}")
        shutil.copy2(args.db, backup)
        print(f"backup: {backup}")
    asyncio.run(init_db(args.db))
    ready, missing = _status(args.db)
    if not ready:
        print(f"migration incomplete; missing: {', '.join(sorted(missing))}")
        return 1
    print(f"{args.db}: schema v2 migration complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
