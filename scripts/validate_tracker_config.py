"""Validate trade-tracker configuration without exposing secret values."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv

from src.sources import load_settings, load_source_report


async def _validate(config: Path) -> int:
    settings = load_settings(config)
    report = load_source_report(config, settings=settings)
    payload = {
        "ok": report.ok,
        "active_sources": [
            {
                "id": source.id,
                "name": source.name,
                "exchange": source.exchange,
            }
            for source in report.sources
        ],
        "issues": [
            {
                "id": issue.source_id,
                "name": issue.name,
                "exchange": issue.exchange,
                "status": issue.status,
                "detail": issue.detail,
            }
            for issue in report.issues
        ],
    }
    print(json.dumps(payload, indent=2))
    for source in report.sources:
        await source.client.close()
    return 0 if report.ok else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    args = parser.parse_args()
    load_dotenv()
    return asyncio.run(_validate(args.config))


if __name__ == "__main__":
    raise SystemExit(main())
