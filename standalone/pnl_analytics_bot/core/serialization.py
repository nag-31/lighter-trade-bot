from __future__ import annotations

from decimal import Decimal

from .engine import ReconstructionResult
from .metrics import compute_analytics
from .models import OpenPosition, Realization, RoundTrip


def q(value: Decimal | None) -> str | None:
    return None if value is None else str(value.quantize(Decimal("0.00000001")))


def realization_to_dict(r: Realization) -> dict:
    return {
        "fill_id": r.fill_id,
        "timestamp": r.timestamp.isoformat(),
        "direction": r.direction,
        "closed_qty": q(r.closed_qty),
        "entry_price": q(r.entry_price),
        "exit_price": q(r.exit_price),
        "cost_basis": q(r.cost_basis),
        "gross_pnl": q(r.gross_pnl),
        "exchange_realized_pnl": q(r.exchange_realized_pnl),
        "allocated_open_fee": q(r.allocated_open_fee),
        "close_fee": q(r.close_fee),
        "fees": q(r.fees),
        "funding": q(r.funding),
        "funding_status": r.funding_status,
        "net_pnl": q(r.net_pnl),
    }


def round_trip_to_dict(rt: RoundTrip) -> dict:
    return {
        "id": rt.id,
        "source": rt.source,
        "account": rt.account,
        "symbol": rt.symbol,
        "direction": rt.direction,
        "opened_at": rt.opened_at.isoformat(),
        "closed_at": rt.closed_at.isoformat(),
        "entry_fill_ids": list(rt.entry_fill_ids),
        "exit_fill_ids": list(rt.exit_fill_ids),
        "closed_qty": q(rt.closed_qty),
        "avg_entry": q(rt.avg_entry),
        "avg_exit": q(rt.avg_exit),
        "cost_basis": q(rt.cost_basis),
        "gross_pnl": q(rt.gross_pnl),
        "net_pnl": q(rt.net_pnl),
        "fees": q(rt.fees),
        "funding": q(rt.funding),
        "funding_status": rt.funding_status,
        "return_on_cost": q(rt.return_on_cost),
        "is_win": rt.is_win,
        "n_realizations": len(rt.realizations),
        "realizations": [realization_to_dict(r) for r in rt.realizations],
    }


def open_position_to_dict(p: OpenPosition) -> dict:
    return {
        "source": p.source,
        "account": p.account,
        "symbol": p.symbol,
        "direction": p.direction,
        "qty": q(p.qty),
        "avg_entry": q(p.avg_entry),
        "cost_basis": q(p.cost_basis),
        "opened_at": p.opened_at.isoformat(),
        "open_fees_unallocated": q(p.open_fees_unallocated),
        "fill_ids": list(p.fill_ids),
    }


def time_series_payload(round_trips: list[RoundTrip]) -> dict:
    ordered = sorted(round_trips, key=lambda r: r.closed_at)
    analytics = compute_analytics(ordered)
    pnl_bars = [
        {
            "index": idx,
            "closed_at": rt.closed_at.isoformat(),
            "label": f"{rt.symbol} {rt.direction}",
            "symbol": rt.symbol,
            "direction": rt.direction,
            "net_pnl": q(rt.net_pnl),
            "return_on_cost": q(rt.return_on_cost),
            "is_win": rt.is_win,
        }
        for idx, rt in enumerate(ordered, start=1)
    ]
    return {
        "equity_curve": analytics["equity_curve"],
        "pnl_bars": pnl_bars,
        "drawdown": [
            {
                "index": p["index"],
                "closed_at": p["closed_at"],
                "drawdown": p["drawdown"],
            }
            for p in analytics["equity_curve"]
        ],
        "by_day": analytics["by_period"]["day"],
        "by_week": analytics["by_period"]["week"],
        "by_month": analytics["by_period"]["month"],
    }


def result_payload(result: ReconstructionResult, *, scenario_checks: list[dict] | None = None) -> dict:
    round_trips = sorted(result.round_trips, key=lambda r: r.closed_at)
    open_positions = sorted(result.open_positions, key=lambda p: (p.source, p.account, p.symbol))
    return {
        "summary": {
            "fills_ingested": len(result.raw_fills),
            "duplicate_fills_skipped": result.duplicates_skipped,
            "closed_round_trips_reconstructed": len(round_trips),
            "open_positions_remaining": len(open_positions),
            "mismatches_vs_exchange_reported_pnl": len(result.mismatches),
        },
        "analytics": compute_analytics(round_trips, open_positions),
        "round_trips": [round_trip_to_dict(rt) for rt in round_trips],
        "open_positions": [open_position_to_dict(p) for p in open_positions],
        "time_series": time_series_payload(round_trips),
        "mismatches": result.mismatches,
        "scenario_checks": scenario_checks or [],
    }

