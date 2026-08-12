from __future__ import annotations

from datetime import UTC, datetime, timedelta

from zeroth.governance.audit import AuditRepository
from zeroth.governance.retention import (
    LegalHoldRepository,
    RetentionAuditLogRepository,
    RetentionErasureService,
)
from zeroth.governance.retention.models import RetentionPolicy
from zeroth.governance.retention.policy_repository import (
    EnabledPolicyMaintenanceReader,
    RetentionPolicyRepository,
)
from zeroth.governance.retention.worker import RetentionPurgeWorker
from zeroth.governance.retention.workspace_reader import (
    RetentionOwnerMaintenanceReader,
    RetentionWorkspaceMaintenanceReader,
)
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.platform.storage import NullWorkspaceScopeContext, ScopeContext
from zeroth.runtime.runs import Run


class _Service:
    def __init__(self, scope, calls) -> None:
        self.scope = scope
        self.calls = calls

    async def purge_runs(self, tenant_id: str) -> list[object]:
        self.calls.append((tenant_id, getattr(self.scope, "workspace_id", None)))
        return []

    async def purge_audits(self, tenant_id: str) -> list[object]:
        return []


async def test_fallback_policy_erases_owner_without_explicit_row_and_preserves_disabled(
    env,
) -> None:
    database = env.database
    for tenant_id in ("tenant-fallback", "tenant-disabled", "tenant-foreign"):
        await RunRepository(
            database,
            ScopeContext(tenant_id=tenant_id, workspace_id="workspace-a"),
        ).create(
            Run(
                run_id=f"run-{tenant_id}",
                graph_version_ref="graph:v1",
                deployment_ref="deployment:v1",
                tenant_id=tenant_id,
                workspace_id="workspace-a",
                status="COMPLETED",
                final_output={"secret": tenant_id},
            )
        )
    old = datetime.now(UTC) - timedelta(days=2)
    async with database.transaction() as connection:
        await connection.execute(
            "UPDATE runs SET updated_at = ? WHERE run_id LIKE ?",
            (old.isoformat(), "run-tenant-%"),
        )
    disabled = RetentionPolicyRepository.scoped(
        database,
        NullWorkspaceScopeContext(tenant_id="tenant-disabled"),
    )
    await disabled.upsert(RetentionPolicy(tenant_id="tenant-disabled", enabled=False))
    default = RetentionPolicy(tenant_id="default", run_ttl_seconds=1)

    def policy_for(tenant_id: str) -> RetentionPolicyRepository:
        return RetentionPolicyRepository.scoped(
            database,
            NullWorkspaceScopeContext(tenant_id=tenant_id),
            default_policy=default,
        )

    def service_for(scope) -> RetentionErasureService:
        tenant_id = scope.tenant_id
        null_scope = NullWorkspaceScopeContext(tenant_id=tenant_id)
        return RetentionErasureService(
            audit_repository=AuditRepository.scoped(database, scope, env.signer),
            run_repository=RunRepository(database, scope),
            policy_repository=policy_for(tenant_id),
            legal_hold_repository=LegalHoldRepository(database, null_scope),
            log_repository=RetentionAuditLogRepository(database, null_scope),
            artifact_store=env.artifact_store,
        )

    worker = RetentionPurgeWorker.for_shared_database(
        policy_reader=EnabledPolicyMaintenanceReader(database),
        tenant_reader=RetentionOwnerMaintenanceReader(database),
        policy_repository_factory=policy_for,
        workspace_reader_factory=lambda tenant_id: RetentionWorkspaceMaintenanceReader(
            database, tenant_id
        ),
        erasure_service_factory=service_for,
    )

    await worker.sweep_once()

    fallback_repository = RunRepository(
        database, ScopeContext(tenant_id="tenant-fallback", workspace_id="workspace-a")
    )
    disabled_repository = RunRepository(
        database, ScopeContext(tenant_id="tenant-disabled", workspace_id="workspace-a")
    )
    fallback = await fallback_repository.get("run-tenant-fallback")
    disabled_run = await disabled_repository.get("run-tenant-disabled")
    assert "tenant-fallback" not in str(fallback.final_output)
    assert "tenant-disabled" in str(disabled_run.final_output)
