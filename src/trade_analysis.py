"""Deterministic, evidence-labelled analysis of completed perp round trips.

The live dashboard stores one row per realization, while Hyperliquid exposes
the stronger ``startPosition`` invariant on every fill.  This module uses that
invariant to rebuild complete position lifecycles, including partial exits and
flips, and keeps every estimate explicitly labelled.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any, Iterable, Optional


ZERO = Decimal("0")
ONE = Decimal("1")
EPS = Decimal("0.0000000001")


def dec(value: Any, default: Decimal = ZERO) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


def dt_ms(value: Any) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)


def _sign(value: Decimal) -> int:
    return 1 if value > ZERO else (-1 if value < ZERO else 0)


def _dex_namespace(symbol: str) -> str:
    return symbol.split(":", 1)[0].lower() if ":" in symbol else "default"


@dataclass(frozen=True)
class AnalysisFill:
    source_id: str
    source_name: str
    symbol: str
    fill_id: str
    order_id: str
    timestamp: datetime
    side: str
    qty: Decimal
    price: Decimal
    start_position: Decimal
    closed_pnl: Decimal
    fee: Decimal
    crossed: bool
    raw_direction: str = ""

    @property
    def signed_qty(self) -> Decimal:
        return self.qty if self.side == "buy" else -self.qty

    @property
    def end_position(self) -> Decimal:
        return self.start_position + self.signed_qty

    @property
    def notional(self) -> Decimal:
        return self.qty * self.price

    @property
    def dex(self) -> str:
        return _dex_namespace(self.symbol)


@dataclass
class AnalyzedTrade:
    trade_id: str
    source_id: str
    source_name: str
    exchange: str
    symbol: str
    direction: str
    opened_at: Optional[datetime]
    closed_at: datetime
    avg_entry: Decimal
    avg_exit: Decimal
    closed_qty: Decimal
    closed_cost_basis: Decimal
    gross_pnl: Decimal
    fees: Optional[Decimal]
    funding: Optional[Decimal]
    net_pnl: Decimal
    turnover: Decimal
    peak_position_qty: Decimal
    peak_notional: Decimal
    entry_fill_count: int
    exit_fill_count: int
    entry_action_count: int
    exit_action_count: int
    taker_notional: Decimal
    leverage: Optional[Decimal] = None
    observed_open: bool = True
    data_notes: list[str] = field(default_factory=list)
    fill_ids: list[str] = field(default_factory=list)
    realization_evidence: list[dict[str, str]] = field(default_factory=list)
    candle_interval: Optional[str] = None
    candle_count: int = 0
    market_return_24h_pct: Optional[Decimal] = None
    market_range_24h_pct: Optional[Decimal] = None
    entry_location_24h: Optional[Decimal] = None
    mfe_pct: Optional[Decimal] = None
    mae_pct: Optional[Decimal] = None
    capture_ratio: Optional[Decimal] = None
    duration_minutes: Optional[Decimal] = None
    size_vs_prior_median: Optional[Decimal] = None
    minutes_since_prior_close: Optional[Decimal] = None
    prior_trade_was_loss: Optional[bool] = None
    setup_label: str = "unknown"
    management_label: str = "unknown"
    execution_label: str = "unknown"
    outcome_label: str = "unknown"
    review_label: str = "review"
    observations: list[str] = field(default_factory=list)

    @property
    def return_on_cost_pct(self) -> Optional[Decimal]:
        if not self.closed_cost_basis:
            return None
        return self.net_pnl / self.closed_cost_basis * Decimal("100")

    @property
    def fee_bps(self) -> Optional[Decimal]:
        if self.fees is None or not self.turnover:
            return None
        return self.fees / self.turnover * Decimal("10000")

    @property
    def taker_ratio(self) -> Optional[Decimal]:
        if not self.turnover:
            return None
        return self.taker_notional / self.turnover

    @property
    def data_confidence(self) -> str:
        if not self.observed_open:
            return "low"
        if self.exchange == "hyperliquid" and self.fees is not None:
            return "high" if self.candle_count else "medium"
        return "medium" if self.closed_cost_basis else "low"


@dataclass
class _ActiveTrade:
    source_id: str
    source_name: str
    symbol: str
    direction: str
    opened_at: datetime
    avg_entry: Decimal
    qty: Decimal
    observed_open: bool
    gross_pnl: Decimal = ZERO
    fees: Decimal = ZERO
    turnover: Decimal = ZERO
    taker_notional: Decimal = ZERO
    closed_qty: Decimal = ZERO
    cost_basis: Decimal = ZERO
    exit_notional: Decimal = ZERO
    peak_position_qty: Decimal = ZERO
    peak_notional: Decimal = ZERO
    entry_fills: list[AnalysisFill] = field(default_factory=list)
    exit_fills: list[AnalysisFill] = field(default_factory=list)
    fill_ids: list[str] = field(default_factory=list)
    data_notes: list[str] = field(default_factory=list)


def parse_hyperliquid_fill(
    raw: dict[str, Any],
    *,
    source_id: str,
    source_name: str,
) -> AnalysisFill:
    symbol = str(raw.get("coin") or "").strip()
    side = "buy" if str(raw.get("side") or "").upper() == "B" else "sell"
    tid = str(raw.get("tid"))
    return AnalysisFill(
        source_id=source_id,
        source_name=source_name,
        symbol=symbol,
        fill_id=f"{_dex_namespace(symbol)}:{tid}",
        order_id=str(raw.get("oid") if raw.get("oid") is not None else tid),
        timestamp=dt_ms(raw.get("time")),
        side=side,
        qty=abs(dec(raw.get("sz"))),
        price=dec(raw.get("px")),
        start_position=dec(raw.get("startPosition")),
        closed_pnl=dec(raw.get("closedPnl")),
        fee=abs(dec(raw.get("fee"))),
        crossed=bool(raw.get("crossed", False)),
        raw_direction=str(raw.get("dir") or ""),
    )


def _order_timestamp_burst(fills: list[AnalysisFill]) -> list[AnalysisFill]:
    """Order same-ms fills by their exact start/end-position chain."""
    if len(fills) < 2:
        return fills
    remaining = list(fills)
    end_values = {f.end_position for f in remaining}
    roots = [f for f in remaining if f.start_position not in end_values]
    if roots:
        root = min(
            roots,
            key=lambda f: (
                0 if f.start_position == ZERO else 1,
                -abs(f.start_position)
                if abs(f.end_position) < abs(f.start_position)
                else abs(f.start_position),
                f.fill_id,
            ),
        )
    else:
        root = min(
            remaining,
            key=lambda f: (
                0 if f.start_position == ZERO else 1,
                abs(f.start_position)
                if abs(f.end_position) >= abs(f.start_position)
                else -abs(f.start_position),
                f.fill_id,
            ),
        )
    ordered = [root]
    remaining.remove(root)
    cursor = root.end_position
    while remaining:
        matches = [f for f in remaining if f.start_position == cursor]
        if matches:
            remaining_starts = {f.start_position for f in remaining}
            nxt = min(
                matches,
                key=lambda f: (
                    0 if f.end_position in remaining_starts else 1,
                    f.fill_id,
                ),
            )
        else:
            nxt = min(
                remaining,
                key=lambda f: (
                    abs(f.start_position - cursor),
                    abs(f.start_position),
                    f.fill_id,
                ),
            )
        ordered.append(nxt)
        remaining.remove(nxt)
        cursor = nxt.end_position
    return ordered


def order_hyperliquid_fills(fills: Iterable[AnalysisFill]) -> list[AnalysisFill]:
    deduped: dict[tuple[str, str], AnalysisFill] = {}
    for fill in fills:
        deduped[(fill.source_id, fill.fill_id)] = fill
    grouped: dict[tuple[str, str, datetime], list[AnalysisFill]] = defaultdict(list)
    for fill in deduped.values():
        grouped[(fill.source_id, fill.symbol, fill.timestamp)].append(fill)
    out: list[AnalysisFill] = []
    for key in sorted(grouped, key=lambda k: (k[2], k[0], k[1])):
        out.extend(_order_timestamp_burst(grouped[key]))
    return out


def _derived_entry(fill: AnalysisFill, direction: str, close_qty: Decimal) -> Decimal:
    if not close_qty:
        return fill.price
    per_unit = fill.closed_pnl / close_qty
    return fill.price - per_unit if direction == "long" else fill.price + per_unit


def _new_active(
    fill: AnalysisFill,
    *,
    direction: str,
    qty: Decimal,
    fee: Decimal,
    observed_open: bool,
    avg_entry: Optional[Decimal] = None,
) -> _ActiveTrade:
    entry = fill.price if avg_entry is None else avg_entry
    notional = qty * fill.price
    active = _ActiveTrade(
        source_id=fill.source_id,
        source_name=fill.source_name,
        symbol=fill.symbol,
        direction=direction,
        opened_at=fill.timestamp,
        avg_entry=entry,
        qty=qty,
        observed_open=observed_open,
        fees=fee,
        turnover=notional,
        taker_notional=notional if fill.crossed else ZERO,
        peak_position_qty=qty,
        peak_notional=qty * entry,
        entry_fills=[fill],
        fill_ids=[fill.fill_id],
    )
    if not observed_open:
        active.data_notes.append(
            "Opening fill was outside the fetched history; entry was derived from the first close."
        )
    return active


def _finish_active(active: _ActiveTrade, closed_at: datetime) -> AnalyzedTrade:
    avg_exit = active.exit_notional / active.closed_qty if active.closed_qty else ZERO
    first_exit = active.exit_fills[0].fill_id if active.exit_fills else "none"
    trade_id = (
        f"{active.source_id}:{active.symbol}:"
        f"{active.opened_at.isoformat()}:{first_exit}"
    )
    entry_actions = {f.order_id for f in active.entry_fills}
    exit_actions = {f.order_id for f in active.exit_fills}
    net = active.gross_pnl - active.fees
    realization_evidence = [
        {
            "fill_id": fill.fill_id,
            "order_id": fill.order_id,
            "timestamp": fill.timestamp.isoformat(),
            "start_position": str(fill.start_position),
            "end_position": str(fill.end_position),
            "qty": str(fill.qty),
            "price": str(fill.price),
            "closed_pnl": str(fill.closed_pnl),
            "fee": str(fill.fee),
        }
        for fill in active.exit_fills
    ]
    return AnalyzedTrade(
        trade_id=trade_id,
        source_id=active.source_id,
        source_name=active.source_name,
        exchange="hyperliquid",
        symbol=active.symbol,
        direction=active.direction,
        opened_at=active.opened_at,
        closed_at=closed_at,
        avg_entry=active.cost_basis / active.closed_qty
        if active.closed_qty
        else active.avg_entry,
        avg_exit=avg_exit,
        closed_qty=active.closed_qty,
        closed_cost_basis=active.cost_basis,
        gross_pnl=active.gross_pnl,
        fees=active.fees,
        funding=ZERO,
        net_pnl=net,
        turnover=active.turnover,
        peak_position_qty=active.peak_position_qty,
        peak_notional=active.peak_notional,
        entry_fill_count=len(active.entry_fills),
        exit_fill_count=len(active.exit_fills),
        entry_action_count=len(entry_actions),
        exit_action_count=len(exit_actions),
        taker_notional=active.taker_notional,
        observed_open=active.observed_open,
        data_notes=list(active.data_notes),
        fill_ids=list(active.fill_ids),
        realization_evidence=realization_evidence,
    )


def reconstruct_hyperliquid_round_trips(
    fills: Iterable[AnalysisFill],
) -> list[AnalyzedTrade]:
    active: dict[tuple[str, str], _ActiveTrade] = {}
    closed: list[AnalyzedTrade] = []

    for fill in order_hyperliquid_fills(fills):
        if fill.qty <= ZERO or fill.price <= ZERO:
            continue
        key = (fill.source_id, fill.symbol)
        start = fill.start_position
        delta = fill.signed_qty
        end = fill.end_position
        start_sign = _sign(start)
        delta_sign = _sign(delta)

        if start_sign == 0 or start_sign == delta_sign:
            direction = "long" if delta_sign > 0 else "short"
            current = active.get(key)
            if current is None or current.direction != direction:
                current = _new_active(
                    fill,
                    direction=direction,
                    qty=fill.qty,
                    fee=fill.fee,
                    observed_open=(start == ZERO),
                )
                active[key] = current
                continue
            prior_qty = abs(start)
            if abs(current.qty - prior_qty) > EPS:
                current.data_notes.append(
                    "A position-state discontinuity was detected inside the fetched window."
                )
                current.observed_open = False
                current.qty = prior_qty
            new_qty = prior_qty + fill.qty
            current.avg_entry = (
                (current.avg_entry * prior_qty) + (fill.price * fill.qty)
            ) / new_qty
            current.qty = new_qty
            current.fees += fill.fee
            current.turnover += fill.notional
            if fill.crossed:
                current.taker_notional += fill.notional
            current.peak_position_qty = max(current.peak_position_qty, new_qty)
            current.peak_notional = max(
                current.peak_notional, new_qty * current.avg_entry
            )
            current.entry_fills.append(fill)
            current.fill_ids.append(fill.fill_id)
            continue

        # Opposing fill: close some/all of the existing position, then possibly
        # use the remainder to open the opposite side.
        direction = "long" if start > ZERO else "short"
        close_qty = min(abs(start), fill.qty)
        remainder = fill.qty - close_qty
        close_fee = fill.fee * (close_qty / fill.qty)
        open_fee = fill.fee - close_fee
        current = active.get(key)
        if current is None or current.direction != direction:
            current = _new_active(
                fill,
                direction=direction,
                qty=abs(start),
                fee=ZERO,
                observed_open=False,
                avg_entry=_derived_entry(fill, direction, close_qty),
            )
            # The synthetic starter is not an actual entry action.
            current.entry_fills.clear()
            current.turnover = ZERO
            current.taker_notional = ZERO
            current.fill_ids.clear()
            active[key] = current
        elif abs(current.qty - abs(start)) > EPS:
            current.data_notes.append(
                "A position-state discontinuity was detected before an exit."
            )
            current.observed_open = False
            current.qty = abs(start)

        current.gross_pnl += fill.closed_pnl
        current.fees += close_fee
        current.turnover += fill.price * close_qty
        if fill.crossed:
            current.taker_notional += fill.price * close_qty
        current.closed_qty += close_qty
        current.cost_basis += current.avg_entry * close_qty
        current.exit_notional += fill.price * close_qty
        current.exit_fills.append(fill)
        current.fill_ids.append(fill.fill_id)
        current.qty = abs(start) - close_qty

        if current.qty <= EPS:
            closed.append(_finish_active(current, fill.timestamp))
            active.pop(key, None)

        if remainder > EPS:
            new_direction = "long" if delta > ZERO else "short"
            active[key] = _new_active(
                fill,
                direction=new_direction,
                qty=remainder,
                fee=open_fee,
                observed_open=True,
            )

    return sorted(closed, key=lambda t: (t.closed_at, t.trade_id))


def apply_funding(
    trades: Iterable[AnalyzedTrade],
    funding_rows: Iterable[dict[str, Any]],
) -> None:
    by_symbol: dict[str, list[tuple[datetime, Decimal]]] = defaultdict(list)
    for row in funding_rows:
        delta = row.get("delta") if isinstance(row, dict) else None
        if not isinstance(delta, dict) or delta.get("type") != "funding":
            continue
        symbol = str(delta.get("coin") or "")
        by_symbol[symbol].append((dt_ms(row.get("time")), dec(delta.get("usdc"))))
    for values in by_symbol.values():
        values.sort(key=lambda item: item[0])
    for trade in trades:
        if trade.opened_at is None:
            trade.funding = None
            trade.data_notes.append("Funding could not be allocated without an opening time.")
            continue
        funding = sum(
            (
                amount
                for ts, amount in by_symbol.get(trade.symbol, [])
                if trade.opened_at < ts <= trade.closed_at
            ),
            ZERO,
        )
        trade.funding = funding
        trade.net_pnl = trade.gross_pnl - (trade.fees or ZERO) + funding


def trade_from_closed_record(record: dict[str, Any]) -> AnalyzedTrade:
    closed_at = datetime.fromisoformat(str(record["ts"]).replace("Z", "+00:00"))
    source_id = str(record.get("source_id") or record.get("source") or "lighter")
    entry = dec(record.get("entry"))
    exit_price = dec(record.get("exit"))
    qty = abs(dec(record.get("size")))
    pnl = dec(record.get("pnl"))
    cost = entry * qty
    notional = dec(record.get("notional"), cost + exit_price * qty)
    return AnalyzedTrade(
        trade_id=f"{source_id}:{record.get('market_symbol')}:{closed_at.isoformat()}",
        source_id=source_id,
        source_name=str(record.get("source") or source_id),
        exchange=str(record.get("exchange") or "lighter"),
        symbol=str(record.get("market_symbol") or ""),
        direction=str(record.get("side") or "").lower(),
        opened_at=None,
        closed_at=closed_at,
        avg_entry=entry,
        avg_exit=exit_price,
        closed_qty=qty,
        closed_cost_basis=cost,
        gross_pnl=pnl,
        fees=None,
        funding=None,
        net_pnl=pnl,
        turnover=notional,
        peak_position_qty=qty,
        peak_notional=cost,
        entry_fill_count=0,
        exit_fill_count=int(record.get("n_fills") or 1),
        entry_action_count=0,
        exit_action_count=int(record.get("n_fills") or 1),
        taker_notional=ZERO,
        leverage=dec(record.get("leverage"))
        if record.get("leverage") is not None
        else None,
        observed_open=False,
        data_notes=[
            "Imported from the tracker realization database; opening time, fees, "
            "funding, and maker/taker execution were unavailable."
        ],
    )


def apply_candle_metrics(
    trade: AnalyzedTrade,
    candles: Iterable[dict[str, Any]],
    *,
    interval: str,
) -> None:
    rows = sorted(
        (row for row in candles if isinstance(row, dict)),
        key=lambda row: int(row.get("t") or 0),
    )
    trade.candle_interval = interval
    trade.candle_count = len(rows)
    if trade.opened_at is None or not rows or trade.avg_entry <= ZERO:
        return
    opened_ms = int(trade.opened_at.timestamp() * 1000)
    closed_ms = int(trade.closed_at.timestamp() * 1000)
    context_start = opened_ms - 24 * 60 * 60 * 1000
    context = [
        row
        for row in rows
        if context_start <= int(row.get("t") or 0) <= opened_ms
    ]
    during = [
        row
        for row in rows
        if opened_ms <= int(row.get("T") or row.get("t") or 0)
        and int(row.get("t") or 0) <= closed_ms
    ]
    if context:
        first_close = dec(context[0].get("c"))
        if first_close:
            trade.market_return_24h_pct = (
                trade.avg_entry / first_close - ONE
            ) * Decimal("100")
        high = max(dec(row.get("h")) for row in context)
        low = min(dec(row.get("l")) for row in context)
        if trade.avg_entry:
            trade.market_range_24h_pct = (
                (high - low) / trade.avg_entry * Decimal("100")
            )
        if high > low:
            trade.entry_location_24h = (trade.avg_entry - low) / (high - low)
    if during:
        high = max(dec(row.get("h")) for row in during)
        low = min(dec(row.get("l")) for row in during)
        if trade.direction == "long":
            trade.mfe_pct = (high / trade.avg_entry - ONE) * Decimal("100")
            trade.mae_pct = (low / trade.avg_entry - ONE) * Decimal("100")
        else:
            trade.mfe_pct = (ONE - low / trade.avg_entry) * Decimal("100")
            trade.mae_pct = (ONE - high / trade.avg_entry) * Decimal("100")
        roc = trade.return_on_cost_pct
        if roc is not None and trade.mfe_pct is not None and trade.mfe_pct > EPS:
            trade.capture_ratio = roc / trade.mfe_pct


def _q(value: Optional[Decimal], places: str = "0.01") -> str:
    if value is None:
        return "n/a"
    return str(value.quantize(Decimal(places)))


def enrich_peer_context(trades: list[AnalyzedTrade]) -> None:
    ordered = sorted(trades, key=lambda t: (t.closed_at, t.trade_id))
    prior_by_source: dict[str, list[AnalyzedTrade]] = defaultdict(list)
    prior_by_symbol: dict[tuple[str, str], AnalyzedTrade] = {}
    for trade in ordered:
        prior = prior_by_source[trade.source_id]
        comparable = [t.closed_cost_basis for t in prior[-20:] if t.closed_cost_basis > ZERO]
        if comparable and trade.closed_cost_basis > ZERO:
            base = Decimal(str(median(comparable)))
            if base:
                trade.size_vs_prior_median = trade.closed_cost_basis / base
        previous = prior_by_symbol.get((trade.source_id, trade.symbol))
        if previous:
            trade.minutes_since_prior_close = Decimal(
                str((trade.closed_at - previous.closed_at).total_seconds() / 60)
            )
            trade.prior_trade_was_loss = previous.net_pnl < ZERO
        prior.append(trade)
        prior_by_symbol[(trade.source_id, trade.symbol)] = trade


def classify_trade(trade: AnalyzedTrade) -> None:
    roc = trade.return_on_cost_pct
    trend = trade.market_return_24h_pct
    location = trade.entry_location_24h
    aligned: Optional[bool] = None
    if trend is not None and abs(trend) >= Decimal("0.5"):
        aligned = (trade.direction == "long" and trend > ZERO) or (
            trade.direction == "short" and trend < ZERO
        )
    chase = False
    if location is not None:
        chase = (trade.direction == "long" and location >= Decimal("0.85")) or (
            trade.direction == "short" and location <= Decimal("0.15")
        )
    if aligned is True and not chase:
        trade.setup_label = "trend-aligned"
    elif aligned is False and chase:
        trade.setup_label = "countertrend chase"
    elif aligned is False:
        trade.setup_label = "countertrend"
    elif chase:
        trade.setup_label = "late-range entry"
    else:
        trade.setup_label = "neutral/unclear"

    taker = trade.taker_ratio
    fee_bps = trade.fee_bps
    if taker is None:
        trade.execution_label = "not observable"
    elif taker <= Decimal("0.35") and (fee_bps is None or fee_bps <= Decimal("5")):
        trade.execution_label = "efficient"
    elif taker >= Decimal("0.90"):
        trade.execution_label = "taker-heavy"
    else:
        trade.execution_label = "mixed"

    if trade.capture_ratio is None or trade.mae_pct is None:
        trade.management_label = "partially observable"
    elif roc is not None and roc > ZERO:
        if trade.capture_ratio >= Decimal("0.60") and abs(trade.mae_pct) <= Decimal("2"):
            trade.management_label = "efficient winner"
        elif trade.capture_ratio < Decimal("0.25"):
            trade.management_label = "large profit giveback"
        else:
            trade.management_label = "moderate capture"
    elif trade.mfe_pct is not None and trade.mfe_pct > Decimal("1"):
        trade.management_label = "winner turned loss"
    elif abs(trade.mae_pct) > Decimal("3"):
        trade.management_label = "large adverse excursion"
    else:
        trade.management_label = "controlled loss"

    if trade.net_pnl > ZERO:
        trade.outcome_label = "win"
    elif trade.net_pnl < ZERO:
        trade.outcome_label = "loss"
    else:
        trade.outcome_label = "breakeven"

    weak_process = (
        trade.setup_label in {"countertrend chase", "late-range entry"}
        or trade.management_label
        in {"large profit giveback", "winner turned loss", "large adverse excursion"}
        or (
            trade.size_vs_prior_median is not None
            and trade.size_vs_prior_median >= Decimal("2.5")
        )
    )
    if trade.net_pnl > ZERO and weak_process:
        trade.review_label = "profitable, but process needs review"
    elif trade.net_pnl <= ZERO and weak_process:
        trade.review_label = "priority review"
    elif trade.net_pnl > ZERO:
        trade.review_label = "constructive"
    else:
        trade.review_label = "normal loss / verify thesis"

    observations: list[str] = []
    if aligned is True:
        observations.append(
            f"Entry aligned with the prior 24h move ({_q(trend)}%)."
        )
    elif aligned is False:
        observations.append(
            f"Entry opposed the prior 24h move ({_q(trend)}%)."
        )
    if chase:
        observations.append(
            f"Entry was at {_q((location or ZERO) * Decimal('100'))}% of the prior 24h range."
        )
    if trade.mfe_pct is not None and trade.mae_pct is not None:
        observations.append(
            f"Approximate MFE/MAE: +{_q(trade.mfe_pct)}% / {_q(trade.mae_pct)}% "
            f"from weighted entry ({trade.candle_interval} candles)."
        )
    if trade.capture_ratio is not None and trade.mfe_pct and trade.mfe_pct > ZERO:
        observations.append(
            f"Realized return captured about {_q(trade.capture_ratio * Decimal('100'))}% "
            "of the observed favorable excursion."
        )
    if trade.size_vs_prior_median is not None:
        observations.append(
            f"Closed cost basis was {_q(trade.size_vs_prior_median)}x the prior-20-trade median."
        )
    if (
        trade.prior_trade_was_loss
        and trade.size_vs_prior_median is not None
        and trade.size_vs_prior_median >= Decimal("1.5")
    ):
        observations.append(
            "Size increased materially after the previous same-symbol loss; review for loss-chasing."
        )
    if trade.taker_ratio is not None and trade.taker_ratio >= Decimal("0.9"):
        observations.append(
            f"{_q(trade.taker_ratio * Decimal('100'))}% of turnover was taker execution."
        )
    if trade.entry_action_count > 1:
        observations.append(
            f"Position used {trade.entry_action_count} entry actions; verify each add followed the original thesis."
        )
    if trade.exit_action_count > 1:
        observations.append(
            f"Position used {trade.exit_action_count} exit actions, indicating scale-out management."
        )
    if not observations:
        observations.append("Available evidence did not produce a strong process flag.")
    trade.observations = observations


def prepare_analyses(trades: list[AnalyzedTrade]) -> list[AnalyzedTrade]:
    enrich_peer_context(trades)
    for trade in trades:
        if trade.opened_at is not None:
            trade.duration_minutes = Decimal(
                str((trade.closed_at - trade.opened_at).total_seconds() / 60)
            )
        classify_trade(trade)
    return sorted(trades, key=lambda t: (t.closed_at, t.trade_id), reverse=True)


def trade_to_dict(trade: AnalyzedTrade) -> dict[str, Any]:
    payload = asdict(trade)
    payload["opened_at"] = trade.opened_at.isoformat() if trade.opened_at else None
    payload["closed_at"] = trade.closed_at.isoformat()
    payload["return_on_cost_pct"] = trade.return_on_cost_pct
    payload["fee_bps"] = trade.fee_bps
    payload["taker_ratio"] = trade.taker_ratio
    payload["data_confidence"] = trade.data_confidence
    for key, value in list(payload.items()):
        if isinstance(value, Decimal):
            payload[key] = str(value)
    return payload
