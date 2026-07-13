"""Validated, versioned state for post-authorization external erasure cleanup."""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

CleanupKind = Literal["artifact_prefix", "artifact_key", "econ"]
CleanupStatus = Literal["pending", "in_progress", "completed", "failed", "skipped"]


class DatabaseErasureOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audits_erased: int = 0
    checkpoints_deleted: int = 0
    run_redacted: bool = False


class CleanupOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str
    kind: CleanupKind
    tenant_id: str
    run_id: str
    status: CleanupStatus = "pending"
    artifact_key: str | None = None
    join_keys: list[str] = Field(default_factory=list)
    deleted_count: int | None = 0
    error: str | None = None


class CleanupManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    tenant_id: str
    run_id: str
    reason: str
    database_result: DatabaseErasureOutcome
    operations: list[CleanupOperation]

    @model_validator(mode="after")
    def _validate_operations(self) -> CleanupManifest:
        seen: set[str] = set()
        for operation in self.operations:
            if operation.tenant_id != self.tenant_id or operation.run_id != self.run_id:
                raise ValueError("cleanup operation identity does not match manifest")
            if operation.kind == "artifact_prefix":
                target = self.run_id
            elif operation.kind == "artifact_key":
                if operation.artifact_key is None or not operation.artifact_key.startswith(
                    f"{self.run_id}/"
                ):
                    raise ValueError("artifact cleanup operation is outside run namespace")
                target = operation.artifact_key
            else:
                target = "\0".join(operation.join_keys)
            expected_id = operation_id(self.tenant_id, self.run_id, operation.kind, target)
            if operation.operation_id != expected_id or operation.operation_id in seen:
                raise ValueError("invalid or duplicate cleanup operation id")
            seen.add(operation.operation_id)
        return self


def operation_id(tenant_id: str, run_id: str, kind: CleanupKind, target: str) -> str:
    """Create a stable external-operation idempotency key."""
    raw = "\0".join((tenant_id, run_id, kind, target))
    return hashlib.sha256(raw.encode()).hexdigest()


def parse_cleanup_manifest(
    raw: Any,
    *,
    tenant_id: str,
    run_id: str,
    reason: str,
) -> CleanupManifest:
    """Validate v1 state or migrate the pre-versioned Task 5 manifest."""
    try:
        if isinstance(raw, dict) and raw.get("version") == 1:
            manifest = CleanupManifest.model_validate(raw)
        elif isinstance(raw, dict) and "cleanup_status" in raw:
            manifest = _migrate_legacy_manifest(
                raw, tenant_id=tenant_id, run_id=run_id, reason=reason
            )
        else:
            raise ValueError("unsupported or malformed cleanup manifest")
    except (ValidationError, TypeError, KeyError) as exc:
        raise ValueError("invalid cleanup manifest") from exc
    if manifest.tenant_id != tenant_id or manifest.run_id != run_id:
        raise ValueError("cleanup manifest identity does not match authorization log")
    return manifest


def _migrate_legacy_manifest(
    raw: dict[str, Any],
    *,
    tenant_id: str,
    run_id: str,
    reason: str,
) -> CleanupManifest:
    status = raw["cleanup_status"]
    operations: list[CleanupOperation] = []
    prefix = status["artifact_prefix"]
    operations.append(
        CleanupOperation(
            operation_id=operation_id(tenant_id, run_id, "artifact_prefix", run_id),
            kind="artifact_prefix",
            tenant_id=tenant_id,
            run_id=run_id,
            status=prefix["status"],
            deleted_count=prefix.get("deleted_count"),
            error=prefix.get("error"),
        )
    )
    for key in raw.get("artifact_keys", []):
        key_status = status["artifact_keys"][key]
        operations.append(
            CleanupOperation(
                operation_id=operation_id(tenant_id, run_id, "artifact_key", key),
                kind="artifact_key",
                tenant_id=tenant_id,
                run_id=run_id,
                artifact_key=key,
                status=key_status["status"],
                deleted_count=key_status.get("deleted_count"),
                error=key_status.get("error"),
            )
        )
    econ = status["econ"]
    join_keys = list(raw.get("join_keys", []))
    operations.append(
        CleanupOperation(
            operation_id=operation_id(tenant_id, run_id, "econ", "\0".join(join_keys)),
            kind="econ",
            tenant_id=tenant_id,
            run_id=run_id,
            join_keys=join_keys,
            status=econ["status"],
            deleted_count=econ.get("deleted_count"),
            error=econ.get("error"),
        )
    )
    return CleanupManifest(
        tenant_id=tenant_id,
        run_id=run_id,
        reason=reason,
        database_result=DatabaseErasureOutcome(
            audits_erased=int(raw.get("database_result", {}).get("audits_erased", 0)),
            checkpoints_deleted=int(raw.get("database_result", {}).get("checkpoints_deleted", 0)),
            run_redacted=bool(raw.get("database_result", {}).get("run_redacted", False)),
        ),
        operations=operations,
    )
