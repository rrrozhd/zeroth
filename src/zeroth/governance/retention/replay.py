"""Rebuilding cleanup state from a run's retention audit entries.

The materialized ``retention_cleanup_state`` table is the fast path. This is the
fallback for authorizations written before that table existed: it folds the
append-only log back into the same claim/lease/terminal view, so a legacy row
makes the same fencing decision a materialized one would.

Two rules carry the fencing guarantee and are the reason this replay is ordered
rather than a simple last-write-wins scan. Events apply in ``revision`` order,
not row order; and a claim whose ``generation`` is not newer than the current
one is discarded, so an expired worker can never take the lease back.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from zeroth.core.retention.cleanup_manifest import CleanupManifest, parse_cleanup_manifest
from zeroth.platform.storage.json import from_json_value


@dataclass(slots=True)
class CleanupReplayState:
    """Current claim/lease/terminal view of one cleanup manifest (replayed or materialized)."""

    manifest: CleanupManifest
    generation: int = 0
    revision: int = 0
    active_claim_id: str | None = None
    active_claim_log_id: str | None = None
    lease_expires_at: datetime | None = None
    terminal_status: str | None = None
    terminal_log_id: str | None = None


def replay_cleanup_state(
    authorization: dict[str, Any],
    entries: list[dict[str, Any]],
) -> CleanupReplayState:
    """Rebuild cleanup state by replaying a run's retention audit entries (legacy rows)."""
    log_id = str(authorization["log_id"])
    tenant_id = str(authorization["tenant_id"])
    run_id = str(authorization["run_id"])
    reason = str(authorization["reason"])
    authorization_detail = from_json_value(authorization["detail"])
    state = CleanupReplayState(
        manifest=parse_cleanup_manifest(
            authorization_detail,
            tenant_id=tenant_id,
            run_id=run_id,
            reason=reason,
        )
    )
    versioned_events: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for entry in entries:
        detail = from_json_value(entry["detail"])
        if not isinstance(detail, dict) or detail.get("authorization_log_id") != log_id:
            continue
        revision = detail.get("revision")
        generation = detail.get("generation")
        if isinstance(revision, int) and isinstance(generation, int):
            versioned_events.append((revision, entry, detail))
        elif "manifest" in detail:
            state.manifest = parse_cleanup_manifest(
                detail["manifest"],
                tenant_id=tenant_id,
                run_id=run_id,
                reason=reason,
            )
        elif (
            isinstance(authorization_detail, dict)
            and "cleanup_status" in detail
            and "version" not in authorization_detail
        ):
            migrated_detail = dict(authorization_detail)
            migrated_detail["cleanup_status"] = detail["cleanup_status"]
            state.manifest = parse_cleanup_manifest(
                migrated_detail,
                tenant_id=tenant_id,
                run_id=run_id,
                reason=reason,
            )
    operation_indexes = {
        operation.operation_id: index for index, operation in enumerate(state.manifest.operations)
    }
    for revision, entry, detail in sorted(versioned_events, key=lambda item: item[0]):
        if revision <= state.revision:
            continue
        generation = int(detail["generation"])
        action = str(entry["action"])
        claim_id = detail.get("claim_id")
        if action == "external_cleanup_claimed":
            if generation <= state.generation:
                continue
            state.generation = generation
            state.revision = revision
            state.active_claim_id = str(claim_id)
            state.active_claim_log_id = str(entry["log_id"])
            state.lease_expires_at = datetime.fromisoformat(str(detail["lease_expires_at"]))
            state.terminal_log_id = None
            continue
        if generation != state.generation:
            continue
        if action == "external_cleanup_heartbeat":
            if claim_id != state.active_claim_id:
                continue
            state.revision = revision
            state.lease_expires_at = datetime.fromisoformat(str(detail["lease_expires_at"]))
        elif action == "external_cleanup_operation":
            if claim_id != state.active_claim_id:
                continue
            operation_id_value = str(detail["operation_id"])
            index = operation_indexes.get(operation_id_value)
            if index is None:
                raise ValueError("cleanup delta references unknown operation")
            operation = state.manifest.operations[index]
            state.manifest.operations[index] = operation.model_copy(
                update={
                    "status": detail["status"],
                    "deleted_count": detail.get("deleted_count"),
                    "error": detail.get("error"),
                }
            )
            state.revision = revision
            if detail.get("lease_expires_at"):
                state.lease_expires_at = datetime.fromisoformat(str(detail["lease_expires_at"]))
        elif action == "external_cleanup_claim_released":
            if claim_id != state.active_claim_id:
                continue
            state.revision = revision
            state.active_claim_id = None
            state.active_claim_log_id = None
            state.lease_expires_at = None
        elif action in {"external_cleanup_completed", "external_cleanup_failed"}:
            if claim_id is not None and claim_id != state.active_claim_id:
                continue
            state.revision = revision
            state.active_claim_id = None
            state.active_claim_log_id = None
            state.lease_expires_at = None
            state.terminal_status = (
                "completed" if action == "external_cleanup_completed" else "failed"
            )
            state.terminal_log_id = str(entry["log_id"])
    return state
