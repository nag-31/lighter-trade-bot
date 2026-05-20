from typing import Iterable

from .types import Event, EventKind, Position, Trade


class PositionTracker:
    """Holds current position per market. Classifies each incoming trade.

    Keyed by market_id (numeric) since both Lighter and Hyperliquid expose
    numeric asset ids. One tracker instance per tracked source, so market_id
    keys never collide across sources.

    Leverage is NOT tracked here — it's fetched from the position snapshot
    by the orchestrator and stamped onto the Event before formatting.
    """

    def __init__(self, source: str = "", initial: dict[int, Position] | None = None):
        self._source = source
        self._positions: dict[int, Position] = dict(initial or {})

    def snapshot(self) -> dict[int, Position]:
        return dict(self._positions)

    def seed(self, positions: dict[int, Position]) -> None:
        self._positions = dict(positions)

    def apply(self, trade: Trade) -> list[Event]:
        existing = self._positions.get(trade.market_id)

        if existing is None:
            new_pos = Position(
                market_id=trade.market_id,
                market_symbol=trade.market_symbol,
                side=trade.side,
                size=trade.size,
                avg_entry_price=trade.price,
                source=self._source,
            )
            self._positions[trade.market_id] = new_pos
            return [Event(kind=EventKind.OPEN, trade=trade, position_before=None, position_after=new_pos)]

        if trade.side == existing.side:
            total_size = existing.size + trade.size
            new_avg = (existing.avg_entry_price * existing.size + trade.price * trade.size) / total_size
            after = Position(
                market_id=existing.market_id,
                market_symbol=existing.market_symbol,
                side=existing.side,
                size=total_size,
                avg_entry_price=new_avg,
                source=self._source,
            )
            self._positions[trade.market_id] = after
            return [Event(kind=EventKind.SIZE_CHANGE, trade=trade, position_before=existing, position_after=after)]

        # opposite side
        if trade.size < existing.size:
            after = Position(
                market_id=existing.market_id,
                market_symbol=existing.market_symbol,
                side=existing.side,
                size=existing.size - trade.size,
                avg_entry_price=existing.avg_entry_price,
                source=self._source,
            )
            self._positions[trade.market_id] = after
            return [Event(kind=EventKind.SIZE_CHANGE, trade=trade, position_before=existing, position_after=after)]

        if trade.size == existing.size:
            del self._positions[trade.market_id]
            return [Event(kind=EventKind.CLOSE, trade=trade, position_before=existing, position_after=None)]

        # trade.size > existing.size — flip
        close_event = Event(kind=EventKind.CLOSE, trade=trade, position_before=existing, position_after=None)
        flip_size = trade.size - existing.size
        flipped = Position(
            market_id=trade.market_id,
            market_symbol=trade.market_symbol,
            side=trade.side,
            size=flip_size,
            avg_entry_price=trade.price,
            source=self._source,
        )
        self._positions[trade.market_id] = flipped
        open_event = Event(kind=EventKind.OPEN, trade=trade, position_before=None, position_after=flipped)
        return [close_event, open_event]

    def apply_many(self, trades: Iterable[Trade]) -> list[Event]:
        out: list[Event] = []
        for t in trades:
            out.extend(self.apply(t))
        return out
