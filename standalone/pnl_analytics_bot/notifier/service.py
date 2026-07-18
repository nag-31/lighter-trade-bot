from __future__ import annotations

from datetime import datetime, timezone

from ..core.engine import PnlReconstructor
from ..core.models import RawFill
from ..storage.sqlite_store import AnalyticsStore
from .telegram import RoundTripTelegramAlerter


async def process_fills_and_alert_closed_round_trips(
    fills: list[RawFill],
    *,
    store: AnalyticsStore,
    alerter: RoundTripTelegramAlerter,
    max_alerts: int = 10,
    send_historical: bool = False,
    alert_after: datetime | None = None,
) -> dict:
    """Persist standalone analytics and alert only newly closed round trips.

    This is the shadow-bot boundary: callers may fetch fills however they want,
    then pass them here. The live bot database is never touched.
    """
    store.init()
    cutoff = alert_after or datetime.now(timezone.utc)
    marked_at = datetime.now(timezone.utc).isoformat()
    bootstrap_key = "historical_bootstrapped"
    already_bootstrapped = store.get_alert_state(bootstrap_key) == "1"
    apply_historical_cutoff = not send_historical and (alert_after is not None or not already_bootstrapped)
    result = PnlReconstructor().reconstruct(fills)
    store.save_result(result)

    sent = 0
    skipped_existing = 0
    skipped_historical = 0
    skipped_rate_limited = 0
    for rt in sorted(result.round_trips, key=lambda r: r.closed_at):
        if store.was_alert_sent(rt.id):
            skipped_existing += 1
            continue
        if apply_historical_cutoff and rt.closed_at <= cutoff:
            store.mark_alert_sent(rt.id, marked_at)
            skipped_historical += 1
            continue
        if sent >= max_alerts:
            skipped_rate_limited += 1
            continue
        if await alerter.alert_closed_round_trip(rt):
            store.mark_alert_sent(rt.id, datetime.now(timezone.utc).isoformat())
            sent += 1
    if not send_historical and not already_bootstrapped:
        store.set_alert_state(bootstrap_key, "1")
        store.set_alert_state("historical_bootstrapped_at", cutoff.isoformat())

    return {
        "fills_ingested": len(result.raw_fills),
        "duplicate_fills_skipped": result.duplicates_skipped,
        "closed_round_trips_reconstructed": len(result.round_trips),
        "open_positions_remaining": len(result.open_positions),
        "telegram_alerts_sent": sent,
        "telegram_alerts_skipped_existing": skipped_existing,
        "telegram_alerts_skipped_historical": skipped_historical,
        "telegram_alerts_skipped_rate_limited": skipped_rate_limited,
    }
