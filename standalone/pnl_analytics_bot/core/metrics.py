from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Iterable, Optional

from .models import OpenPosition, RoundTrip, ZERO


def _q(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else str(value.quantize(Decimal("0.00000001")))


def _bucket(rt: RoundTrip, mode: str) -> str:
    dt = rt.closed_at
    if mode == "day":
        return dt.date().isoformat()
    if mode == "week":
        year, week, _ = dt.isocalendar()
        return f"{year}-W{week:02d}"
    if mode == "month":
        return f"{dt.year}-{dt.month:02d}"
    raise ValueError(f"unknown bucket mode: {mode}")


def filter_round_trips(
    round_trips: Iterable[RoundTrip],
    *,
    start: Optional[datetime] = None,
    cutoff_mode: str = "raw-fill",
) -> list[RoundTrip]:
    if start is None:
        return list(round_trips)
    if cutoff_mode not in {"raw-fill", "full-round-trip"}:
        raise ValueError("cutoff_mode must be 'raw-fill' or 'full-round-trip'")
    out: list[RoundTrip] = []
    for rt in round_trips:
        if cutoff_mode == "raw-fill":
            if any(r.timestamp >= start for r in rt.realizations):
                out.append(rt)
        elif rt.opened_at >= start and rt.closed_at >= start:
            out.append(rt)
    return out


def compute_analytics(
    round_trips: Iterable[RoundTrip],
    open_positions: Iterable[OpenPosition] = (),
    *,
    include_open: bool = False,
) -> dict:
    trips = sorted(round_trips, key=lambda r: r.closed_at)
    closed_count = len(trips)
    wins = sum(1 for r in trips if r.is_win)
    losses = sum(1 for r in trips if r.net_pnl < ZERO)
    breakevens = sum(1 for r in trips if r.net_pnl == ZERO)
    non_wins = closed_count - wins
    net_pnl = sum((r.net_pnl for r in trips), ZERO)
    gross_pnl = sum((r.gross_pnl for r in trips), ZERO)
    fees = sum((r.fees for r in trips), ZERO)
    funding = sum((r.funding for r in trips), ZERO)
    win_pnls = [r.net_pnl for r in trips if r.net_pnl > ZERO]
    loss_pnls = [r.net_pnl for r in trips if r.net_pnl < ZERO]
    gross_profit = sum(win_pnls, ZERO)
    gross_loss = abs(sum(loss_pnls, ZERO))

    equity_curve = []
    cumulative = ZERO
    peak = ZERO
    max_drawdown = ZERO
    win_streak = loss_streak = max_win_streak = max_loss_streak = 0
    for idx, rt in enumerate(trips, start=1):
        cumulative += rt.net_pnl
        peak = max(peak, cumulative)
        drawdown = cumulative - peak
        max_drawdown = min(max_drawdown, drawdown)
        if rt.is_win:
            win_streak += 1
            loss_streak = 0
        else:
            loss_streak += 1
            win_streak = 0
        max_win_streak = max(max_win_streak, win_streak)
        max_loss_streak = max(max_loss_streak, loss_streak)
        equity_curve.append(
            {
                "index": idx,
                "closed_at": rt.closed_at.isoformat(),
                "symbol": rt.symbol,
                "net_pnl": _q(rt.net_pnl),
                "equity": _q(cumulative),
                "drawdown": _q(drawdown),
            }
        )

    def split_by(attr: str) -> list[dict]:
        grouped: dict[str, list[RoundTrip]] = defaultdict(list)
        for rt in trips:
            grouped[str(getattr(rt, attr))].append(rt)
        return [_group_summary(k, v) for k, v in sorted(grouped.items())]

    period_splits = {
        mode: [_group_summary(k, v) for k, v in sorted(_group_period(trips, mode).items())]
        for mode in ("day", "week", "month")
    }

    best_net = max(trips, key=lambda r: r.net_pnl, default=None)
    worst_net = min(trips, key=lambda r: r.net_pnl, default=None)
    best_return = max(
        (r for r in trips if r.return_on_cost is not None),
        key=lambda r: r.return_on_cost or ZERO,
        default=None,
    )
    worst_return = min(
        (r for r in trips if r.return_on_cost is not None),
        key=lambda r: r.return_on_cost or ZERO,
        default=None,
    )

    open_list = list(open_positions)
    return {
        "closed_trades": closed_count,
        "open_positions": len(open_list),
        "wins": wins,
        "losses": losses,
        "breakevens": breakevens,
        "non_wins": non_wins,
        "win_rate": _q(Decimal(wins) / Decimal(closed_count) * Decimal("100")) if closed_count else "0E-8",
        "net_pnl": _q(net_pnl),
        "gross_pnl": _q(gross_pnl),
        "total_fees": _q(fees),
        "funding": _q(funding),
        "avg_win": _q(sum(win_pnls, ZERO) / Decimal(len(win_pnls))) if win_pnls else "0E-8",
        "avg_loss": _q(sum(loss_pnls, ZERO) / Decimal(len(loss_pnls))) if loss_pnls else "0E-8",
        "expectancy": _q(net_pnl / Decimal(closed_count)) if closed_count else "0E-8",
        "profit_factor": _q(gross_profit / gross_loss) if gross_loss else None,
        "payoff_ratio": _q((sum(win_pnls, ZERO) / Decimal(len(win_pnls))) / abs(sum(loss_pnls, ZERO) / Decimal(len(loss_pnls)))) if win_pnls and loss_pnls else None,
        "best_trade_net": _trade_ref(best_net),
        "worst_trade_net": _trade_ref(worst_net),
        "best_trade_return": _trade_ref(best_return),
        "worst_trade_return": _trade_ref(worst_return),
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
        "max_drawdown": _q(max_drawdown),
        "equity_curve": equity_curve,
        "by_source": split_by("source"),
        "by_symbol": split_by("symbol"),
        "by_direction": split_by("direction"),
        "by_period": period_splits,
        "open_position_details": [
            {
                "source": p.source,
                "account": p.account,
                "symbol": p.symbol,
                "direction": p.direction,
                "qty": _q(p.qty),
                "avg_entry": _q(p.avg_entry),
                "cost_basis": _q(p.cost_basis),
                "opened_at": p.opened_at.isoformat(),
            }
            for p in open_list
        ],
        "include_open": include_open,
    }


def _group_period(trips: list[RoundTrip], mode: str) -> dict[str, list[RoundTrip]]:
    grouped: dict[str, list[RoundTrip]] = defaultdict(list)
    for rt in trips:
        grouped[_bucket(rt, mode)].append(rt)
    return grouped


def _group_summary(name: str, trips: list[RoundTrip]) -> dict:
    net = sum((r.net_pnl for r in trips), ZERO)
    wins = sum(1 for r in trips if r.is_win)
    losses = sum(1 for r in trips if r.net_pnl < ZERO)
    breakevens = sum(1 for r in trips if r.net_pnl == ZERO)
    count = len(trips)
    return {
        "name": name,
        "closed_trades": count,
        "wins": wins,
        "losses": losses,
        "breakevens": breakevens,
        "non_wins": count - wins,
        "win_rate": _q(Decimal(wins) / Decimal(count) * Decimal("100")) if count else "0E-8",
        "net_pnl": _q(net),
    }


def _trade_ref(rt: Optional[RoundTrip]) -> Optional[dict]:
    if rt is None:
        return None
    return {
        "id": rt.id,
        "source": rt.source,
        "symbol": rt.symbol,
        "direction": rt.direction,
        "closed_at": rt.closed_at.isoformat(),
        "net_pnl": _q(rt.net_pnl),
        "return_on_cost": _q(rt.return_on_cost),
    }
