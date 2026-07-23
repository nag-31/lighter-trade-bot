"""Typed exchange fetch outcomes.

Exchange clients historically returned an empty mapping both when an account
had no positions and when a request failed. Reconciliation must only close
positions from an authoritative successful snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Generic, Optional, TypeVar

T = TypeVar("T")


class ResultState(str, Enum):
    OK = "ok"
    STALE = "stale"
    ERROR = "error"
    DISABLED = "disabled"


@dataclass(frozen=True)
class FetchResult(Generic[T]):
    value: T
    state: ResultState = ResultState.OK
    authoritative: bool = True
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.state == ResultState.OK

    @classmethod
    def success(cls, value: T, *, detail: str = "") -> "FetchResult[T]":
        return cls(value=value, detail=detail)

    @classmethod
    def stale(
        cls, value: T, *, error: str = "", detail: str = ""
    ) -> "FetchResult[T]":
        return cls(
            value=value,
            state=ResultState.STALE,
            authoritative=False,
            error=error,
            detail=detail,
        )

    @classmethod
    def failure(
        cls, fallback: T, *, error: str, detail: str = ""
    ) -> "FetchResult[T]":
        return cls(
            value=fallback,
            state=ResultState.ERROR,
            authoritative=False,
            error=error,
            detail=detail,
        )

    @classmethod
    def disabled(cls, fallback: T, *, detail: str) -> "FetchResult[T]":
        return cls(
            value=fallback,
            state=ResultState.DISABLED,
            authoritative=False,
            detail=detail,
        )


def coerce_fetch_result(value: T | FetchResult[T]) -> FetchResult[T]:
    """Wrap legacy client values as authoritative successes."""
    if isinstance(value, FetchResult):
        return value
    return FetchResult.success(value)
