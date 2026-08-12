from __future__ import annotations

import asyncio

import pytest

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


class _RecordingService:
    def __init__(self, calls: list[tuple[str, str, str | None]]) -> None:
        self._calls = calls

    async def purge_runs(self, tenant_id: str) -> list[object]:
        self._calls.append(("runs", tenant_id, None))
        return []

    async def purge_audits(self, tenant_id: str) -> list[object]:
        self._calls.append(("audits", tenant_id, None))
        return []


async def _seed_policy(database, tenant_id: str) -> None:
    repository = RetentionPolicyRepository.scoped(
        database, NullWorkspaceScopeContext(tenant_id=tenant_id)
    )
    await repository.upsert(RetentionPolicy(tenant_id=tenant_id, run_ttl_seconds=1))


async def test_shared_scheduler_fans_out_across_tenants_and_named_workspaces(
    async_database,
) -> None:
    for tenant_id in ("tenant-a", "tenant-b"):
        await _seed_policy(async_database, tenant_id)
    await RunRepository(
        async_database, ScopeContext(tenant_id="tenant-a", workspace_id="workspace-a")
    ).create(
        Run(
            run_id="named-run",
            graph_version_ref="graph:v1",
            deployment_ref="deployment:v1",
            tenant_id="tenant-a",
            workspace_id="workspace-a",
        )
    )
    scopes: list[tuple[str, str | None]] = []
    calls: list[tuple[str, str, str | None]] = []

    def service_for(scope):
        scopes.append((scope.tenant_id, getattr(scope, "workspace_id", None)))
        return _RecordingService(calls)

    worker = RetentionPurgeWorker.for_shared_database(
        policy_reader=EnabledPolicyMaintenanceReader(async_database),
        workspace_reader_factory=lambda tenant_id: RetentionWorkspaceMaintenanceReader(
            async_database, tenant_id
        ),
        erasure_service_factory=service_for,
    )
    await worker.sweep_once()

    assert ("tenant-a", None) in scopes
    assert ("tenant-a", "workspace-a") in scopes
    assert ("tenant-b", None) in scopes
    assert {tenant_id for _, tenant_id, _ in calls} >= {"tenant-a", "tenant-b"}


class _PolicyReader:
    async def list_all_enabled_for_maintenance(self) -> list[RetentionPolicy]:
        return [RetentionPolicy(tenant_id="tenant-a"), RetentionPolicy(tenant_id="tenant-b")]


class _WorkspaceReader:
    def __init__(self, tenant_id: str, *, fail: bool = False, cancel: bool = False) -> None:
        self._tenant_id = tenant_id
        self._fail = fail
        self._cancel = cancel

    async def list_workspace_ids(self) -> list[str]:
        if self._cancel:
            raise asyncio.CancelledError
        if self._fail:
            raise RuntimeError("workspace discovery failed")
        return [f"workspace-{self._tenant_id}"]


async def test_discovery_failure_for_one_tenant_does_not_starve_the_next() -> None:
    scopes: list[tuple[str, str | None]] = []
    worker = RetentionPurgeWorker.for_shared_database(
        policy_reader=_PolicyReader(),  # type: ignore[arg-type]
        workspace_reader_factory=lambda tenant_id: _WorkspaceReader(
            tenant_id, fail=tenant_id == "tenant-a"
        ),
        erasure_service_factory=lambda scope: (
            scopes.append((scope.tenant_id, getattr(scope, "workspace_id", None))),
            _RecordingService([]),
        )[1],
    )

    await worker.sweep_once()

    assert scopes == [("tenant-b", None), ("tenant-b", "workspace-tenant-b")]


async def test_factory_failure_for_one_scope_does_not_starve_later_scopes() -> None:
    scopes: list[tuple[str, str | None]] = []

    def service_for(scope):
        identity = (scope.tenant_id, getattr(scope, "workspace_id", None))
        if identity == ("tenant-a", None):
            raise RuntimeError("factory failed")
        scopes.append(identity)
        return _RecordingService([])

    worker = RetentionPurgeWorker.for_shared_database(
        policy_reader=_PolicyReader(),  # type: ignore[arg-type]
        workspace_reader_factory=lambda tenant_id: _WorkspaceReader(tenant_id),
        erasure_service_factory=service_for,
    )

    await worker.sweep_once()

    assert ("tenant-a", "workspace-tenant-a") in scopes
    assert ("tenant-b", None) in scopes


async def test_discovery_cancellation_propagates() -> None:
    worker = RetentionPurgeWorker.for_shared_database(
        policy_reader=_PolicyReader(),  # type: ignore[arg-type]
        workspace_reader_factory=lambda tenant_id: _WorkspaceReader(tenant_id, cancel=True),
        erasure_service_factory=lambda _scope: _RecordingService([]),
    )

    with pytest.raises(asyncio.CancelledError):
        await worker.sweep_once()


async def test_cross_tenant_reader_authority_rejects_mutation_and_other_resources(
    async_database,
) -> None:
    context = CrossTenantMaintenanceScopeContext.for_scheduled_maintenance()
    policies = ScopedTable.for_cross_tenant_maintenance(
        async_database, SERVICE_SCOPE_REGISTRY, "service.retention_policies", context
    )
    async with policies.transaction() as rows:
        with pytest.raises(ValueError, match="read-only"):
            await rows.insert({"tenant_id": "tenant-x"})
    with pytest.raises(ValueError, match="limited to approved resources"):
        ScopedTable.for_cross_tenant_maintenance(
            async_database, SERVICE_SCOPE_REGISTRY, "service.runs", context
        )
