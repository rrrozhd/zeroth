from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.retention.conftest import make_audit_record
from zeroth.governance.audit.erasure_schema import (
    AUDIT_CLEANUP_PAYLOAD_FIELDS,
    ERASED_PII_VALUES,
    pii_commitment_fields,
)
from zeroth.governance.retention import RetentionErasureService
from zeroth.governance.retention.audit_log_repository import RetentionAuditLogRepository
from zeroth.governance.retention.cleanup_manifest import (
    CleanupManifest,
    CleanupOperation,
    DatabaseErasureOutcome,
    operation_id,
    parse_cleanup_manifest,
)
from zeroth.governance.retention.erasure_service import StaleCleanupClaimError
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
    assert not any(operation.kind == "artifact_key" for operation in manifest.operations)
    econ = next(operation for operation in manifest.operations if operation.kind == "econ")
    assert econ.join_keys == ["run-old"]


async def test_complete_manifest_without_terminal_is_repaired(env) -> None:
    operations = [
        CleanupOperation(
            operation_id=operation_id("default", "run-repair", "artifact_prefix", "run-repair"),
            kind="artifact_prefix",
            tenant_id="default",
            run_id="run-repair",
            status="completed",
            deleted_count=1,
        ),
        CleanupOperation(
            operation_id=operation_id("default", "run-repair", "econ", "run-repair"),
            kind="econ",
            tenant_id="default",
            run_id="run-repair",
            join_keys=["run-repair"],
            status="skipped",
            deleted_count=None,
        ),
    ]
    manifest = CleanupManifest(
        tenant_id="default",
        run_id="run-repair",
        reason="rte",
        database_result=DatabaseErasureOutcome(),
        operations=operations,
    )
    log_id = await env.log_repo.record(
        tenant_id="default",
        run_id="run-repair",
        action="erasure_authorized",
        reason="rte",
        detail=manifest.model_dump(mode="json"),
    )

    result = await env.service.retry_external_cleanup(log_id)

    assert result.external_cleanup_status == "complete"
    assert result.retry_log_id is not None
    actions = [row["action"] for row in await env.log_repo.list_for_run("run-repair")]
    assert "external_cleanup_completed" in actions


class _FailTerminalOnceLog(RetentionAuditLogRepository):
    def __init__(self, database: Any) -> None:
        super().__init__(database)
        self.fail_once = True

    async def record_in_transaction(self, connection, *, action: str, **kwargs: Any) -> str:
        if action == "external_cleanup_completed" and self.fail_once:
            self.fail_once = False
            raise RuntimeError("terminal log unavailable")
        return await super().record_in_transaction(connection, action=action, **kwargs)


async def test_retry_repairs_terminal_after_terminal_write_failure(env) -> None:
    key = "run-claim/n1/blob"
    log_id = await _record_pending_artifact_authorization(env, key)
    store = _IdempotentBlockingStore(key)
    store.release.set()
    log_repo = _FailTerminalOnceLog(env.database)
    service = RetentionErasureService(
        audit_repository=env.audit_repo,
        run_repository=env.run_repo,
        policy_repository=env.policy_repo,
        legal_hold_repository=env.hold_repo,
        log_repository=log_repo,
        artifact_store=store,
    )

    with pytest.raises(RuntimeError, match="terminal log unavailable"):
        await service.retry_external_cleanup(log_id)
    repaired = await service.retry_external_cleanup(log_id)

    assert repaired.external_cleanup_status == "complete"
    assert repaired.retry_log_id is not None
    actions = [row["action"] for row in await log_repo.list_for_run("run-claim")]
    assert actions.count("external_cleanup_completed") == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda detail: detail.update(reason="not-authorized"),
        lambda detail: detail.update(operations=[]),
        lambda detail: detail["operations"].append(dict(detail["operations"][0])),
    ],
    ids=["reason", "empty-operations", "duplicate-prefix"],
)
async def test_manifest_invariants_reject_ambiguous_authorization(env, mutate) -> None:
    operations = [
        CleanupOperation(
            operation_id=operation_id("default", "run-invalid", "artifact_prefix", "run-invalid"),
            kind="artifact_prefix",
            tenant_id="default",
            run_id="run-invalid",
            status="skipped",
        ),
        CleanupOperation(
            operation_id=operation_id("default", "run-invalid", "econ", "run-invalid"),
            kind="econ",
            tenant_id="default",
            run_id="run-invalid",
            join_keys=["run-invalid"],
            status="skipped",
            deleted_count=None,
        ),
    ]
    detail = CleanupManifest(
        tenant_id="default",
        run_id="run-invalid",
        reason="rte",
        database_result=DatabaseErasureOutcome(),
        operations=operations,
    ).model_dump(mode="json")
    mutate(detail)
    log_id = await env.log_repo.record(
        tenant_id="default",
        run_id="run-invalid",
        action="erasure_authorized",
        reason="rte",
        detail=detail,
    )

    with pytest.raises(ValueError, match="manifest"):
        await env.service.retry_external_cleanup(log_id)


async def _record_pending_artifact_authorization(env, key: str) -> str:
    operations = [
        CleanupOperation(
            operation_id=operation_id("default", "run-claim", "artifact_prefix", "run-claim"),
            kind="artifact_prefix",
            tenant_id="default",
            run_id="run-claim",
            status="skipped",
        ),
        CleanupOperation(
            operation_id=operation_id("default", "run-claim", "artifact_key", key),
            kind="artifact_key",
            tenant_id="default",
            run_id="run-claim",
            artifact_key=key,
        ),
        CleanupOperation(
            operation_id=operation_id("default", "run-claim", "econ", "run-claim"),
            kind="econ",
            tenant_id="default",
            run_id="run-claim",
            join_keys=["run-claim"],
            status="skipped",
            deleted_count=None,
        ),
    ]
    manifest = CleanupManifest(
        tenant_id="default",
        run_id="run-claim",
        reason="rte",
        database_result=DatabaseErasureOutcome(),
        operations=operations,
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


async def test_stale_generation_cannot_release_or_progress_new_claim(env) -> None:
    key = "run-claim/n1/blob"
    log_id = await _record_pending_artifact_authorization(env, key)
    expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    future = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    await env.log_repo.record(
        tenant_id="default",
        run_id="run-claim",
        action="external_cleanup_claimed",
        reason="rte",
        detail={
            "authorization_log_id": log_id,
            "claim_id": "claim-a",
            "generation": 1,
            "revision": 1,
            "lease_expires_at": expired,
        },
    )
    claim_b_log_id = await env.log_repo.record(
        tenant_id="default",
        run_id="run-claim",
        action="external_cleanup_claimed",
        reason="rte",
        detail={
            "authorization_log_id": log_id,
            "claim_id": "claim-b",
            "generation": 2,
            "revision": 2,
            "lease_expires_at": future,
        },
    )
    operation = CleanupOperation(
        operation_id=operation_id("default", "run-claim", "artifact_key", key),
        kind="artifact_key",
        tenant_id="default",
        run_id="run-claim",
        artifact_key=key,
        status="completed",
        deleted_count=1,
    )

    await env.service._release_cleanup_claim(
        authorization_log_id=log_id,
        claim_id="claim-a",
        generation=1,
        tenant_id="default",
        run_id="run-claim",
        reason="rte",
    )
    with pytest.raises(StaleCleanupClaimError):
        await env.service._record_operation_delta(log_id, "claim-a", 1, operation)

    pending = await env.service.retry_external_cleanup(log_id)
    assert pending.external_cleanup_status == "pending"
    assert pending.retry_log_id == claim_b_log_id
    rows = await env.log_repo.list_for_run("run-claim")
    assert not any(
        row["action"] == "external_cleanup_claim_released"
        and json.loads(row["detail"])["claim_id"] == "claim-a"
        for row in rows
    )


async def test_long_operation_heartbeats_renew_claim_lease(env) -> None:
    key = "run-claim/n1/heartbeat"
    log_id = await _record_pending_artifact_authorization(env, key)
    store = _IdempotentBlockingStore(key)
    service = RetentionErasureService(
        audit_repository=env.audit_repo,
        run_repository=env.run_repo,
        policy_repository=env.policy_repo,
        legal_hold_repository=env.hold_repo,
        log_repository=env.log_repo,
        artifact_store=store,
        cleanup_lease_seconds=0.06,
    )

    task = asyncio.create_task(service.retry_external_cleanup(log_id))
    await asyncio.wait_for(store.entered.wait(), timeout=1)
    await asyncio.sleep(0.12)
    pending = await service.retry_external_cleanup(log_id)
    assert pending.external_cleanup_status == "pending"
    store.release.set()
    completed = await asyncio.wait_for(task, timeout=1)
    assert completed.external_cleanup_status == "complete"
    actions = [row["action"] for row in await env.log_repo.list_for_run("run-claim")]
    assert "external_cleanup_heartbeat" in actions


class _FailCompletedProgressLog(RetentionAuditLogRepository):
    def __init__(self, database: Any) -> None:
        super().__init__(database)
        self.fail_once = True

    async def record_in_transaction(
        self,
        connection,
        *,
        tenant_id: str,
        action: str,
        run_id: str | None = None,
        reason: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> str:
        if (
            action == "external_cleanup_operation"
            and self.fail_once
            and detail is not None
            and detail["status"] == "completed"
        ):
            self.fail_once = False
            raise RuntimeError("crash after external operation")
        return await super().record_in_transaction(
            connection,
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


async def test_operation_progress_events_are_constant_size_deltas(env, monkeypatch) -> None:
    keys = [f"run-linear/n/{index}" for index in range(100)]
    operations = [
        CleanupOperation(
            operation_id=operation_id("default", "run-linear", "artifact_prefix", "run-linear"),
            kind="artifact_prefix",
            tenant_id="default",
            run_id="run-linear",
            status="skipped",
        ),
        *[
            CleanupOperation(
                operation_id=operation_id("default", "run-linear", "artifact_key", key),
                kind="artifact_key",
                tenant_id="default",
                run_id="run-linear",
                artifact_key=key,
            )
            for key in keys
        ],
        CleanupOperation(
            operation_id=operation_id("default", "run-linear", "econ", "run-linear"),
            kind="econ",
            tenant_id="default",
            run_id="run-linear",
            join_keys=["run-linear"],
            status="skipped",
            deleted_count=None,
        ),
    ]
    manifest = CleanupManifest(
        tenant_id="default",
        run_id="run-linear",
        reason="rte",
        database_result=DatabaseErasureOutcome(),
        operations=operations,
    )
    log_id = await env.log_repo.record(
        tenant_id="default",
        run_id="run-linear",
        action="erasure_authorized",
        reason="rte",
        detail=manifest.model_dump(mode="json"),
    )
    store = _IdempotentBlockingStore(keys[0])
    store.release.set()
    env.service._artifact_store = store

    full_log_loads = 0
    replay_calls = 0
    operation_lookups = 0
    original_list = env.log_repo.list_for_run_in_transaction
    original_replay = env.service._replay_cleanup_state
    original_operation_lookup = env.service._cleanup_state.get_operation_in_transaction

    async def counted_list(*args, **kwargs):
        nonlocal full_log_loads
        full_log_loads += 1
        return await original_list(*args, **kwargs)

    def counted_replay(*args, **kwargs):
        nonlocal replay_calls
        replay_calls += 1
        return original_replay(*args, **kwargs)

    async def counted_operation_lookup(*args, **kwargs):
        nonlocal operation_lookups
        operation_lookups += 1
        return await original_operation_lookup(*args, **kwargs)

    monkeypatch.setattr(env.log_repo, "list_for_run_in_transaction", counted_list)
    monkeypatch.setattr(env.service, "_replay_cleanup_state", counted_replay)
    monkeypatch.setattr(
        type(env.service._cleanup_state),
        "get_operation_in_transaction",
        staticmethod(counted_operation_lookup),
    )

    await env.service.retry_external_cleanup(log_id)

    assert full_log_loads == 1
    assert replay_calls == 1
    assert operation_lookups == 2 * len(keys)

    rows = await env.log_repo.list_for_run("run-linear")
    deltas = [row for row in rows if row["action"] == "external_cleanup_operation"]
    assert len(deltas) == 2 * len(keys)
    assert all('"manifest"' not in row["detail"] for row in deltas)
    assert max(len(row["detail"]) for row in deltas) < 800


async def test_new_authorization_uses_materialized_state_without_log_replay(
    env, monkeypatch
) -> None:
    await env.seed_run(
        "run-materialized",
        n_audits=1,
        artifact_key="run-materialized/n/blob",
    )
    full_log_loads = 0
    original_list = env.log_repo.list_for_run_in_transaction

    async def counted_list(*args, **kwargs):
        nonlocal full_log_loads
        full_log_loads += 1
        return await original_list(*args, **kwargs)

    monkeypatch.setattr(env.log_repo, "list_for_run_in_transaction", counted_list)

    result = await env.service.erase_run("run-materialized", "rte")

    assert result.external_cleanup_status == "complete"
    assert full_log_loads == 0
    async with env.database.transaction() as connection:
        state = await connection.fetch_one(
            "SELECT * FROM retention_cleanup_state WHERE authorization_log_id = ?",
            (result.authorization_log_id,),
        )
        operations = await connection.fetch_all(
            """
            SELECT operation_id FROM retention_cleanup_operations
            WHERE authorization_log_id = ?
            """,
            (result.authorization_log_id,),
        )
    assert state is not None
    assert state["terminal_status"] == "completed"
    assert len(operations) == 3
