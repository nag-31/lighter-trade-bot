"""Create one immutable exchange-fill ledger per configured account.

The command is deliberately dry-run by default.  ``--apply`` only creates or
appends files under ``data/accounts``; the legacy ``data/events.db`` is never
deleted or rewritten.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

# Allow ``python scripts/migrate_account_ledgers.py`` from the repository root.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.account_ledger import account_db_path, migrate_shared_db
from src.sources import load_settings, load_source_report


DATA_DIR = ROOT / "data"
SHARED_DB = DATA_DIR / "events.db"


async def main(apply: bool) -> None:
    load_dotenv(ROOT / ".env")
    settings = load_settings(ROOT / "config.yaml")
    report = load_source_report(ROOT / "config.yaml", settings=settings)
    sources = report.sources
    paths = {source.id: account_db_path(DATA_DIR, source.id) for source in sources}
    print("Account ledgers:")
    for source in sources:
        print(f"  {source.id}: {paths[source.id]}")
    print("Cutoff for dashboard/PnL remains 2026-06-01T00:00:00Z")
    print("Backfill alerts: disabled")
    if not apply:
        print("DRY-RUN: no account ledger files were changed. Use --apply to migrate.")
        return
    aliases = {(source.name, source.exchange): source.id for source in sources}
    aliases.update({(source.name, ""): source.id for source in sources})
    metadata = {
        source.id: {"exchange": source.exchange, "display_name": source.name}
        for source in sources
    }
    counts = await migrate_shared_db(
        SHARED_DB,
        account_paths=paths,
        aliases=aliases,
        metadata=metadata,
    )
    print("Migrated legacy event fills (idempotent):")
    for account_id, count in counts.items():
        print(f"  {account_id}: {count}")
    print("The shared events.db was preserved unchanged.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create/append per-account ledgers; default is dry-run.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main(args.apply))
