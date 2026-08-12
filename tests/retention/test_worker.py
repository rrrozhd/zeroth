"""WS-E: the retention purge worker mirrors the SLA-checker resilience pattern."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import pytest

from tests.conftest import content_capture
from tests.retention.conftest import make_audit_record
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
from zeroth.governance.retention.workspace_reader import RetentionWorkspaceMaintenanceReader
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.platform.storage import (
    SERVICE_SCOPE_REGISTRY,
    CrossTenantMaintenanceScopeContext,
    NullWorkspaceScopeContext,
    ScopeContext,
    ScopedTable,
)
from zeroth.runtime.runs import Run


class _FlakyErasureService:
    """Both sweep surfaces raise for one tenant, succeed for the rest."""

    def __init__(self, failing_tenant: str) -> None:
        self.failing_tenant = failing_tenant
        self.purged: list[str] = []
        self.audit_swept: list[str] = []

    async def purge_runs(self, tenant_id: str) -> list:
        if tenant_id == self.failing_tenant:
            raise RuntimeError("boom")
        self.purged.append(tenant_id)
        return []

    async def purge_audits(self, tenant_id: str) -> list:
        if tenant_id == self.failing_tenant:
            raise RuntimeError("boom")
        self.audit_swept.append(tenant_id)
        return []


class _NoWorkspaces:
    async def list_workspace_ids(self) -> list[str]:
        return []


def _worker(database, factory, *, poll_interval: float = 3600.0) -> RetentionPurgeWorker:
    return RetentionPurgeWorker(
        policy_reader=EnabledPolicyMaintenanceReader(database),
        workspace_reader_factory=lambda _tenant_id: _NoWorkspaces(),
        erasure_service_factory=factory,
        poll_interval=poll_interval,
    )


async def test_real_policy_repository_scheduler_fans_out_to_tenant_bound_services(
    async_database,
) -> None:
    services: dict[str, _FlakyErasureService] = {}
    for tenant_id in ("tenant-a", "tenant-b"):
        repository = RetentionPolicyRepository(
            async_database, NullWorkspaceScopeContext(tenant_id=tenant_id)
        )
        await repository.upsert(RetentionPolicy(tenant_id=tenant_id, run_ttl_seconds=1))

    def service_for(scope) -> _FlakyErasureService:
        return services.setdefault(scope.tenant_id, _FlakyErasureService("none"))

    worker = _worker(async_database, service_for)
    await worker.sweep_once()

    assert set(services) >= {"tenant-a", "tenant-b"}
    assert services["tenant-a"].purged == ["tenant-a"]
    assert services["tenant-b"].purged == ["tenant-b"]


def test_normal_policy_repository_has_no_maintenance_enumeration(async_database) -> None:
    repository = RetentionPolicyRepository(
        async_database, NullWorkspaceScopeContext(tenant_id="tenant-a")
    )
    assert not hasattr(repository, "list_all_enabled_for_maintenance")


def test_maintenance_policy_reader_has_no_ordinary_repository_surface(async_database) -> None:
    reader = EnabledPolicyMaintenanceReader(async_database)
    for method_name in ("get", "resolve", "upsert", "list_for_tenant"):
        assert not hasattr(reader, method_name)


async def test_workspace_reader_discovers_named_scopes_only(async_database) -> None:
    repository = RunRepository(
        async_database, ScopeContext(tenant_id="tenant-a", workspace_id="workspace-a")
    )
    await repository.create(
        Run(
            run_id="workspace-discovery-run",
            graph_version_ref="graph:v1",
            deployment_ref="deployment:v1",
            tenant_id="tenant-a",
            workspace_id="workspace-a",
        )
    )
    assert await RetentionWorkspaceMaintenanceReader(
        async_database, "tenant-a"
    ).list_workspace_ids() == ["workspace-a"]


async def test_scheduler_fans_out_null_and_discovered_named_workspace_scopes(
    async_database,
) -> None:
    await _seed_policy(async_database, RetentionPolicy(tenant_id="tenant-a", run_ttl_seconds=1))
    repository = RunRepository(
        async_database, ScopeContext(tenant_id="tenant-a", workspace_id="workspace-a")
    )
    await repository.create(
        Run(
            run_id="workspace-fanout-run",
            graph_version_ref="graph:v1",
            deployment_ref="deployment:v1",
            tenant_id="tenant-a",
            workspace_id="workspace-a",
        )
    )
    scopes = []
    service = _FlakyErasureService("none")
    worker = RetentionPurgeWorker(
        policy_reader=EnabledPolicyMaintenanceReader(async_database),
        workspace_reader_factory=lambda tenant_id: RetentionWorkspaceMaintenanceReader(
            async_database, tenant_id
        ),
        erasure_service_factory=lambda scope: (scopes.append(scope), service)[1],
    )

    await worker.sweep_once()

    tenant_scopes = [scope for scope in scopes if scope.tenant_id == "tenant-a"]
    assert any(type(scope) is NullWorkspaceScopeContext for scope in tenant_scopes)
    assert any(
        type(scope) is ScopeContext and scope.workspace_id == "workspace-a"
        for scope in tenant_scopes
    )


async def test_scheduler_real_service_erases_stale_named_workspace_run(env) -> None:
    tenant_id = "tenant-workspace"
    workspace_id = "workspace-a"
    scope = ScopeContext(tenant_id=tenant_id, workspace_id=workspace_id)
    run_repository = RunRepository(env.database, scope)
    run = Run(
        run_id="named-stale-run",
        graph_version_ref="graph:v1",
        deployment_ref="deployment:v1",
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        final_output={"secret": "named-pii"},
    )
    await run_repository.create(run)
    audit_repository = content_capture(AuditRepository.scoped(env.database, scope, env.signer))
    await audit_repository.write(
        make_audit_record(
            audit_id="named-stale-audit",
            run_id=run.run_id,
            tenant_id=tenant_id,
            ssn="named-pii",
        ).model_copy(update={"workspace_id": workspace_id})
    )
    old = datetime.now(UTC) - timedelta(days=2)
    async with env.database.transaction() as connection:
        await connection.execute(
            "UPDATE runs SET status = ?, updated_at = ? WHERE tenant_id = ? AND workspace_id = ? AND run_id = ?",
            ("COMPLETED", old.isoformat(), tenant_id, workspace_id, run.run_id),
        )
    await _seed_policy(
        env.database,
        RetentionPolicy(tenant_id=tenant_id, run_ttl_seconds=1, audit_ttl_seconds=1),
    )

    null_repository = RunRepository(env.database, NullWorkspaceScopeContext(tenant_id=tenant_id))
    await null_repository.create(
        Run(
            run_id="null-stale-run",
            graph_version_ref="graph:v1",
            deployment_ref="deployment:v1",
            tenant_id=tenant_id,
            final_output={"secret": "null-pii"},
        )
    )
    tenant_b_scope = ScopeContext(tenant_id="tenant-b", workspace_id="workspace-b")
    tenant_b_repository = RunRepository(env.database, tenant_b_scope)
    await tenant_b_repository.create(
        Run(
            run_id="tenant-b-recent-run",
            graph_version_ref="graph:v1",
            deployment_ref="deployment:v1",
            tenant_id="tenant-b",
            workspace_id="workspace-b",
            final_output={"secret": "tenant-b-pii"},
        )
    )
    await _seed_policy(env.database, RetentionPolicy(tenant_id="tenant-b", run_ttl_seconds=1))
    async with env.database.transaction() as connection:
        await connection.execute(
            "UPDATE runs SET status = ?, updated_at = ? WHERE tenant_id = ? AND workspace_id IS NULL AND run_id = ?",
            ("COMPLETED", old.isoformat(), tenant_id, "null-stale-run"),
        )

    def service_for(bound_scope):
        tenant_scope = (
            NullWorkspaceScopeContext.for_default_compatibility()
            if bound_scope.tenant_id == "default"
            else NullWorkspaceScopeContext(tenant_id=bound_scope.tenant_id)
        )
        return RetentionErasureService(
            audit_repository=AuditRepository.scoped(env.database, bound_scope, env.signer),
            run_repository=RunRepository(env.database, bound_scope),
            policy_repository=RetentionPolicyRepository(env.database, tenant_scope),
            legal_hold_repository=LegalHoldRepository(env.database, tenant_scope),
            log_repository=RetentionAuditLogRepository(env.database, tenant_scope),
            artifact_store=env.artifact_store,
        )

    worker = RetentionPurgeWorker(
        policy_reader=EnabledPolicyMaintenanceReader(env.database),
        workspace_reader_factory=lambda tenant: RetentionWorkspaceMaintenanceReader(
            env.database, tenant
        ),
        erasure_service_factory=service_for,
    )
    await worker.sweep_once()

    stored = await run_repository.get(run.run_id)
    assert "named-pii" not in str(stored.final_output)
    assert "null-pii" not in str((await null_repository.get("null-stale-run")).final_output)
    assert "tenant-b-pii" in str(
        (await tenant_b_repository.get("tenant-b-recent-run")).final_output
    )
    records = await audit_repository.list_by_run(run.run_id)
    assert records and records[0].erased


def test_cross_tenant_maintenance_capability_cannot_bind_other_resources(
    async_database,
) -> None:
    with pytest.raises(ValueError, match="limited to retention policies"):
        ScopedTable.for_cross_tenant_maintenance(
            async_database,
            SERVICE_SCOPE_REGISTRY,
            "service.webhook_deliveries",
            CrossTenantMaintenanceScopeContext.for_scheduled_maintenance(),
        )


async def _seed_policy(database, policy: RetentionPolicy) -> None:
    repository = RetentionPolicyRepository(
        database, NullWorkspaceScopeContext(tenant_id=policy.tenant_id)
    )
    await repository.upsert(policy)


async def test_worker_is_bound_to_one_deployment_owner_scope(async_database) -> None:
    service_a = _FlakyErasureService("none")
    service_b = _FlakyErasureService("none")
    policy = RetentionPolicy(tenant_id="tenant-a", run_ttl_seconds=1)
    await _seed_policy(async_database, policy)
    worker_a = _worker(async_database, lambda _scope: service_a)
    worker_b = _worker(async_database, lambda _scope: service_b)

    await worker_a.sweep_once()

    assert (
        worker_b.erasure_service_factory(NullWorkspaceScopeContext(tenant_id="tenant-a"))
        is service_b
    )
    assert "tenant-a" in service_a.purged
    assert "tenant-a" in service_a.audit_swept
    assert service_b.purged == []
    assert service_b.audit_swept == []


async def test_worker_applies_bound_repository_default_policy(async_database) -> None:
    service = _FlakyErasureService("none")
    await _seed_policy(
        async_database, RetentionPolicy(tenant_id="tenant-default", run_ttl_seconds=123)
    )
    worker = _worker(async_database, lambda _scope: service)

    await worker.sweep_once()

    assert "tenant-default" in service.purged
    assert "tenant-default" in service.audit_swept


async def test_worker_skips_a_disabled_bound_policy(async_database) -> None:
    services: dict[str, _FlakyErasureService] = {}
    await _seed_policy(async_database, RetentionPolicy(tenant_id="tenant-disabled", enabled=False))
    worker = _worker(
        async_database,
        lambda scope: services.setdefault(scope.tenant_id, _FlakyErasureService("none")),
    )

    await worker.sweep_once()

    assert "tenant-disabled" not in services


async def test_worker_survives_bound_tenant_failure(async_database) -> None:
    service = _FlakyErasureService(failing_tenant="tenant-boom")
    await _seed_policy(async_database, RetentionPolicy(tenant_id="tenant-boom"))
    worker = _worker(async_database, lambda _scope: service, poll_interval=0.01)

    task = asyncio.create_task(worker.poll_loop())
    await asyncio.sleep(0.03)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "tenant-boom" not in service.purged


async def test_worker_cancellation_propagates(async_database) -> None:
    """CancelledError from the sleep must re-raise, not get swallowed."""
    worker = _worker(
        async_database, lambda _scope: _FlakyErasureService("none"), poll_interval=10.0
    )
    task = asyncio.create_task(worker.poll_loop())
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


class _HalfFailingService:
    """purge_runs raises for one tenant; purge_audits always succeeds."""

    def __init__(self, fail_runs_for: str) -> None:
        self.fail_runs_for = fail_runs_for
        self.run_swept: list[str] = []
        self.audit_swept: list[str] = []

    async def purge_runs(self, tenant_id: str) -> list:
        if tenant_id == self.fail_runs_for:
            raise RuntimeError("runs boom")
        self.run_swept.append(tenant_id)
        return []

    async def purge_audits(self, tenant_id: str) -> list:
        self.audit_swept.append(tenant_id)
        return []


async def test_worker_sweeps_surfaces_independently(async_database) -> None:
    """A failing run sweep must not starve the same tenant's audit sweep."""
    service = _HalfFailingService(fail_runs_for="tenant-a")
    await _seed_policy(async_database, RetentionPolicy(tenant_id="tenant-a"))
    worker = _worker(async_database, lambda _scope: service, poll_interval=0.01)

    task = asyncio.create_task(worker.poll_loop())
    for _ in range(200):
        if "tenant-a" in service.audit_swept:
            break
        await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "tenant-a" in service.audit_swept  # audit sweep survived the runs failure
    assert "tenant-a" not in service.run_swept
