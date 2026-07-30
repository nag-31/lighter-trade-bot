from __future__ import annotations

from collections import defaultdict
from types import MappingProxyType
from typing import Iterable

from architecture_v2.domain.accounting import project_account
from architecture_v2.domain.models import Execution, PortfolioProjection


def project_portfolio(
    executions: Iterable[Execution],
    *,
    included_accounts: set[str] | frozenset[str] | None = None,
) -> PortfolioProjection:
    """Compose already-independent account projections without regrouping them."""
    grouped: dict[str, list[Execution]] = defaultdict(list)
    for execution in executions:
        if (
            included_accounts is not None
            and execution.account_id not in included_accounts
        ):
            continue
        grouped[execution.account_id].append(execution)

    accounts = {
        account_id: project_account(account_id, account_executions)
        for account_id, account_executions in sorted(grouped.items())
    }
    return PortfolioProjection(
        accounts=MappingProxyType(accounts),
        executions=tuple(
            execution
            for projection in accounts.values()
            for execution in projection.executions
        ),
        realizations=tuple(
            realization
            for projection in accounts.values()
            for realization in projection.realizations
        ),
        lifecycles=tuple(
            lifecycle
            for projection in accounts.values()
            for lifecycle in projection.lifecycles
        ),
        open_positions=tuple(
            position
            for projection in accounts.values()
            for position in projection.open_positions
        ),
    )
