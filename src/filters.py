from decimal import Decimal

from .types import Event, EventKind


def passes_min_notional(event: Event, min_usd: Decimal) -> bool:
    # Full close always fires — position is gone, user always wants to know.
    if event.kind == EventKind.CLOSE:
        return True

    # For SIZE_CHANGE (partial close or add), the relevant size is the
    # existing position being touched, not the individual fill.
    if event.position_before is not None:
        return event.position_before.notional_usd >= min_usd

    # OPEN — no prior position, judge by the fill itself.
    return event.trade.notional_usd >= min_usd
