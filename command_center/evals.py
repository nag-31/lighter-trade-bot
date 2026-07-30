"""Human-readable quality evaluations for reconstructed trade lifecycles."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Iterable


def _close(left: Any, right: Any, tolerance: float = 1e-7) -> bool:
    if left is None or right is None:
        return left is right
    return math.isclose(float(left), float(right), rel_tol=1e-9, abs_tol=tolerance)


def _weighted(items: list[dict[str, Any]]) -> float | None:
    usable = [
        item for item in items
        if item.get("price") is not None and float(item.get("size") or 0) > 0
    ]
    size = sum(float(item["size"]) for item in usable)
    if not size:
        return None
    return sum(float(item["price"]) * float(item["size"]) for item in usable) / size


def evaluate_lifecycles(
    lifecycles: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Score lifecycle data with checks a human trade reviewer would make."""
    items = list(lifecycles)
    checks = 0
    issues: list[dict[str, Any]] = []

    def assess(
        trade: dict[str, Any] | None, rule: str, passed: bool, detail: str = ""
    ) -> None:
        nonlocal checks
        checks += 1
        if not passed:
            issues.append(
                {
                    "lifecycle_id": trade.get("id") if trade else None,
                    "symbol": trade.get("symbol") if trade else None,
                    "source": trade.get("source") if trade else None,
                    "rule": rule,
                    "detail": detail,
                }
            )

    lifecycle_keys = [str(item.get("lifecycle_key") or "") for item in items]
    assess(
        None, "unique_lifecycle_keys",
        len(lifecycle_keys) == len(set(lifecycle_keys)),
        f"{len(lifecycle_keys)} lifecycle records",
    )
    all_execution_keys = [
        str(execution.get("execution_key") or "")
        for item in items for execution in item.get("executions") or []
    ]
    assess(
        None, "unique_execution_keys",
        len(all_execution_keys) == len(set(all_execution_keys)),
        f"{len(all_execution_keys)} economic execution records",
    )

    for trade in items:
        executions = trade.get("executions") or []
        batches = trade.get("batches") or trade.get("metadata", {}).get("batches", [])
        entry_executions = [
            item for item in executions if item.get("action") == "entry"
        ]
        exit_executions = [
            item for item in executions if item.get("action") == "exit"
        ]
        exit_batches = [batch for batch in batches if batch.get("family") == "exit"]
        management_batches = [
            batch for batch in batches if batch.get("family") == "management"
        ]
        status = trade.get("status")

        assess(
            trade, "fill_count",
            int(trade.get("fill_count") or 0) == len(executions),
            f"declared {trade.get('fill_count')}, found {len(executions)}",
        )
        assess(
            trade, "batch_fill_accounting",
            sum(int(batch.get("fill_count") or 0) for batch in batches)
            == len(executions),
        )
        assess(
            trade, "closed_trade_has_exit",
            status != "closed" or bool(exit_batches),
            "closed lifecycle requires an exit batch",
        )
        assess(
            trade, "closed_trade_ends_with_final_exit",
            status != "closed"
            or (
                bool(batches)
                and batches[-1].get("label") == "Final exit"
                and sum(batch.get("label") == "Final exit" for batch in batches) == 1
            ),
        )
        assess(
            trade, "open_trade_has_no_final_exit",
            status == "closed"
            or all(batch.get("label") != "Final exit" for batch in batches),
        )
        assess(
            trade, "reversal_is_explained",
            status != "reversed"
            or (
                len(management_batches) == 1
                and str(management_batches[0].get("label") or "").startswith(
                    "Direction reversal"
                )
            ),
        )
        scaled_in = int(trade.get("entry_batch_count") or 0) > 1
        scaled_out = int(trade.get("partial_exit_count") or 0) > 0
        expected_style = (
            "Direction reversed" if status == "reversed"
            else "Scaled in & out" if scaled_in and scaled_out
            else "Scaled in" if scaled_in
            else "Scaled out" if scaled_out
            else "Single entry / exit"
        )
        assess(
            trade, "management_style",
            trade.get("management_style") == expected_style,
            f"{trade.get('management_style')} should be {expected_style}",
        )

        for batch in exit_batches[:-1] if status == "closed" else exit_batches:
            pnl = float(batch.get("pnl") or 0)
            expected = (
                "Partial profit" if pnl > 0
                else "Partial loss" if pnl < 0
                else "Scale-out"
            )
            assess(
                trade, "partial_exit_label",
                batch.get("label") == expected,
                f"{batch.get('label')} should be {expected}",
            )

        reported_pnl = trade.get("pnl")
        exit_pnl_values = [
            float(item["pnl"]) for item in exit_executions
            if item.get("pnl") is not None
        ]
        expected_pnl = sum(exit_pnl_values) if exit_pnl_values else None
        assess(
            trade, "pnl_reconciliation",
            _close(reported_pnl, expected_pnl),
            f"{reported_pnl} vs {expected_pnl}",
        )

        expected_entry = _weighted(entry_executions)
        inferred_open = bool(
            trade.get("inferred_open")
            or trade.get("metadata", {}).get("inferred_open")
        )
        assess(
            trade, "entry_vwap",
            inferred_open or _close(trade.get("entry_vwap"), expected_entry),
            f"{trade.get('entry_vwap')} vs {expected_entry}",
        )
        assess(
            trade, "exit_vwap",
            _close(trade.get("exit_vwap"), _weighted(exit_executions)),
            f"{trade.get('exit_vwap')} vs {_weighted(exit_executions)}",
        )

        for previous, current in zip(executions, executions[1:]):
            if previous.get("action") != current.get("action"):
                continue
            gap = (
                datetime.fromisoformat(str(current["occurred_at"]))
                - datetime.fromisoformat(str(previous["occurred_at"]))
            ).total_seconds()
            if 0 <= gap <= 120:
                assess(
                    trade, "nearby_fills_share_batch",
                    previous.get("batch_key") == current.get("batch_key"),
                    f"{gap:.1f}s gap",
                )
            elif gap > 120:
                assess(
                    trade, "separated_fills_keep_distinct_batches",
                    previous.get("batch_key") != current.get("batch_key"),
                    f"{gap:.1f}s gap",
                )

    passed = checks - len(issues)
    return {
        "score": round((passed / checks * 100) if checks else 100.0, 2),
        "status": "pass" if not issues else "needs_review",
        "checks": checks,
        "passed": passed,
        "failed": len(issues),
        "lifecycles": len(items),
        "execution_records": len(all_execution_keys),
        "issues": issues[:100],
        "rubric": [
            "Unique lifecycle and execution identities",
            "Every raw fill accounted for exactly once",
            "Closed trades end with an explainable final exit",
            "Direction reversals are explicit management events",
            "Management style agrees with scale-in and partial-exit behavior",
            "Partial profit/loss labels agree with realized P&L",
            "Lifecycle P&L reconciles to exit executions",
            "Weighted entry and exit prices reconcile",
            "Fills within two minutes share a batch",
            "Time-separated fills remain distinct management steps",
        ],
    }
