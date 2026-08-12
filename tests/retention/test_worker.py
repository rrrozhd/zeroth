"""WS-E: the retention purge worker mirrors the SLA-checker resilience pattern."""

from __future__ import annotations

import asyncio
import pytest

from zeroth.governance.retention.models import RetentionPolicy
from zeroth.governance.retention.worker import RetentionPurgeWorker
from zeroth.governance.retention.policy_repository import RetentionPolicyRepository
from zeroth.platform.storage import (
    SERVICE_SCOPE_REGISTRY,
    CrossTenantMaintenanceScopeContext,
    NullWorkspaceScopeContext,
    ScopedTable,
)


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


async def test_real_policy_repository_scheduler_fans_out_to_tenant_bound_services(
    async_database,
) -> None:
    services: dict[str, _FlakyErasureService] = {}
    for tenant_id in ("tenant-a", "tenant-b"):
        repository = RetentionPolicyRepository(
            async_database, NullWorkspaceScopeContext(tenant_id=tenant_id)
        )
        await repository.upsert(RetentionPolicy(tenant_id=tenant_id, run_ttl_seconds=1))

    def service_for(tenant_id: str) -> _FlakyErasureService:
        return services.setdefault(tenant_id, _FlakyErasureService("none"))

    worker = RetentionPurgeWorker(
        database=async_database,
        erasure_service_factory=service_for,  # type: ignore[arg-type]
    )
    await worker.sweep_once()

    assert set(services) >= {"tenant-a", "tenant-b"}
    assert services["tenant-a"].purged == ["tenant-a"]
    assert services["tenant-b"].purged == ["tenant-b"]


async def test_cross_tenant_policy_enumeration_requires_maintenance_factory(
    async_database,
) -> None:
    repository = RetentionPolicyRepository(
        async_database, NullWorkspaceScopeContext(tenant_id="tenant-a")
    )

    with pytest.raises(PermissionError, match="maintenance scope required"):
        await repository.list_all_enabled_for_maintenance()


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
    worker_a = RetentionPurgeWorker(
        database=async_database,
        erasure_service_factory=lambda _tenant_id: service_a,  # type: ignore[arg-type]
    )
    worker_b = RetentionPurgeWorker(
        database=async_database,
        erasure_service_factory=lambda _tenant_id: service_b,  # type: ignore[arg-type]
    )

    await worker_a.sweep_once()

    assert worker_b.erasure_service_factory("tenant-a") is service_b
    assert "tenant-a" in service_a.purged
    assert "tenant-a" in service_a.audit_swept
    assert service_b.purged == []
    assert service_b.audit_swept == []


async def test_worker_applies_bound_repository_default_policy(async_database) -> None:
    service = _FlakyErasureService("none")
    await _seed_policy(
        async_database, RetentionPolicy(tenant_id="tenant-default", run_ttl_seconds=123)
    )
    worker = RetentionPurgeWorker(
        database=async_database,
        erasure_service_factory=lambda _tenant_id: service,  # type: ignore[arg-type]
    )

    await worker.sweep_once()

    assert "tenant-default" in service.purged
    assert "tenant-default" in service.audit_swept


async def test_worker_skips_a_disabled_bound_policy(async_database) -> None:
    services: dict[str, _FlakyErasureService] = {}
    await _seed_policy(async_database, RetentionPolicy(tenant_id="tenant-disabled", enabled=False))
    worker = RetentionPurgeWorker(
        database=async_database,
        erasure_service_factory=lambda tenant_id: services.setdefault(
            tenant_id, _FlakyErasureService("none")
        ),  # type: ignore[arg-type]
    )

    await worker.sweep_once()

    assert "tenant-disabled" not in services


async def test_worker_survives_bound_tenant_failure(async_database) -> None:
    service = _FlakyErasureService(failing_tenant="tenant-boom")
    await _seed_policy(async_database, RetentionPolicy(tenant_id="tenant-boom"))
    worker = RetentionPurgeWorker(
        database=async_database,
        erasure_service_factory=lambda _tenant_id: service,  # type: ignore[arg-type]
        poll_interval=0.01,
    )

    task = asyncio.create_task(worker.poll_loop())
    await asyncio.sleep(0.03)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "tenant-boom" not in service.purged


async def test_worker_cancellation_propagates(async_database) -> None:
    """CancelledError from the sleep must re-raise, not get swallowed."""
    worker = RetentionPurgeWorker(
        database=async_database,
        erasure_service_factory=lambda _tenant_id: _FlakyErasureService("none"),  # type: ignore[arg-type]
        poll_interval=10.0,
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
    worker = RetentionPurgeWorker(
        database=async_database,
        erasure_service_factory=lambda _tenant_id: service,  # type: ignore[arg-type]
        poll_interval=0.01,
    )

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
