from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from ..core.models import RawFill


def ts(minute: int) -> datetime:
    return datetime(2026, 6, 1, 0, minute, tzinfo=timezone.utc)


def fill(
    fill_id: str,
    minute: int,
    side: str,
    qty: str,
    price: str,
    *,
    source: str = "Lighter",
    account: str = "fixture",
    symbol: str = "BTC",
    fee: str = "0",
    exchange_realized_pnl: str | None = None,
    funding: str | None = "0",
) -> RawFill:
    return RawFill(
        source=source,
        account=account,
        symbol=symbol,
        fill_id=fill_id,
        timestamp=ts(minute),
        side=side,  # type: ignore[arg-type]
        qty=Decimal(qty),
        price=Decimal(price),
        fee=Decimal(fee),
        exchange_realized_pnl=Decimal(exchange_realized_pnl) if exchange_realized_pnl is not None else None,
        funding=Decimal(funding) if funding is not None else None,
        sequence=int("".join(ch for ch in fill_id if ch.isdigit()) or 0),
        raw={"fixture": fill_id},
    )


def acceptance_fills() -> list[RawFill]:
    return [
        fill("1", 1, "buy", "1", "100", symbol="WIN"),
        fill("2", 2, "sell", "1", "120", symbol="WIN"),
        fill("3", 3, "buy", "1", "100", symbol="LOSS"),
        fill("4", 4, "sell", "1", "80", symbol="LOSS"),
        fill("5", 5, "buy", "3", "100", symbol="PARTWIN"),
        fill("6", 6, "sell", "1", "90", symbol="PARTWIN"),
        fill("7", 7, "sell", "2", "120", symbol="PARTWIN"),
        fill("8", 8, "buy", "3", "100", symbol="PARTLOSS"),
        fill("9", 9, "sell", "2", "80", symbol="PARTLOSS"),
        fill("10", 10, "sell", "1", "130", symbol="PARTLOSS"),
        fill("11", 11, "buy", "2", "100", symbol="FLIP"),
        fill("12", 12, "sell", "3", "90", symbol="FLIP"),
        fill("13", 13, "buy", "1", "80", symbol="FLIP"),
    ]


def scenario_fills() -> list[RawFill]:
    """Fixture set with distinct symbols for human dashboard review."""
    return [
        fill("101", 1, "buy", "1", "100", symbol="LONGWIN"),
        fill("102", 2, "sell", "1", "125", symbol="LONGWIN"),
        fill("103", 3, "sell", "2", "100", symbol="SHORTWIN"),
        fill("104", 4, "buy", "2", "80", symbol="SHORTWIN"),
        fill("120", 20, "buy", "1", "100", symbol="LONGLOSS"),
        fill("121", 21, "sell", "1", "80", symbol="LONGLOSS"),
        fill("122", 22, "sell", "1", "100", symbol="SHORTLOSS"),
        fill("123", 23, "buy", "1", "120", symbol="SHORTLOSS"),
        fill("105", 5, "buy", "3", "100", symbol="PARTIAL_TOTAL_LOSS"),
        fill("106", 6, "sell", "2", "70", symbol="PARTIAL_TOTAL_LOSS"),
        fill("107", 7, "sell", "1", "130", symbol="PARTIAL_TOTAL_LOSS"),
        fill("108", 8, "buy", "3", "100", symbol="PARTIAL_TOTAL_WIN"),
        fill("109", 9, "sell", "2", "130", symbol="PARTIAL_TOTAL_WIN"),
        fill("110", 10, "sell", "1", "80", symbol="PARTIAL_TOTAL_WIN"),
        fill("111", 11, "buy", "1", "100", symbol="SCALEIN"),
        fill("112", 12, "buy", "3", "200", symbol="SCALEIN"),
        fill("113", 13, "sell", "4", "180", symbol="SCALEIN"),
        fill("114", 14, "buy", "2", "100", symbol="FLIPCASE"),
        fill("115", 15, "sell", "3", "90", symbol="FLIPCASE"),
        fill("116", 16, "buy", "1", "80", symbol="FLIPCASE"),
        fill("117", 17, "buy", "1", "100", symbol="BREAKEVEN"),
        fill("118", 18, "sell", "1", "100", symbol="BREAKEVEN"),
        fill("119", 19, "buy", "1", "100", symbol="OPENLEFT"),
    ]


EXPECTED_SCENARIOS = {
    ("LONGWIN", "long"): {"net_pnl": Decimal("25"), "return_on_cost": Decimal("25.0")},
    ("LONGLOSS", "long"): {"net_pnl": Decimal("-20"), "return_on_cost": Decimal("-20.0")},
    ("SHORTWIN", "short"): {"net_pnl": Decimal("40"), "return_on_cost": Decimal("20.0")},
    ("SHORTLOSS", "short"): {"net_pnl": Decimal("-20"), "return_on_cost": Decimal("-20.0")},
    ("PARTIAL_TOTAL_LOSS", "long"): {"net_pnl": Decimal("-30"), "return_on_cost": Decimal("-10.0")},
    ("PARTIAL_TOTAL_WIN", "long"): {"net_pnl": Decimal("40"), "return_on_cost": Decimal("13.33333333333333333333333333")},
    ("SCALEIN", "long"): {"net_pnl": Decimal("20"), "return_on_cost": Decimal("2.857142857142857142857142857")},
    ("FLIPCASE", "long"): {"net_pnl": Decimal("-20"), "return_on_cost": Decimal("-10.0")},
    ("FLIPCASE", "short"): {"net_pnl": Decimal("10"), "return_on_cost": Decimal("11.11111111111111111111111111")},
    ("BREAKEVEN", "long"): {"net_pnl": Decimal("0"), "return_on_cost": Decimal("0")},
}


def validate_expected_scenarios(round_trips) -> list[dict]:
    checks: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for rt in round_trips:
        key = (rt.symbol, rt.direction)
        expected = EXPECTED_SCENARIOS.get(key)
        if expected is None:
            continue
        seen.add(key)
        pnl_ok = rt.net_pnl == expected["net_pnl"]
        roc = rt.return_on_cost
        roc_ok = roc is not None and abs(roc - expected["return_on_cost"]) < Decimal("0.00000001")
        checks.append(
            {
                "symbol": rt.symbol,
                "direction": rt.direction,
                "expected_net_pnl": str(expected["net_pnl"]),
                "actual_net_pnl": str(rt.net_pnl),
                "expected_return_on_cost": str(expected["return_on_cost"]),
                "actual_return_on_cost": str(roc),
                "passed": bool(pnl_ok and roc_ok),
            }
        )
    for key, expected in EXPECTED_SCENARIOS.items():
        if key not in seen:
            checks.append(
                {
                    "symbol": key[0],
                    "direction": key[1],
                    "expected_net_pnl": str(expected["net_pnl"]),
                    "actual_net_pnl": None,
                    "expected_return_on_cost": str(expected["return_on_cost"]),
                    "actual_return_on_cost": None,
                    "passed": False,
                }
            )
    return sorted(checks, key=lambda c: (c["symbol"], c["direction"]))
