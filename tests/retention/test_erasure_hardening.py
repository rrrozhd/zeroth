from __future__ import annotations

import json
import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from tests.retention.conftest import make_audit_record
from zeroth.core.audit.erasure_schema import (
    AUDIT_CLEANUP_PAYLOAD_FIELDS,
    ERASED_PII_VALUES,
    pii_commitment_fields,
)
from zeroth.core.retention import RetentionErasureService
from zeroth.core.retention.audit_log_repository import RetentionAuditLogRepository
from zeroth.core.retention.cleanup_manifest import (
    CleanupManifest,
    CleanupOperation,
    DatabaseErasureOutcome,
    operation_id,
    parse_cleanup_manifest,
)
from zeroth.core.runs import Run


def _artifact_ref(key: str) -> dict[str, object]:
    return {
        "store": "filesystem",
        "key": key,
        "content_type": "application/octet-stream",
        "size": 3,
    }


class _TenantRecordingEconEraser:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], str]] = []

    async def delete_events_for_run(
        self,
        tenant_id: str,
        join_keys: list[str],
        *,
        idempotency_key: str,
    ) -> int:
        self.calls.append((tenant_id, list(join_keys), idempotency_key))
        return 2


async def test_econ_cleanup_is_tenant_scoped_and_ignores_nested_untrusted_join_keys(env) -> None:
    eraser = _TenantRecordingEconEraser()
    service = RetentionErasureService(
        audit_repository=env.audit_repo,
        run_repository=env.run_repo,
        policy_repository=env.policy_repo,
        legal_hold_repository=env.hold_repo,
        log_repository=env.log_repo,
        artifact_store=env.artifact_store,
        econ_eraser=eraser,
    )
    await env.run_repo.put(
        Run(
            run_id="run-econ-safe",
            graph_version_ref="graph:v1",
            deployment_ref="deploy",
            tenant_id="tenant-a",
        )
    )
    base = make_audit_record(
        audit_id="run-econ-safe-a0",
        run_id="run-econ-safe",
        tenant_id="tenant-a",
    )
    record = base.__class__.model_validate(
        {
            **base.model_dump(mode="python"),
            "execution_metadata": {"join_key": "trusted-correlation"},
            "validation_results": {"nested": {"join_key": "tenant-b-secret"}},
        }
    )
    await env.audit_repo.write(record)

    await service.erase_run("run-econ-safe", "rte", tenant_id="tenant-a")

    assert len(eraser.calls) == 1
    tenant_id, join_keys, idempotency_key = eraser.calls[0]
    assert tenant_id == "tenant-a"
    assert join_keys == ["run-econ-safe", "trusted-correlation"]
    assert idempotency_key


async def test_valid_cross_run_artifact_reference_is_never_deleted(env) -> None:
    owned_key = "run-artifact-safe/n1/owned"
    foreign_key = "run-other/n9/foreign"
    env.artifact_store.blobs.update({owned_key: b"own", foreign_key: b"foreign"})
    await env.run_repo.put(
        Run(run_id="run-artifact-safe", graph_version_ref="graph:v1", deployment_ref="deploy")
    )
    base = make_audit_record(
        audit_id="run-artifact-safe-a0",
        run_id="run-artifact-safe",
    )
    record = base.model_copy(
        update={
            "output_snapshot": {
                "owned": _artifact_ref(owned_key),
                "attack": _artifact_ref(foreign_key),
            }
        }
    )
    await env.audit_repo.write(record)

    await env.service.erase_run("run-artifact-safe", "rte")

    assert owned_key not in env.artifact_store.blobs
    assert foreign_key in env.artifact_store.blobs
    assert foreign_key not in env.artifact_store.deleted_keys


@pytest.mark.parametrize("tamper", ["value", "missing"], ids=["wrong-value", "missing-field"])
async def test_tampered_pii_commitments_roll_back_before_erasure(env, tamper: str) -> None:
    await env.seed_run("run-tampered-commitment", n_audits=1)
    async with env.database.transaction() as connection:
        row = await connection.fetch_one(
            "SELECT audit_id, record_json FROM node_audits WHERE run_id = ?",
            ("run-tampered-commitment",),
        )
        assert row is not None
        payload = json.loads(row["record_json"])
        if tamper == "value":
            payload["pii_commitments"]["input_snapshot"] = "0" * 64
        else:
            payload["pii_commitments"].pop("approval_actions")
        await connection.execute(
            "UPDATE node_audits SET record_json = ? WHERE audit_id = ?",
            (json.dumps(payload), row["audit_id"]),
        )

    with pytest.raises(ValueError, match="pii_commitments"):
        await env.service.erase_run("run-tampered-commitment", "rte")

    entries = await env.log_repo.list_for_run("run-tampered-commitment")
    assert not any(entry["action"] == "erasure_authorized" for entry in entries)
    records = await env.audit_repo.list_by_run("run-tampered-commitment")
    assert records[0].erased is False
    async with env.database.transaction() as connection:
        checkpoints = await connection.fetch_all(
            "SELECT checkpoint_id FROM run_checkpoints WHERE run_id = ?",
            ("run-tampered-commitment",),
        )
    assert checkpoints


async def test_erasure_field_schemas_are_centralized_and_consistent() -> None:
    latest_fields = set(pii_commitment_fields(3))
    assert set(ERASED_PII_VALUES) == latest_fields
    assert set(AUDIT_CLEANUP_PAYLOAD_FIELDS) <= latest_fields


async def test_retry_rejects_malformed_versioned_manifest_before_cleanup(env) -> None:
    log_id = await env.log_repo.record(
        tenant_id="default",
        run_id="run-malformed",
        action="erasure_authorized",
        reason="rte",
        detail={"version": 999, "operations": "not-a-list"},
    )

    with pytest.raises(ValueError, match="manifest"):
        await env.service.retry_external_cleanup(log_id)

    assert env.artifact_store.cleanup_calls == []
    assert env.artifact_store.deleted_keys == []


async def test_retry_rejects_manifest_operation_outside_authorized_identity(env) -> None:
    foreign_key = "foreign-run/n1/blob"
    env.artifact_store.blobs[foreign_key] = b"foreign"
    detail = {
        "version": 1,
        "tenant_id": "default",
        "run_id": "run-authorized",
        "reason": "rte",
        "database_result": {},
        "operations": [
            {
                "operation_id": operation_id(
                    "tenant-b", "foreign-run", "artifact_key", foreign_key
                ),
                "kind": "artifact_key",
                "tenant_id": "tenant-b",
                "run_id": "foreign-run",
                "status": "pending",
                "artifact_key": foreign_key,
            }
        ],
    }
    log_id = await env.log_repo.record(
        tenant_id="default",
        run_id="run-authorized",
        action="erasure_authorized",
        reason="rte",
        detail=detail,
    )

    with pytest.raises(ValueError, match="manifest"):
        await env.service.retry_external_cleanup(log_id)

    assert foreign_key in env.artifact_store.blobs


async def test_legacy_manifest_migration_preserves_safe_completed_progress() -> None:
    raw = {
        "artifact_keys": ["run-old/n1/blob"],
        "join_keys": ["run-old"],
        "database_result": {
            "audits_erased": 2,
            "checkpoints_deleted": 1,
            "run_redacted": True,
            "artifacts_deleted": 99,
            "econ_events_deleted": 99,
        },
        "cleanup_status": {
            "artifact_prefix": {"status": "completed", "deleted_count": 1},
            "artifact_keys": {"run-old/n1/blob": {"status": "completed", "deleted_count": 1}},
            "econ": {"status": "skipped", "deleted_count": None},
        },
    }

    manifest = parse_cleanup_manifest(
        raw,
        tenant_id="default",
        run_id="run-old",
        reason="rte",
    )

    assert manifest.version == 1
    assert manifest.database_result.audits_erased == 2
    assert all(operation.status in {"completed", "skipped"} for operation in manifest.operations)


async def _record_pending_artifact_authorization(env, key: str) -> str:
    operation = CleanupOperation(
        operation_id=operation_id("default", "run-claim", "artifact_key", key),
        kind="artifact_key",
        tenant_id="default",
        run_id="run-claim",
        artifact_key=key,
    )
    manifest = CleanupManifest(
        tenant_id="default",
        run_id="run-claim",
        reason="rte",
        database_result=DatabaseErasureOutcome(),
        operations=[operation],
    )
    return await env.log_repo.record(
        tenant_id="default",
        run_id="run-claim",
        action="erasure_authorized",
        reason="rte",
        detail=manifest.model_dump(mode="json"),
    )


class _IdempotentBlockingStore:
    def __init__(self, key: str) -> None:
        self.key = key
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[str] = []
        self.side_effect_count = 0
        self._results: dict[str, bool] = {}

    async def delete(self, key: str, *, idempotency_key: str) -> bool:
        self.calls.append(idempotency_key)
        self.entered.set()
        await self.release.wait()
        if idempotency_key in self._results:
            return self._results[idempotency_key]
        self.side_effect_count += 1
        self._results[idempotency_key] = True
        return True


async def test_concurrent_retries_share_one_durable_claim(env) -> None:
    key = "run-claim/n1/blob"
    log_id = await _record_pending_artifact_authorization(env, key)
    store = _IdempotentBlockingStore(key)
    env.service._artifact_store = store

    first = asyncio.create_task(env.service.retry_external_cleanup(log_id))
    await asyncio.wait_for(store.entered.wait(), timeout=1)
    second = await asyncio.wait_for(env.service.retry_external_cleanup(log_id), timeout=1)
    assert second.external_cleanup_status == "pending"
    store.release.set()
    completed = await asyncio.wait_for(first, timeout=1)

    assert completed.external_cleanup_status == "complete"
    assert store.side_effect_count == 1
    assert len(store.calls) == 1


class _FailCompletedProgressLog(RetentionAuditLogRepository):
    def __init__(self, database: Any) -> None:
        super().__init__(database)
        self.fail_once = True

    async def record(
        self,
        *,
        tenant_id: str,
        action: str,
        run_id: str | None = None,
        reason: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> str:
        if action == "external_cleanup_operation" and self.fail_once and detail is not None:
            operations = detail["manifest"]["operations"]
            if any(operation["status"] == "completed" for operation in operations):
                self.fail_once = False
                raise RuntimeError("crash after external operation")
        return await super().record(
            tenant_id=tenant_id,
            action=action,
            run_id=run_id,
            reason=reason,
            detail=detail,
        )


async def test_retry_after_progress_interruption_reuses_operation_id(env) -> None:
    key = "run-claim/n1/blob"
    log_id = await _record_pending_artifact_authorization(env, key)
    store = _IdempotentBlockingStore(key)
    store.release.set()
    log_repo = _FailCompletedProgressLog(env.database)
    service = RetentionErasureService(
        audit_repository=env.audit_repo,
        run_repository=env.run_repo,
        policy_repository=env.policy_repo,
        legal_hold_repository=env.hold_repo,
        log_repository=log_repo,
        artifact_store=store,
    )

    with pytest.raises(RuntimeError, match="crash after external operation"):
        await service.retry_external_cleanup(log_id)
    completed = await service.retry_external_cleanup(log_id)

    assert completed.external_cleanup_status == "complete"
    assert completed.artifacts_deleted == 1
    assert store.side_effect_count == 1
    assert len(set(store.calls)) == 1


class _AlwaysFailStore:
    async def delete(self, key: str, *, idempotency_key: str) -> bool:
        raise RuntimeError("external deletion unavailable")


async def test_failed_external_cleanup_is_explicit_in_result(env) -> None:
    log_id = await _record_pending_artifact_authorization(env, "run-claim/n1/blob")
    env.service._artifact_store = _AlwaysFailStore()

    result = await env.service.retry_external_cleanup(log_id)

    assert result.external_cleanup_status == "failed"
    assert result.authorization_log_id == log_id
    assert result.retry_log_id is not None


class _CompatibilityFailLog(RetentionAuditLogRepository):
    async def record(self, *, action: str, **kwargs: Any) -> str:
        if action in {"crypto_erase_audits", "artifact_cleanup", "erase_run_complete"}:
            raise RuntimeError("compatibility log unavailable")
        return await super().record(action=action, **kwargs)


async def test_compatibility_log_failure_does_not_block_external_cleanup(env) -> None:
    key = "run-compat/n1/blob"
    env.artifact_store.blobs[key] = b"pii"
    await env.run_repo.put(
        Run(run_id="run-compat", graph_version_ref="graph:v1", deployment_ref="deploy")
    )
    await env.audit_repo.write(
        make_audit_record(
            audit_id="run-compat-a0",
            run_id="run-compat",
            artifact_key=key,
        )
    )
    service = RetentionErasureService(
        audit_repository=env.audit_repo,
        run_repository=env.run_repo,
        policy_repository=env.policy_repo,
        legal_hold_repository=env.hold_repo,
        log_repository=_CompatibilityFailLog(env.database),
        artifact_store=env.artifact_store,
    )

    result = await service.erase_run("run-compat", "rte")

    assert result.external_cleanup_status == "complete"
    assert key not in env.artifact_store.blobs
