from decimal import Decimal

from .types import Event, EventKind


def passes_min_notional(event: Event, min_usd: Decimal) -> bool:
    # Full close always fires — position gone, user always wants to know.
    if event.kind == EventKind.CLOSE:
        return True

    # Partial reduce — judge by the position being reduced (before the fill).
    if event.kind == EventKind.REDUCE:
        if event.position_before is not None:
            return event.position_before.notional_usd >= min_usd
        return event.trade.notional_usd >= min_usd

    # SIZE_CHANGE (same-side add) — judge by existing position size.
    if event.kind == EventKind.SIZE_CHANGE and event.position_before is not None:
        return event.position_before.notional_usd >= min_usd

    # OPEN — judge by the fill itself.
    return event.trade.notional_usd >= min_usd
