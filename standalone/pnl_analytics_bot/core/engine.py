from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from .models import OpenPosition, PositionState, RawFill, Realization, RoundTrip, ZERO


@dataclass
class ReconstructionResult:
    raw_fills: list[RawFill]
    round_trips: list[RoundTrip]
    open_positions: list[OpenPosition]
    duplicates_skipped: int = 0
    mismatches: list[dict] = field(default_factory=list)


@dataclass
class _ActiveRound:
    position: PositionState
    realizations: list[Realization] = field(default_factory=list)


def _sort_key(fill: RawFill) -> tuple[datetime, int, str]:
    seq = fill.sequence if fill.sequence is not None else _safe_int(fill.fill_id)
    return fill.timestamp, seq, fill.fill_id


def _safe_int(value: str) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _direction_from_signed(qty: Decimal) -> str:
    return "long" if qty > 0 else "short"


def _same_direction(position: PositionState, signed_qty: Decimal) -> bool:
    return (position.direction == "long" and signed_qty > 0) or (
        position.direction == "short" and signed_qty < 0
    )


class PnlReconstructor:
    """Deterministically reconstruct round trips from immutable raw fills."""

    def reconstruct(self, fills: list[RawFill]) -> ReconstructionResult:
        seen: set[tuple[str, str, str]] = set()
        ordered: list[RawFill] = []
        duplicates = 0
        for fill in sorted(fills, key=_sort_key):
            key = (fill.source, fill.account, fill.fill_id)
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            if fill.qty <= ZERO:
                continue
            ordered.append(fill)

        active: dict[tuple[str, str, str], _ActiveRound] = {}
        closed: list[RoundTrip] = []
        mismatches: list[dict] = []

        for fill in ordered:
            key = (fill.source, fill.account, fill.symbol)
            signed_qty = fill.signed_qty
            current = active.get(key)

            if current is None:
                active[key] = _ActiveRound(self._open_position(fill, fill.qty, fill.fee))
                continue

            pos = current.position
            if _same_direction(pos, signed_qty):
                self._increase_position(pos, fill)
                continue

            close_qty = min(pos.qty, fill.qty)
            close_fraction = close_qty / fill.qty
            close_fee = fill.fee * close_fraction
            open_remainder_fee = fill.fee - close_fee
            allocated_open_fee = pos.open_fees_unallocated * (close_qty / pos.qty)
            pos.open_fees_unallocated -= allocated_open_fee

            gross_pnl = self._gross_pnl(pos.direction, pos.avg_entry, fill.price, close_qty)
            pnl_basis = fill.exchange_realized_pnl if fill.exchange_realized_pnl is not None else gross_pnl
            funding_status = "complete"
            if fill.source.lower() == "lighter" and fill.funding is None:
                funding_status = "unknown"
            funding = fill.funding if fill.funding is not None else ZERO
            net_pnl = pnl_basis + funding - close_fee - allocated_open_fee

            realization = Realization(
                source=fill.source,
                account=fill.account,
                symbol=fill.symbol,
                fill_id=fill.fill_id,
                timestamp=fill.timestamp,
                direction=pos.direction,
                closed_qty=close_qty,
                entry_price=pos.avg_entry,
                exit_price=fill.price,
                gross_pnl=gross_pnl,
                exchange_realized_pnl=fill.exchange_realized_pnl,
                allocated_open_fee=allocated_open_fee,
                close_fee=close_fee,
                funding=funding,
                funding_status=funding_status,
                net_pnl=net_pnl,
            )
            current.realizations.append(realization)
            if fill.exchange_realized_pnl is not None and abs(fill.exchange_realized_pnl - gross_pnl) > Decimal("0.00000001"):
                mismatches.append(
                    {
                        "fill_id": fill.fill_id,
                        "source": fill.source,
                        "symbol": fill.symbol,
                        "gross_pnl": str(gross_pnl),
                        "exchange_realized_pnl": str(fill.exchange_realized_pnl),
                    }
                )

            pos.qty -= close_qty
            if pos.qty == ZERO:
                closed.append(self._close_round_trip(current))
                del active[key]

                remainder = fill.qty - close_qty
                if remainder > ZERO:
                    active[key] = _ActiveRound(self._open_position(fill, remainder, open_remainder_fee))

        open_positions = [
            OpenPosition(
                source=a.position.source,
                account=a.position.account,
                symbol=a.position.symbol,
                direction=a.position.direction,
                qty=a.position.qty,
                avg_entry=a.position.avg_entry,
                opened_at=a.position.opened_at,
                open_fees_unallocated=a.position.open_fees_unallocated,
                fill_ids=tuple(a.position.fill_ids),
            )
            for a in active.values()
        ]
        return ReconstructionResult(
            raw_fills=ordered,
            round_trips=closed,
            open_positions=sorted(open_positions, key=lambda p: (p.source, p.account, p.symbol)),
            duplicates_skipped=duplicates,
            mismatches=mismatches,
        )

    @staticmethod
    def _open_position(fill: RawFill, qty: Decimal, fee: Decimal) -> PositionState:
        return PositionState(
            source=fill.source,
            account=fill.account,
            symbol=fill.symbol,
            direction=_direction_from_signed(fill.signed_qty),
            qty=qty,
            avg_entry=fill.price,
            open_fees_unallocated=fee,
            opened_at=fill.timestamp,
            fill_ids=[fill.fill_id],
        )

    @staticmethod
    def _increase_position(pos: PositionState, fill: RawFill) -> None:
        new_qty = pos.qty + fill.qty
        pos.avg_entry = ((pos.avg_entry * pos.qty) + (fill.price * fill.qty)) / new_qty
        pos.qty = new_qty
        pos.open_fees_unallocated += fill.fee
        pos.fill_ids.append(fill.fill_id)

    @staticmethod
    def _gross_pnl(direction: str, entry: Decimal, exit_price: Decimal, qty: Decimal) -> Decimal:
        if direction == "long":
            return (exit_price - entry) * qty
        return (entry - exit_price) * qty

    @staticmethod
    def _close_round_trip(active: _ActiveRound) -> RoundTrip:
        pos = active.position
        exits = tuple(r.fill_id for r in active.realizations)
        first_exit = exits[0] if exits else "none"
        return RoundTrip(
            id=f"{pos.source}:{pos.account}:{pos.symbol}:{pos.opened_at.isoformat()}:{first_exit}",
            source=pos.source,
            account=pos.account,
            symbol=pos.symbol,
            direction=pos.direction,
            opened_at=pos.opened_at,
            closed_at=active.realizations[-1].timestamp,
            entry_fill_ids=tuple(pos.fill_ids),
            exit_fill_ids=exits,
            realizations=tuple(active.realizations),
        )

