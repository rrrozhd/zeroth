"""Cleanup-manifest construction and projection.

A manifest is the durable record of what external cleanup an authorized erasure
still owes. It is built once, inside the destructive transaction, and from then
on it is the only authority on what remains to be deleted -- so building it and
reading it back are pure functions with no access to the surfaces they describe.

Operation order in the manifest *is* the execution order: the artifact prefix
sweep, then each harvested key, then the econ deletion.
"""

from __future__ import annotations

from zeroth.governance.retention.cleanup_manifest import (
    CleanupManifest,
    CleanupOperation,
    DatabaseErasureOutcome,
    operation_id,
)
from zeroth.governance.retention.models import ErasureResult


def build_cleanup_manifest(
    result: ErasureResult,
    artifact_keys: list[str],
    join_keys: list[str],
    *,
    artifact_store: object | None,
    econ_eraser: object | None,
) -> CleanupManifest:
    """Build the external-cleanup manifest (artifact + econ operations) for an erased run."""
    artifact_status = "pending" if artifact_store is not None else "skipped"
    econ_status = "pending" if econ_eraser is not None else "skipped"
    operations = [
        CleanupOperation(
            operation_id=operation_id(
                result.tenant_id, result.run_id, "artifact_prefix", result.run_id
            ),
            kind="artifact_prefix",
            tenant_id=result.tenant_id,
            run_id=result.run_id,
            status=(
                artifact_status
                if getattr(artifact_store, "cleanup_run", None) is not None
                else "skipped"
            ),
        )
    ]
    operations.extend(
        CleanupOperation(
            operation_id=operation_id(result.tenant_id, result.run_id, "artifact_key", key),
            kind="artifact_key",
            tenant_id=result.tenant_id,
            run_id=result.run_id,
            artifact_key=key,
            status=artifact_status,
        )
        for key in artifact_keys
    )
    operations.append(
        CleanupOperation(
            operation_id=operation_id(
                result.tenant_id,
                result.run_id,
                "econ",
                "\0".join(join_keys),
            ),
            kind="econ",
            tenant_id=result.tenant_id,
            run_id=result.run_id,
            join_keys=join_keys,
            status=econ_status,
            deleted_count=None,
        )
    )
    return CleanupManifest(
        tenant_id=result.tenant_id,
        run_id=result.run_id,
        reason=result.reason,
        database_result=DatabaseErasureOutcome(
            audits_erased=result.audits_erased,
            checkpoints_deleted=result.checkpoints_deleted,
            run_redacted=result.run_redacted,
        ),
        operations=operations,
    )


def manifest_complete(manifest: CleanupManifest) -> bool:
    """Return True when every manifest operation is already completed or skipped."""
    return all(operation.status in {"completed", "skipped"} for operation in manifest.operations)


def result_from_manifest(
    manifest: CleanupManifest,
    *,
    authorization_log_id: str,
    retry_log_id: str | None,
    force_status: str | None = None,
) -> ErasureResult:
    """Project a cleanup manifest into the :class:`ErasureResult` returned to callers."""
    statuses = {operation.status for operation in manifest.operations}
    cleanup_status = force_status
    if cleanup_status is None:
        if "failed" in statuses:
            cleanup_status = "failed"
        elif statuses <= {"completed", "skipped"}:
            cleanup_status = "complete"
        else:
            cleanup_status = "pending"
    artifact_deleted = sum(
        int(operation.deleted_count or 0)
        for operation in manifest.operations
        if operation.kind in {"artifact_prefix", "artifact_key"}
    )
    econ_operations = [operation for operation in manifest.operations if operation.kind == "econ"]
    econ_deleted = None
    if econ_operations and econ_operations[0].status != "skipped":
        econ_deleted = int(econ_operations[0].deleted_count or 0)
    database = manifest.database_result
    return ErasureResult(
        run_id=manifest.run_id,
        tenant_id=manifest.tenant_id,
        reason=manifest.reason,
        audits_erased=database.audits_erased,
        checkpoints_deleted=database.checkpoints_deleted,
        run_redacted=database.run_redacted,
        artifacts_deleted=artifact_deleted,
        econ_events_deleted=econ_deleted,
        external_cleanup_status=cleanup_status,  # type: ignore[arg-type]
        authorization_log_id=authorization_log_id,
        retry_log_id=retry_log_id,
    )
