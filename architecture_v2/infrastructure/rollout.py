"""Pure rollout-gate evaluation for shadow and rollback evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from architecture_v2.domain.policy import ProjectionRunManifest, RunMode


@dataclass(frozen=True, slots=True)
class RolloutGateResult:
    passed: bool
    failures: tuple[str, ...]


def evaluate_rollout_gate(
    manifest: ProjectionRunManifest,
    *,
    shadow_summary: Mapping[str, int] | None = None,
    backup_integrity: str | None = None,
    restored_integrity: str | None = None,
    replay_hash_stable: bool = False,
    consumer_approved: bool = False,
) -> RolloutGateResult:
    """Evaluate evidence without enabling a consumer or changing a database."""
    failures: list[str] = []
    if not manifest.input_snapshot_hash or not manifest.projection_hash:
        failures.append("missing deterministic snapshot/projection hash")
    if manifest.mode is not RunMode.SHADOW:
        failures.append("consumer cutover requires a SHADOW evidence run")
    if manifest.alerts_created != 0:
        failures.append("shadow run created notification events")
    if not replay_hash_stable:
        failures.append("replay hash is not stable")
    if (shadow_summary or {}).get("UNEXPLAINED", 0) != 0:
        failures.append("unexplained shadow differences remain")
    if backup_integrity != "ok":
        failures.append("source backup integrity is not verified")
    if restored_integrity != "ok":
        failures.append("rollback restore integrity is not verified")
    if not consumer_approved:
        failures.append("consumer approval is missing")
    return RolloutGateResult(passed=not failures, failures=tuple(failures))