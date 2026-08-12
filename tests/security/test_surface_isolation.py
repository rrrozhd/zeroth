"""Separately addressable tenant-isolation nodes for shared runtime surfaces."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

import pytest

from zeroth.governance.approvals.models import ApprovalDecision, ApprovalRecord
from zeroth.governance.approvals.repository import ApprovalRepository
from zeroth.governance.approvals.service import ApprovalService
from zeroth.governance.audit import AuditRepository, NodeAuditRecord
from zeroth.governance.audit.models import AuditQuery
from zeroth.governance.identity import ActorIdentity, AuthMethod
from zeroth.governance.guardrails.rate_limit import QuotaEnforcer, TokenBucketRateLimiter
from zeroth.integrations.execution.models import (
    CommandArtifactSource,
    ExecutionMode,
    InputMode,
    OutputMode,
    RunConfig,
    WrappedCommandUnitManifest,
)
from zeroth.integrations.execution.runner import (
    ExecutableUnitBinding,
    ExecutableUnitRegistry,
    ExecutableUnitRunner,
)
from zeroth.integrations.execution.sandbox import SandboxManager
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.platform.storage import NullWorkspaceScopeContext, ScopeContext


class _Input(BaseModel):
    value: str


class _Output(BaseModel):
    value: str


def _audit(audit_id: str, tenant_id: str, *, workspace_id: str | None = None) -> NodeAuditRecord:
    return NodeAuditRecord(
        audit_id=audit_id,
        run_id=f"run-{tenant_id}",
        node_id="node",
        graph_version_ref="graph:v1",
        deployment_ref="deployment",
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        status="completed",
        started_at=datetime(2026, 8, 9, tzinfo=UTC),
        completed_at=datetime(2026, 8, 9, 0, 0, 1, tzinfo=UTC),
    )


async def test_cache_same_manifest_isolated_by_enforced_tenant_identity(tmp_path: Path) -> None:
    script = tmp_path / "unit.py"
    script.write_text(
        "import json, sys\nprint(json.dumps(json.load(sys.stdin)))\n",
        encoding="utf-8",
    )
    manifest = WrappedCommandUnitManifest(
        unit_id="tenant-cache",
        onboarding_mode=ExecutionMode.WRAPPED_COMMAND,
        runtime="command",
        artifact_source=CommandArtifactSource(ref=str(script)),
        entrypoint_type="command",
        input_mode=InputMode.JSON_STDIN,
        output_mode=OutputMode.JSON_STDOUT,
        input_contract_ref="contract://input",
        output_contract_ref="contract://output",
        run_config=RunConfig(command=[sys.executable, str(script)]),
        cache_identity_fields={"source": "same-manifest"},
    )
    registry = ExecutableUnitRegistry()
    registry.register(
        ExecutableUnitBinding(
            manifest_ref="eu://tenant-cache",
            manifest=manifest,
            input_model=_Input,
            output_model=_Output,
        )
    )
    manager = SandboxManager(base_env={})
    runner = ExecutableUnitRunner(registry, sandbox_manager=manager)

    first = await runner.run_manifest_ref(
        "eu://tenant-cache",
        {"value": "same-subject"},
        enforcement_context={"tenant_id": "tenant-a", "workspace_id": "workspace"},
    )
    second = await runner.run_manifest_ref(
        "eu://tenant-cache",
        {"value": "same-subject"},
        enforcement_context={"tenant_id": "tenant-b", "workspace_id": "workspace"},
    )

    assert first.audit_record["cache_key"] != second.audit_record["cache_key"]
    cached = manager.cache_manager.snapshot()
    assert first.audit_record["cache_key"] in cached
    assert second.audit_record["cache_key"] in cached


async def test_rate_limit_same_subject_isolated_by_tenant_key(sqlite_db) -> None:
    limiter = TokenBucketRateLimiter(sqlite_db)
    tenant_a = "tenant:tenant-a:subject:same-subject"
    tenant_b = "tenant:tenant-b:subject:same-subject"

    assert await limiter.check_and_consume(tenant_a, capacity=1.0, refill_rate=0.0) is True
    assert await limiter.check_and_consume(tenant_a, capacity=1.0, refill_rate=0.0) is False
    assert await limiter.check_and_consume(tenant_b, capacity=1.0, refill_rate=0.0) is True


async def test_quota_same_subject_isolated_by_tenant_key(sqlite_db) -> None:
    enforcer = QuotaEnforcer(sqlite_db)
    tenant_a = "tenant:tenant-a:subject:same-subject"
    tenant_b = "tenant:tenant-b:subject:same-subject"

    assert await enforcer.check_and_increment(tenant_a, limit=1) is True
    assert await enforcer.check_and_increment(tenant_a, limit=1) is False
    assert await enforcer.check_and_increment(tenant_b, limit=1) is True


async def test_audit_retrieve_foreign_id_matches_unknown(sqlite_db) -> None:
    owner_repository = AuditRepository.scoped(
        sqlite_db, NullWorkspaceScopeContext(tenant_id="tenant-a")
    )
    foreign_repository = AuditRepository.scoped(
        sqlite_db, NullWorkspaceScopeContext(tenant_id="tenant-b")
    )
    await owner_repository.write(_audit("owner-audit", "tenant-a"))

    foreign = await foreign_repository.get("owner-audit")
    unknown = await foreign_repository.get("unknown-audit")

    assert foreign is unknown is None


async def test_approval_retrieve_foreign_matches_unknown(sqlite_db) -> None:
    repository = ApprovalRepository(sqlite_db)
    await repository.write(
        ApprovalRecord(
            approval_id="owner-approval",
            run_id="owner-run",
            node_id="node",
            graph_version_ref="graph:v1",
            deployment_ref="deployment",
            tenant_id="tenant-a",
            workspace_id="workspace-a",
            summary="owner only",
            rationale="owner only",
        )
    )

    foreign = await repository.get("owner-approval", tenant_id="tenant-b")
    unknown = await repository.get("unknown-approval", tenant_id="tenant-b")
    assert foreign is unknown is None


async def test_approval_enumerate_is_query_scoped_by_tenant_and_workspace(sqlite_db) -> None:
    repository = ApprovalRepository(sqlite_db)
    await repository.write(
        ApprovalRecord(
            approval_id="owner-approval-list",
            run_id="owner-run",
            node_id="node",
            graph_version_ref="graph:v1",
            deployment_ref="deployment",
            tenant_id="tenant-a",
            workspace_id="workspace-a",
            summary="owner only",
            rationale="owner only",
        )
    )

    foreign = await repository.list_pending(
        tenant_id="tenant-a",
        workspace_id="workspace-b",
        deployment_ref="deployment",
        graph_version_ref="graph:v1",
    )
    unknown = await repository.list_pending(
        tenant_id="tenant-a",
        workspace_id="workspace-unknown",
        deployment_ref="deployment",
        graph_version_ref="graph:v1",
    )

    assert foreign == unknown == []


async def test_approval_resolve_foreign_matches_unknown_before_mutation(sqlite_db) -> None:
    repository = ApprovalRepository(sqlite_db)
    await repository.write(
        ApprovalRecord(
            approval_id="owner-approval-resolve",
            run_id="owner-run",
            node_id="node",
            graph_version_ref="graph:v1",
            deployment_ref="deployment",
            tenant_id="tenant-a",
            workspace_id="workspace-a",
            summary="owner only",
            rationale="owner only",
        )
    )
    service = ApprovalService(
        repository=repository, run_repository=RunRepository.for_default_compatibility(sqlite_db)
    )
    actor = ActorIdentity(subject="reviewer", auth_method=AuthMethod.API_KEY)

    for approval_id in ("owner-approval-resolve", "unknown-approval"):
        with pytest.raises(KeyError):
            await service.resolve(
                approval_id,
                decision=ApprovalDecision.APPROVE,
                actor=actor,
                tenant_id="tenant-a",
                workspace_id="workspace-b",
                deployment_ref="deployment",
                graph_version_ref="graph:v1",
            )
    owner = await repository.get("owner-approval-resolve", tenant_id="tenant-a")
    assert owner is not None and owner.status.value == "pending"


async def test_audit_enumerate_is_query_scoped_by_tenant_and_workspace(sqlite_db) -> None:
    owner_repository = AuditRepository.scoped(
        sqlite_db, ScopeContext(tenant_id="tenant-a", workspace_id="workspace-a")
    )
    foreign_repository = AuditRepository.scoped(
        sqlite_db, ScopeContext(tenant_id="tenant-a", workspace_id="workspace-b")
    )
    await owner_repository.write(
        _audit("owner-workspace-audit", "tenant-a", workspace_id="workspace-a")
    )

    foreign = await foreign_repository.list(
        AuditQuery(
            run_id="run-tenant-a",
            tenant_id="tenant-a",
            workspace_id="workspace-b",
            deployment_ref="deployment",
        )
    )
    unknown = await foreign_repository.list(
        AuditQuery(
            run_id="unknown-run",
            tenant_id="tenant-a",
            workspace_id="workspace-b",
            deployment_ref="deployment",
        )
    )

    assert foreign == unknown == []
