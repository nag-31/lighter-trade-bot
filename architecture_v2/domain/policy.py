"""Shared policy contracts for account state, cutoffs, and projection runs.

These values are deliberately small, immutable domain objects.  Keeping them
out of the SQLite adapter prevents a consumer (Dashboard, Journal, or alerts)
from inventing its own interpretation of the reporting boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Mapping


UTC = timezone.utc
REPORT_START_UTC = datetime(2026, 6, 1, tzinfo=UTC)


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


class RunMode(str, Enum):
    LIVE = "LIVE"
    BACKFILL = "BACKFILL"
    REPAIR = "REPAIR"
    SHADOW = "SHADOW"

    @property
    def allows_alerts(self) -> bool:
        return self is RunMode.LIVE


@dataclass(frozen=True, slots=True)
class ProjectionWindow:
    """The two-boundary time policy used by every report projection.

    ``context_start`` controls how far back a projector may read to establish
    an opening position.  ``report_start`` controls what is published as PnL
    or a closed trade.  Context can therefore precede the reporting cutoff
    without leaking pre-cutoff activity into the report.
    """

    context_start: datetime | None = None
    report_start: datetime = REPORT_START_UTC
    report_end: datetime | None = None
    as_of: datetime | None = None
    timezone: str = "UTC"

    def __post_init__(self) -> None:
        if self.context_start is not None:
            object.__setattr__(self, "context_start", _utc(self.context_start, "context_start"))
        object.__setattr__(self, "report_start", _utc(self.report_start, "report_start"))
        if self.report_end is not None:
            object.__setattr__(self, "report_end", _utc(self.report_end, "report_end"))
            if self.report_end <= self.report_start:
                raise ValueError("report_end must be later than report_start")
        if self.as_of is not None:
            object.__setattr__(self, "as_of", _utc(self.as_of, "as_of"))
        if self.context_start is not None and self.context_start > self.report_start:
            raise ValueError("context_start cannot be later than report_start")
        if not self.timezone.strip():
            raise ValueError("timezone must not be blank")

    @classmethod
    def default(cls, *, as_of: datetime | None = None) -> "ProjectionWindow":
        return cls(report_start=REPORT_START_UTC, as_of=as_of)

    def in_report(self, occurred_at: datetime) -> bool:
        value = _utc(occurred_at, "occurred_at")
        if value < self.report_start:
            return False
        return self.report_end is None or value < self.report_end

    def in_context(self, occurred_at: datetime) -> bool:
        value = _utc(occurred_at, "occurred_at")
        return self.context_start is None or value >= self.context_start

    def as_metadata(self) -> Mapping[str, str]:
        return {
            "context_start": self.context_start.isoformat() if self.context_start else "",
            "report_start": self.report_start.isoformat(),
            "report_end": self.report_end.isoformat() if self.report_end else "",
            "as_of": self.as_of.isoformat() if self.as_of else "",
            "timezone": self.timezone,
        }


@dataclass(frozen=True, slots=True)
class AccountState:
    """Independent controls for one account's data and consumers."""

    ingestion_enabled: bool = True
    alerts_enabled: bool = True
    portfolio_included: bool = True
    historical_visible: bool = True


@dataclass(frozen=True, slots=True)
class AccountLabel:
    account_id: str
    label: str
    valid_from: datetime
    valid_until: datetime | None = None
    changed_by: str = "system"
    change_reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "valid_from", _utc(self.valid_from, "valid_from"))
        if self.valid_until is not None:
            object.__setattr__(self, "valid_until", _utc(self.valid_until, "valid_until"))
            if self.valid_until <= self.valid_from:
                raise ValueError("valid_until must be later than valid_from")
        if not self.account_id.strip() or not self.label.strip():
            raise ValueError("account_id and label are required")


@dataclass(frozen=True, slots=True)
class ProjectionRunPolicy:
    mode: RunMode = RunMode.LIVE
    alerts_enabled: bool = True

    @property
    def alerts_allowed(self) -> bool:
        return self.mode.allows_alerts and self.alerts_enabled


@dataclass(frozen=True, slots=True)
class ProjectionRunManifest:
    run_id: str
    mode: RunMode
    accounting_version: str
    input_snapshot_hash: str
    projection_hash: str
    window: ProjectionWindow
    rows_read: int
    rows_written: int
    alerts_created: int
    status: str = "completed"

    def __post_init__(self) -> None:
        if self.rows_read < 0 or self.rows_written < 0 or self.alerts_created < 0:
            raise ValueError("projection row counts cannot be negative")
        if self.alerts_created and not self.mode.allows_alerts:
            raise ValueError("non-live projection runs cannot create alerts")
