"""Generate a one-by-one, evidence-labelled report for 100 completed trades.

This is read-only with respect to exchanges and the tracker database.  It uses
the configured Hyperliquid sources, imports completed Lighter round trips from
a SQLite snapshot when supplied, and writes Markdown/CSV/JSON artifacts.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.sources import load_settings, load_sources
from src.stats import aggregate_round_trips
from src.trade_analysis import (
    ZERO,
    AnalyzedTrade,
    apply_candle_metrics,
    apply_funding,
    parse_hyperliquid_fill,
    prepare_analyses,
    reconstruct_hyperliquid_round_trips,
    trade_from_closed_record,
    trade_to_dict,
)


INFO_DOC = (
    "https://hyperliquid.gitbook.io/hyperliquid-docs/"
    "for-developers/api/info-endpoint"
)
PNL_DOC = (
    "https://hyperliquid.gitbook.io/hyperliquid-docs/"
    "trading/entry-price-and-pnl"
)
FUNDING_DOC = "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding"
LIGHTER_TRADES_DOC = "https://apidocs.lighter.xyz/reference/trades"
OOS_PAPER = "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2745220"
DRAWDOWN_PAPER = "https://arxiv.org/abs/1404.7493"


def _decimal_json(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def _d(value: Decimal | None, places: str = "0.01") -> str:
    if value is None:
        return "n/a"
    return f"{value.quantize(Decimal(places)):,}"


def _money(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    amount = abs(value).quantize(Decimal("0.01"))
    if value < ZERO:
        return f"-${amount:,.2f}"
    if value > ZERO:
        return f"+${amount:,.2f}"
    return "$0.00"


def _pct(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    sign = "+" if value > ZERO else ""
    return f"{sign}{value.quantize(Decimal('0.01'))}%"


def _duration(minutes: Decimal | None) -> str:
    if minutes is None:
        return "unknown"
    total = int(minutes)
    days, remainder = divmod(total, 1440)
    hours, mins = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h {mins}m"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


async def _call_with_backoff(func, *args, attempts: int = 5):
    delay = 3.0
    for attempt in range(attempts):
        try:
            return await asyncio.to_thread(func, *args)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if status != 429 and "429" not in str(exc):
                raise
            if attempt == attempts - 1:
                raise
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
    return None


async def _fetch_hl_fills(source) -> list:
    rows = await _call_with_backoff(
        source.client._info.user_fills,
        source.client.address,
    )
    if not isinstance(rows, list):
        return []
    return [
        parse_hyperliquid_fill(
            row,
            source_id=source.id,
            source_name=source.name,
        )
        for row in rows
        if isinstance(row, dict)
    ]


async def _fetch_funding(source, start: datetime, end: datetime) -> list[dict]:
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    out: list[dict] = []
    seen: set[tuple[int, str, str]] = set()
    cursor = start_ms
    for _ in range(60):
        rows = await _call_with_backoff(
            source.client._info.user_funding_history,
            source.client.address,
            cursor,
            end_ms,
        )
        if not isinstance(rows, list) or not rows:
            break
        max_time = cursor
        for row in rows:
            if not isinstance(row, dict):
                continue
            delta = row.get("delta") if isinstance(row.get("delta"), dict) else {}
            ts = int(row.get("time") or cursor)
            key = (ts, str(delta.get("coin")), str(delta.get("usdc")))
            if key in seen:
                continue
            seen.add(key)
            out.append(row)
            max_time = max(max_time, ts)
        if len(rows) < 500 or max_time <= cursor:
            break
        cursor = max_time + 1
        if cursor >= end_ms:
            break
        await asyncio.sleep(0.35)
    return out


def _load_lighter_round_trips(db_path: Path | None) -> list[AnalyzedTrade]:
    if db_path is None or not db_path.exists():
        return []
    con = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = [dict(row) for row in con.execute("select * from closed_trades")]
    con.close()
    aggregated = aggregate_round_trips(rows)
    out: list[AnalyzedTrade] = []
    for row in aggregated:
        exchange = str(row.get("exchange") or "").lower()
        source_id = str(row.get("source_id") or "")
        source_name = str(row.get("source") or "")
        is_lighter = (
            exchange == "lighter"
            or source_id.startswith("lighter")
            or source_name == "My NK pool"
        )
        if (
            is_lighter
            and str(row.get("realization_kind") or "").upper() == "FULL"
            and row.get("pnl") is not None
        ):
            out.append(trade_from_closed_record(row))
    return out


async def _add_candles(
    trades: list[AnalyzedTrade],
    source_by_id: dict[str, Any],
) -> dict[str, int]:
    stats = Counter()
    async with httpx.AsyncClient(timeout=30.0) as client:
        for idx, trade in enumerate(trades, start=1):
            if trade.exchange != "hyperliquid" or trade.opened_at is None:
                stats["skipped"] += 1
                continue
            source = source_by_id.get(trade.source_id)
            if source is None:
                stats["missing_source"] += 1
                continue
            duration = trade.closed_at - trade.opened_at
            interval = "5m" if duration <= timedelta(hours=24) else "15m"
            start = trade.opened_at - timedelta(hours=24)
            payload = {
                "type": "candleSnapshot",
                "req": {
                    "coin": trade.symbol,
                    "interval": interval,
                    "startTime": int(start.timestamp() * 1000),
                    "endTime": int(trade.closed_at.timestamp() * 1000),
                },
            }
            try:
                rows = None
                delay = 3.0
                for attempt in range(5):
                    response = await client.post(
                        f"{source.client._http_base}/info",
                        json=payload,
                    )
                    if response.status_code != 429:
                        response.raise_for_status()
                        rows = response.json()
                        break
                    if attempt == 4:
                        response.raise_for_status()
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)
                if isinstance(rows, list) and rows:
                    apply_candle_metrics(trade, rows, interval=interval)
                    stats["loaded"] += 1
                else:
                    trade.data_notes.append(
                        "No exchange candles were available for the trade window."
                    )
                    stats["empty"] += 1
            except Exception as exc:
                trade.data_notes.append(
                    f"Candle enrichment failed ({type(exc).__name__}); MAE/MFE unavailable."
                )
                stats["failed"] += 1
            if idx % 10 == 0:
                print(f"candle enrichment: {idx}/{len(trades)}")
            await asyncio.sleep(0.25)
    return dict(stats)


def _summary(trades: list[AnalyzedTrade]) -> dict[str, Any]:
    chronological = sorted(trades, key=lambda t: (t.closed_at, t.trade_id))
    wins = [t for t in trades if t.net_pnl > ZERO]
    losses = [t for t in trades if t.net_pnl < ZERO]
    breakevens = [t for t in trades if t.net_pnl == ZERO]
    gross_profit = sum((t.net_pnl for t in wins), ZERO)
    gross_loss = abs(sum((t.net_pnl for t in losses), ZERO))
    net = sum((t.net_pnl for t in trades), ZERO)
    known_fees = sum((t.fees or ZERO for t in trades), ZERO)
    known_funding = sum((t.funding or ZERO for t in trades), ZERO)
    cumulative = peak = max_drawdown = ZERO
    for trade in chronological:
        cumulative += trade.net_pnl
        peak = max(peak, cumulative)
        max_drawdown = min(max_drawdown, cumulative - peak)
    durations = [t.duration_minutes for t in trades if t.duration_minutes is not None]
    best = max(trades, key=lambda t: t.net_pnl)
    worst = min(trades, key=lambda t: t.net_pnl)
    ranked = sorted(trades, key=lambda t: t.net_pnl, reverse=True)
    top_five_pnl = sum((t.net_pnl for t in ranked[:5]), ZERO)

    def grouped(attr: str) -> dict[str, dict[str, Any]]:
        values: dict[str, list[AnalyzedTrade]] = defaultdict(list)
        for trade in trades:
            values[str(getattr(trade, attr))].append(trade)
        return {
            name: {
                "trades": len(group),
                "wins": sum(1 for trade in group if trade.net_pnl > ZERO),
                "win_rate_pct": (
                    Decimal(sum(1 for trade in group if trade.net_pnl > ZERO))
                    / Decimal(len(group))
                    * Decimal("100")
                ),
                "net_pnl": sum((trade.net_pnl for trade in group), ZERO),
                "expectancy": sum((trade.net_pnl for trade in group), ZERO)
                / Decimal(len(group)),
            }
            for name, group in sorted(values.items())
        }

    taker_heavy = [
        t
        for t in trades
        if t.taker_ratio is not None and t.taker_ratio >= Decimal("0.90")
    ]
    winner_to_loss = [
        t for t in trades if t.management_label == "winner turned loss"
    ]
    post_loss_escalation = [
        t
        for t in trades
        if t.prior_trade_was_loss
        and t.size_vs_prior_median is not None
        and t.size_vs_prior_median >= Decimal("1.5")
    ]

    def subset(values: list[AnalyzedTrade]) -> dict[str, Any]:
        pnl = sum((trade.net_pnl for trade in values), ZERO)
        return {
            "trades": len(values),
            "wins": sum(1 for trade in values if trade.net_pnl > ZERO),
            "net_pnl": pnl,
            "expectancy": pnl / Decimal(len(values)) if values else None,
        }

    return {
        "trade_count": len(trades),
        "period_start": chronological[0].closed_at.isoformat(),
        "period_end": chronological[-1].closed_at.isoformat(),
        "wins": len(wins),
        "losses": len(losses),
        "breakevens": len(breakevens),
        "win_rate_pct": Decimal(len(wins)) / Decimal(len(trades)) * Decimal("100"),
        "net_pnl": net,
        "known_fees": known_fees,
        "known_funding": known_funding,
        "expectancy": net / Decimal(len(trades)),
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "max_closed_trade_drawdown": max_drawdown,
        "median_duration_minutes": Decimal(str(median(durations))) if durations else None,
        "best_trade_id": best.trade_id,
        "best_trade_pnl": best.net_pnl,
        "net_pnl_without_best_trade": net - best.net_pnl,
        "top_five_pnl": top_five_pnl,
        "net_pnl_without_top_five": net - top_five_pnl,
        "worst_trade_id": worst.trade_id,
        "worst_trade_pnl": worst.net_pnl,
        "by_source": dict(Counter(t.source_id for t in trades)),
        "by_exchange": dict(Counter(t.exchange for t in trades)),
        "by_direction": dict(Counter(t.direction for t in trades)),
        "by_setup": dict(Counter(t.setup_label for t in trades)),
        "by_management": dict(Counter(t.management_label for t in trades)),
        "by_execution": dict(Counter(t.execution_label for t in trades)),
        "by_review": dict(Counter(t.review_label for t in trades)),
        "by_confidence": dict(Counter(t.data_confidence for t in trades)),
        "performance_by_source": grouped("source_id"),
        "performance_by_direction": grouped("direction"),
        "performance_by_setup": grouped("setup_label"),
        "performance_by_execution": grouped("execution_label"),
        "taker_heavy_subset": subset(taker_heavy),
        "winner_to_loss_subset": subset(winner_to_loss),
        "post_loss_size_escalation_subset": subset(post_loss_escalation),
    }


def _top_patterns(trades: list[AnalyzedTrade]) -> list[str]:
    patterns: list[str] = []
    counter = [t for t in trades if t.setup_label.startswith("countertrend")]
    if counter:
        pnl = sum((t.net_pnl for t in counter), ZERO)
        patterns.append(
            f"{len(counter)} countertrend entries produced {_money(pnl)} in total."
        )
    taker = [t for t in trades if t.taker_ratio is not None and t.taker_ratio >= Decimal("0.9")]
    if taker:
        patterns.append(
            f"{len(taker)} trades were at least 90% taker by turnover; compare these "
            "with limit-order alternatives before changing execution."
        )
    givebacks = [
        t
        for t in trades
        if t.management_label in {"large profit giveback", "winner turned loss"}
    ]
    if givebacks:
        patterns.append(
            f"{len(givebacks)} trades showed a large favorable-move giveback."
        )
    oversized = [
        t
        for t in trades
        if t.size_vs_prior_median is not None
        and t.size_vs_prior_median >= Decimal("2.5")
    ]
    if oversized:
        patterns.append(
            f"{len(oversized)} trades exceeded 2.5x their prior-20-trade median cost basis."
        )
    priority = [t for t in trades if t.review_label == "priority review"]
    patterns.append(
        f"{len(priority)} trades are marked priority review by the deterministic rules."
    )
    return patterns


def _render_markdown(
    trades: list[AnalyzedTrade],
    summary: dict[str, Any],
    candle_stats: dict[str, int],
) -> str:
    chronological = sorted(trades, key=lambda t: (t.closed_at, t.trade_id))

    def performance_rows(groups: dict[str, dict[str, Any]]) -> list[str]:
        return [
            f"| {name} | {values['trades']} | "
            f"{_pct(values['win_rate_pct'])} | {_money(values['net_pnl'])} | "
            f"{_money(values['expectancy'])} |"
            for name, values in groups.items()
        ]

    lines = [
        "# One-by-One Analysis of 100 Completed Trades",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Scope and evidence rules",
        "",
        f"This report reviews exactly **{len(trades)} completed round trips**, ordered "
        "oldest to newest. A round trip starts when position size moves from zero and "
        "ends when it returns to zero; scale-ins and partial exits stay inside the same "
        "trade. Hyperliquid reconstruction uses each fill's `startPosition`, not global "
        "trade-ID ordering, which is important for same-millisecond bursts and HIP-3 DEXes.",
        "",
        f"- Hyperliquid fill, candle, and historical-order limits: [official Info endpoint]({INFO_DOC}).",
        f"- Entry-price and closed-PnL mechanics: [official Hyperliquid PnL documentation]({PNL_DOC}).",
        f"- Funding is hourly and position-dependent: [official funding documentation]({FUNDING_DOC}).",
        f"- Lighter trade-history endpoint and authentication boundary: [official Lighter trades reference]({LIGHTER_TRADES_DOC}).",
        f"- Backtest metrics alone can be poor out-of-sample predictors; this report therefore "
        f"separates outcome from process evidence: [Wiecki et al.]({OOS_PAPER}).",
        f"- Drawdown is retained as a distinct path-risk measure: [Goldberg and Mahmoud]({DRAWDOWN_PAPER}).",
        "",
        "MAE/MFE are approximations from exchange candle highs/lows, not tick-by-tick path "
        "replays. They can overstate both extremes when entry and exit occur within one "
        "candle. Original thesis, stop level, intended target, account equity, and historical "
        "HL leverage are unavailable, so this report does **not** invent R-multiples, stop "
        "discipline, or risk-as-a-percent-of-equity. Lighter database imports are labelled "
        "lower confidence because their opening time, fees, funding, and maker/taker mix "
        "were not captured.",
        "",
        "## Portfolio-level findings",
        "",
        "| Metric | Result |",
        "|---|---:|",
        f"| Completed trades | {summary['trade_count']} |",
        f"| Close-date range | {summary['period_start']} → {summary['period_end']} |",
        f"| Wins / losses / breakevens | {summary['wins']} / {summary['losses']} / {summary['breakevens']} |",
        f"| Win rate | {_pct(summary['win_rate_pct'])} |",
        f"| Net PnL | {_money(summary['net_pnl'])} |",
        f"| Expectancy per trade | {_money(summary['expectancy'])} |",
        f"| Profit factor | {_d(summary['profit_factor'])} |",
        f"| Known fees | {_money(summary['known_fees'])} |",
        f"| Known funding | {_money(summary['known_funding'])} |",
        f"| Closed-trade equity max drawdown | {_money(summary['max_closed_trade_drawdown'])} |",
        f"| Median observed duration | {_duration(summary['median_duration_minutes'])} |",
        f"| Candle coverage | {candle_stats} |",
        "",
        "Source mix: " + ", ".join(f"`{k}` {v}" for k, v in summary["by_source"].items()) + ".",
        "",
        "### Performance attribution",
        "",
        "The result is concentrated: the best trade contributed "
        f"{_money(summary['best_trade_pnl'])}; without it, the 100-trade sample "
        f"would be {_money(summary['net_pnl_without_best_trade'])}. The top five "
        f"contributed {_money(summary['top_five_pnl'])}; without them, the remainder "
        f"would be {_money(summary['net_pnl_without_top_five'])}. This makes the "
        "positive total fragile and argues against treating the sample profit factor "
        "as a stable edge estimate.",
        "",
        "#### By source",
        "",
        "| Source | Trades | Win rate | Net PnL | Expectancy |",
        "|---|---:|---:|---:|---:|",
    ]
    lines.extend(performance_rows(summary["performance_by_source"]))
    lines.extend(
        [
            "",
            "#### By direction",
            "",
            "| Direction | Trades | Win rate | Net PnL | Expectancy |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    lines.extend(performance_rows(summary["performance_by_direction"]))
    lines.extend(
        [
            "",
            "#### By observed setup",
            "",
            "| Setup | Trades | Win rate | Net PnL | Expectancy |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    lines.extend(performance_rows(summary["performance_by_setup"]))
    lines.extend(
        [
            "",
            "Setup buckets are retrospective context labels, not entry signals. Their "
            "small and uneven samples—plus outcome concentration—make direct strategy "
            "changes unsafe without forward testing.",
            "",
        "### Repeated patterns",
        "",
        ]
    )
    for pattern in _top_patterns(trades):
        lines.append(f"- {pattern}")
    taker = summary["taker_heavy_subset"]
    giveback = summary["winner_to_loss_subset"]
    escalation = summary["post_loss_size_escalation_subset"]
    lines.extend(
        [
            f"- Taker-heavy subset: {taker['trades']} trades, {taker['wins']} "
            f"{'win' if taker['wins'] == 1 else 'wins'}, "
            f"{_money(taker['net_pnl'])} net, {_money(taker['expectancy'])} expectancy.",
            f"- Winner-turned-loss subset: {giveback['trades']} trades and "
            f"{_money(giveback['net_pnl'])} net. This is the clearest management issue "
            "in the candle-covered sample.",
            f"- Post-loss size-escalation subset: {escalation['trades']} trades, "
            f"{escalation['wins']} wins, {_money(escalation['net_pnl'])} net. Treat "
            "this as a review queue, not proof of causation.",
            f"- Known friction was {_money(summary['known_fees'])} in fees and "
            f"{_money(summary['known_funding'])} in funding.",
        ]
    )
    lines.extend(
        [
            "",
            "The labels below are diagnostic, not trading instructions. A profitable trade can "
            "still have weak process, and a well-executed trade can lose.",
            "",
            "## Individual trade reviews",
            "",
        ]
    )
    for idx, trade in enumerate(chronological, start=1):
        lines.extend(
            [
                f"### Trade {idx:03d} — {trade.source_name} / {trade.symbol} / {trade.direction}",
                "",
                f"**Verdict:** {trade.review_label}. "
                f"**Outcome:** {trade.outcome_label}. "
                f"**Data confidence:** {trade.data_confidence}.",
                "",
                "| Factor | Evidence |",
                "|---|---|",
                f"| Open → close | {trade.opened_at.isoformat() if trade.opened_at else 'unknown'} → {trade.closed_at.isoformat()} |",
                f"| Duration | {_duration(trade.duration_minutes)} |",
                f"| Entry / exit | {_d(trade.avg_entry, '0.000001')} / {_d(trade.avg_exit, '0.000001')} |",
                f"| Size / closed cost basis | {_d(trade.closed_qty, '0.000001')} / {_money(trade.closed_cost_basis)} |",
                f"| Gross / fees / funding / net | {_money(trade.gross_pnl)} / {_money(trade.fees)} / {_money(trade.funding)} / **{_money(trade.net_pnl)}** |",
                f"| Return on closed cost | {_pct(trade.return_on_cost_pct)} |",
                f"| Setup | {trade.setup_label}; 24h move {_pct(trade.market_return_24h_pct)}, entry at {_pct((trade.entry_location_24h * Decimal('100')) if trade.entry_location_24h is not None else None)} of range |",
                f"| Excursion | MFE {_pct(trade.mfe_pct)}, MAE {_pct(trade.mae_pct)}, capture {_pct((trade.capture_ratio * Decimal('100')) if trade.capture_ratio is not None else None)} |",
                f"| Management | {trade.management_label}; {trade.entry_action_count} entry action(s), {trade.exit_action_count} exit action(s) |",
                f"| Execution | {trade.execution_label}; taker {_pct((trade.taker_ratio * Decimal('100')) if trade.taker_ratio is not None else None)}, fees {_d(trade.fee_bps)} bps |",
                f"| Relative sizing | "
                f"{(_d(trade.size_vs_prior_median) + 'x prior-20 median') if trade.size_vs_prior_median is not None else 'n/a'} |",
                f"| Leverage | {_d(trade.leverage)} |",
                "",
                "**Observations**",
                "",
            ]
        )
        lines.extend(f"- {observation}" for observation in trade.observations)
        if trade.data_notes:
            lines.extend(["", "**Data limitations**", ""])
            lines.extend(f"- {note}" for note in trade.data_notes)
        lines.append("")
    return "\n".join(lines)


def _write_artifacts(
    output_dir: Path,
    trades: list[AnalyzedTrade],
    summary: dict[str, Any],
    candle_stats: dict[str, int],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "candle_enrichment": candle_stats,
        "trades": [trade_to_dict(t) for t in trades],
    }
    (output_dir / "trade_analysis_100.json").write_text(
        json.dumps(payload, indent=2, default=_decimal_json),
        encoding="utf-8",
    )
    (output_dir / "trade_analysis_100.md").write_text(
        _render_markdown(trades, summary, candle_stats),
        encoding="utf-8",
    )
    rows = [trade_to_dict(t) for t in trades]
    scalar_keys = [
        key
        for key, value in rows[0].items()
        if not isinstance(value, (list, dict))
    ]
    with (output_dir / "trade_analysis_100.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=scalar_keys)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in scalar_keys})


async def run(args: argparse.Namespace) -> int:
    load_dotenv(ROOT / ".env")
    settings = load_settings(args.config)
    sources = load_sources(args.config, settings=settings)
    hl_sources = [source for source in sources if source.is_hyperliquid]
    if not hl_sources:
        raise SystemExit("No Hyperliquid sources are configured.")

    all_hl: list[AnalyzedTrade] = []
    source_by_id = {source.id: source for source in hl_sources}
    try:
        for source in hl_sources:
            fills = await _fetch_hl_fills(source)
            rounds = reconstruct_hyperliquid_round_trips(fills)
            exact = [trade for trade in rounds if trade.observed_open]
            print(
                f"{source.id}: {len(fills)} fills, {len(rounds)} closed rounds, "
                f"{len(exact)} with observed openings"
            )
            all_hl.extend(exact)

        lighter = _load_lighter_round_trips(args.db)
        candidates = sorted(
            all_hl + lighter,
            key=lambda trade: (trade.closed_at, trade.trade_id),
            reverse=True,
        )
        selected = candidates[: args.limit]
        if len(selected) != args.limit:
            raise SystemExit(
                f"Only {len(selected)} complete trades were available; "
                f"{args.limit} are required."
            )

        selected_by_source: dict[str, list[AnalyzedTrade]] = defaultdict(list)
        for trade in selected:
            if trade.exchange == "hyperliquid":
                selected_by_source[trade.source_id].append(trade)
        for source_id, trades in selected_by_source.items():
            source = source_by_id[source_id]
            start = min(t.opened_at for t in trades if t.opened_at) - timedelta(hours=1)
            end = max(t.closed_at for t in trades) + timedelta(hours=1)
            funding = await _fetch_funding(source, start, end)
            apply_funding(trades, funding)
            print(f"{source_id}: allocated {len(funding)} funding records")

        candle_stats = (
            await _add_candles(selected, source_by_id)
            if not args.no_candles
            else {"skipped": len(selected)}
        )
        analyses = prepare_analyses(selected)
        summary = _summary(analyses)
        _write_artifacts(args.output_dir, analyses, summary, candle_stats)
        print(f"wrote {args.output_dir / 'trade_analysis_100.md'}")
        print(f"wrote {args.output_dir / 'trade_analysis_100.csv'}")
        print(f"wrote {args.output_dir / 'trade_analysis_100.json'}")
        return 0
    finally:
        for source in hl_sources:
            await source.client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Analyze exactly 100 completed trades one by one."
    )
    parser.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    parser.add_argument(
        "--db",
        type=Path,
        help="Read-only SQLite snapshot used to include completed Lighter trades.",
    )
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "reports" / "trade_analysis_100",
    )
    parser.add_argument("--no-candles", action="store_true")
    args = parser.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
