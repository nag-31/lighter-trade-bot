from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from ..adapters.common import dt_from_iso
from ..cards.renderer import render_round_trip_card
from ..core.engine import PnlReconstructor
from ..core.metrics import compute_analytics, filter_round_trips
from ..core.models import RawFill
from ..notifier.telegram import HttpxTelegramTransport, RoundTripTelegramAlerter
from ..notifier.service import process_fills_and_alert_closed_round_trips
from ..storage.sqlite_store import AnalyticsStore, DEFAULT_DB
from .fixtures import acceptance_fills


def _json_default(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def load_input(path: Path) -> list[RawFill]:
    from ..adapters.hyperliquid import parse_user_fill
    from ..adapters.lighter import parse_trade

    payload = json.loads(path.read_text())
    fills = []
    for row in payload:
        adapter = str(row.get("adapter") or row.get("source") or "").lower()
        if adapter.startswith("hyper"):
            fills.append(parse_user_fill(row, source=row.get("source", "Hyperliquid"), account=row.get("account", "")))
        else:
            fills.append(parse_trade(row, source=row.get("source", "Lighter"), account=row.get("account", "")))
    return fills


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Standalone PnL analytics dry-run")
    parser.add_argument("--fixture", choices=["acceptance"], default="acceptance")
    parser.add_argument("--input-json", type=Path)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--cards-dir", type=Path, default=Path(__file__).resolve().parents[1] / "data" / "sample_cards")
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--send-telegram", action="store_true", help="Send one Telegram alert per reconstructed closed round trip")
    parser.add_argument("--telegram-max", type=int, default=1, help="Maximum Telegram alerts to send in this dry run")
    parser.add_argument("--telegram-backfill", action="store_true", help="Send unsent historical closed round trips; default marks historical closes as seen without alerting")
    parser.add_argument("--telegram-alert-after", help="Only alert closed round trips after this ISO timestamp unless --telegram-backfill is set")
    parser.add_argument("--start")
    parser.add_argument("--cutoff-mode", choices=["raw-fill", "full-round-trip"], default="raw-fill")
    parser.add_argument("--persist", action="store_true", help="Write to the standalone DB only")
    args = parser.parse_args(argv)

    fills = load_input(args.input_json) if args.input_json else acceptance_fills()
    result = PnlReconstructor().reconstruct(fills)
    start = dt_from_iso(args.start) if args.start else None
    report_round_trips = filter_round_trips(result.round_trips, start=start, cutoff_mode=args.cutoff_mode)
    analytics = compute_analytics(report_round_trips, result.open_positions)

    card_count = 0
    for rt in result.round_trips[:5]:
        safe = f"{rt.closed_at.strftime('%Y%m%d%H%M%S')}_{rt.symbol}_{rt.direction}.png"
        render_round_trip_card(rt, output_path=args.cards_dir / safe)
        card_count += 1

    if args.persist:
        store = AnalyticsStore(args.db)
        store.init()
        store.save_result(result)

    tg_sent = 0
    tg_skipped_historical = 0
    if args.send_telegram and result.round_trips:
        token = os.environ.get("PNL_TG_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("PNL_TG_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            raise SystemExit("Telegram requested but PNL_TG_BOT_TOKEN/PNL_TG_CHAT_ID are not set")
        store = AnalyticsStore(args.db)
        summary = asyncio.run(
            process_fills_and_alert_closed_round_trips(
                fills,
                store=store,
                alerter=RoundTripTelegramAlerter(HttpxTelegramTransport(bot_token=token, chat_id=chat_id)),
                max_alerts=args.telegram_max,
                send_historical=args.telegram_backfill,
                alert_after=dt_from_iso(args.telegram_alert_after) if args.telegram_alert_after else None,
            )
        )
        tg_sent = int(summary["telegram_alerts_sent"])
        tg_skipped_historical = int(summary["telegram_alerts_skipped_historical"])

    report = {
        "fills_ingested": len(result.raw_fills),
        "duplicate_fills_skipped": result.duplicates_skipped,
        "closed_round_trips_reconstructed": len(result.round_trips),
        "open_positions_remaining": len(result.open_positions),
        "net_pnl": analytics["net_pnl"],
        "fees": analytics["total_fees"],
        "funding": analytics["funding"],
        "win_rate": analytics["win_rate"],
        "mismatches_vs_exchange_reported_pnl": result.mismatches,
        "sample_cards_generated": card_count,
        "telegram_alerts_sent": tg_sent,
        "telegram_alerts_skipped_historical": tg_skipped_historical,
        "cutoff_mode": args.cutoff_mode,
    }

    print("Standalone PnL Analytics Dry Run")
    print(f"fills ingested: {report['fills_ingested']}")
    print(f"duplicate fills skipped: {report['duplicate_fills_skipped']}")
    print(f"closed round-trips reconstructed: {report['closed_round_trips_reconstructed']}")
    print(f"open positions remaining: {report['open_positions_remaining']}")
    print(f"net PnL: {report['net_pnl']}")
    print(f"fees: {report['fees']}")
    print(f"funding: {report['funding']}")
    print(f"win rate: {report['win_rate']}%")
    print(f"mismatches vs exchange-reported closed PnL: {len(result.mismatches)}")
    print(f"sample cards generated: {card_count}")
    print(f"telegram alerts sent: {tg_sent}")
    print(f"telegram historical closes marked seen: {tg_skipped_historical}")

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps({"report": report, "analytics": analytics}, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
