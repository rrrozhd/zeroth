"""Shared fixtures for WS-E retention tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from zeroth.contracts.graph import (
    CancellationFence,
    SchedulingState,
    TokenEngineSnapshot,
    TokenEngineSnapshotState,
    TokenEnvelope,
    TokenLifecycleState,
)
from zeroth.governance.audit import AuditRepository, NodeAuditRecord
from zeroth.governance.retention import (
    LegalHoldRepository,
    RetentionAuditLogRepository,
    RetentionErasureService,
    RetentionPolicyRepository,
)
from zeroth.contracts.governed.models.approval import ApprovalRequest
from zeroth.runtime.runs import Run, RunFailureState, RunHistoryEntry
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.platform.signing import EnvHmacSigner


async def seed_token_snapshot(env, run_id: str, *, artifact_key: str, ssn: str) -> None:
    """Persist representative PII and an artifact ref in token-engine state."""
    token = TokenEnvelope(
        token_id=f"{run_id}-token",
        current_node_id="node-a",
        payload={
            "ssn": ssn,
            "artifact": {
                "store": "filesystem",
                "key": artifact_key,
                "content_type": "application/octet-stream",
                "size": 3,
            },
        },
        lifecycle_state=TokenLifecycleState.ACTIVE,
        scheduling_state=SchedulingState.QUEUED,
        state_revision=0,
    )
    snapshot = TokenEngineSnapshot(
        run_id=run_id,
        revision=0,
        state=TokenEngineSnapshotState.RUNNING,
        next_token_ordinal=1,
        queue=(token,),
        tokens=(token,),
        cancellation_fence=CancellationFence(generation=0, state_revision=0),
    )
    env.artifact_store.blobs[artifact_key] = b"pii"
    await env.run_repo.compare_and_swap_token_snapshot(
        run_id,
        expected_revision=None,
        snapshot=snapshot,
    )


class FakeArtifactStore:
    """In-memory artifact store recording cleanup/delete calls for assertions."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.cleanup_calls: list[str] = []
        self.deleted_keys: list[str] = []
        self._receipts: dict[str, int | bool] = {}

    async def cleanup_run(self, run_id: str, *, idempotency_key: str) -> int:
        if idempotency_key in self._receipts:
            return int(self._receipts[idempotency_key])
        self.cleanup_calls.append(run_id)
        removed = [k for k in self.blobs if k.startswith(f"{run_id}/")]
        for key in removed:
            del self.blobs[key]
        self._receipts[idempotency_key] = len(removed)
        return len(removed)

    async def delete(self, key: str, *, idempotency_key: str) -> bool:
        if idempotency_key in self._receipts:
            return bool(self._receipts[idempotency_key])
        self.deleted_keys.append(key)
        result = self.blobs.pop(key, None) is not None
        self._receipts[idempotency_key] = result
        return result


def make_audit_record(
    *,
    audit_id: str,
    run_id: str,
    tenant_id: str = "default",
    node_id: str = "n1",
    deployment_ref: str = "deploy",
    started_at: datetime | None = None,
    artifact_key: str | None = None,
    ssn: str = "123-45-6789",
) -> NodeAuditRecord:
    """A record carrying representative PII across every erasable field."""
    started = started_at or datetime(2026, 7, 11, tzinfo=UTC)
    output: dict = {"result": "ok", "ssn": ssn}
    if artifact_key is not None:
        output["artifact"] = {
            "store": "filesystem",
            "key": artifact_key,
            "content_type": "text/plain",
            "size": 3,
        }
    return NodeAuditRecord(
        audit_id=audit_id,
        run_id=run_id,
        tenant_id=tenant_id,
        node_id=node_id,
        graph_version_ref="graph:v1",
        deployment_ref=deployment_ref,
        status="completed",
        input_snapshot={"ssn": ssn, "name": "Jane Doe"},
        output_snapshot=output,
        validation_results={"email": "jane@example.com"},
        execution_metadata={"prompt": ssn},
        stdout="stdout-with-" + ssn,
        stderr="stderr-secret",
        error="error-" + ssn,
        tool_calls=[
            {
                "tool_ref": "t",
                "alias": "a",
                "arguments": {"query": ssn},
                "outcome": {"row": ssn},
            }
        ],
        memory_interactions=[
            {
                "memory_ref": "m",
                "connector_type": "kv",
                "scope": "thread",
                "operation": "write",
                "key": "k",
                "value": ssn,
            }
        ],
        started_at=started,
        completed_at=started.replace(microsecond=1),
    )


@dataclass
class RetentionEnv:
    """Wired retention surface over one migrated sqlite database."""

    database: object
    signer: EnvHmacSigner
    audit_repo: AuditRepository
    run_repo: RunRepository
    policy_repo: RetentionPolicyRepository
    hold_repo: LegalHoldRepository
    log_repo: RetentionAuditLogRepository
    artifact_store: FakeArtifactStore
    service: RetentionErasureService
    econ_eraser: object | None = None
    run_ids: list[str] = field(default_factory=list)

    async def seed_run(
        self,
        run_id: str,
        *,
        tenant_id: str = "default",
        started_at: datetime | None = None,
        created_at: datetime | None = None,
        n_audits: int = 2,
        artifact_key: str | None = None,
        ssn: str = "123-45-6789",
    ) -> None:
        """Create a run (row + checkpoint) with N chained PII-bearing audits.

        ``created_at`` backdates the persisted ``node_audits.created_at`` column
        (which drives TTL purge) to simulate aged records — the write path always
        stamps the real wall-clock time, so age must be injected explicitly.
        """
        run = Run(
            run_id=run_id,
            graph_version_ref="graph:v1",
            deployment_ref="deploy",
            tenant_id=tenant_id,
            final_output={"answer": ssn},
            error="run-error-" + ssn,
            artifacts={"blob": ssn},
            metadata={"pii": ssn},
            # failure_state holds the failure message/details; erasure must clear
            # it too (else `error` re-derives from it on read — audit F1 re-audit).
            failure_state=RunFailureState(reason="failed", message="err-" + ssn),
            # pending_approval carries the requester's free-form reason + metadata
            # for an outstanding gate; erasure must clear it (audit F1 re-audit^2).
            pending_approval=ApprovalRequest(
                request_id="req-" + run_id,
                run_id=run_id,
                workflow_name="wf",
                step_name="gate",
                executor_name="exec",
                reason="approve for " + ssn,
                metadata={"requester_note": ssn},
            ),
            # Per-node input/output snapshots persist in runs.execution_history;
            # seed PII here so erasure coverage catches residue (audit F1).
            execution_history=[
                RunHistoryEntry(
                    node_id="n0",
                    status="completed",
                    input_snapshot={"prompt": ssn},
                    output_snapshot={"response": ssn},
                )
            ],
        )
        await self.run_repo.put(run)
        if artifact_key is not None:
            self.artifact_store.blobs[artifact_key] = b"pii"
        for i in range(n_audits):
            await self.audit_repo.write(
                make_audit_record(
                    audit_id=f"{run_id}-a{i}",
                    run_id=run_id,
                    tenant_id=tenant_id,
                    node_id=f"n{i}",
                    started_at=started_at,
                    artifact_key=artifact_key if i == 0 else None,
                    ssn=ssn,
                )
            )
        if created_at is not None:
            async with self.database.transaction() as connection:
                await connection.execute(
                    "UPDATE node_audits SET created_at = ? WHERE run_id = ?",
                    (created_at.astimezone(UTC).isoformat(), run_id),
                )
        self.run_ids.append(run_id)


def _build_env(database) -> RetentionEnv:
    """Wire a full RetentionEnv over a given (possibly encrypted) database."""
    signer = EnvHmacSigner(key_id="k1", keys={"k1": b"retention-secret"})
    audit_repo = AuditRepository(database, signer=signer)
    run_repo = RunRepository(database)
    policy_repo = RetentionPolicyRepository(database)
    hold_repo = LegalHoldRepository(database)
    log_repo = RetentionAuditLogRepository(database)
    artifact_store = FakeArtifactStore()
    service = RetentionErasureService(
        audit_repository=audit_repo,
        run_repository=run_repo,
        policy_repository=policy_repo,
        legal_hold_repository=hold_repo,
        log_repository=log_repo,
        artifact_store=artifact_store,
        econ_eraser=None,
    )
    return RetentionEnv(
        database=database,
        signer=signer,
        audit_repo=audit_repo,
        run_repo=run_repo,
        policy_repo=policy_repo,
        hold_repo=hold_repo,
        log_repo=log_repo,
        artifact_store=artifact_store,
        service=service,
    )


@pytest.fixture
async def env(sqlite_db) -> RetentionEnv:
    """A fully wired RetentionEnv over the migrated test database."""
    return _build_env(sqlite_db)


@pytest.fixture
async def encrypted_env(tmp_path):
    """A RetentionEnv over an at-rest-encrypted DB (encryption_key set), so
    erasure exercises the decrypt-before-parse path (audit F1 part b)."""
    from zeroth.core.service.bootstrap import run_migrations
    from zeroth.platform.storage.async_sqlite import AsyncSQLiteDatabase
    from zeroth.platform.storage.sqlite import EncryptedField

    db_path = str(tmp_path / "encrypted.db")
    run_migrations(f"sqlite:///{db_path}")
    database = AsyncSQLiteDatabase(path=db_path, encryption_key=EncryptedField.generate_key())
    try:
        yield _build_env(database)
    finally:
        await database.close()
