from __future__ import annotations

import pytest

from zeroth.governance.retention.models import RetentionPolicy
from zeroth.governance.retention.policy_repository import RetentionPolicyRepository
from zeroth.governance.retention.worker import RetentionPurgeWorker
from zeroth.platform.storage import NullWorkspaceScopeContext


class _RecordingErasureService:
    def __init__(self) -> None:
        self.run_tenants: list[str] = []
        self.audit_tenants: list[str] = []

    async def purge_runs(self, tenant_id: str) -> list[object]:
        self.run_tenants.append(tenant_id)
        return []

    async def purge_audits(self, tenant_id: str) -> list[object]:
        self.audit_tenants.append(tenant_id)
        return []


async def test_legacy_policy_repository_is_bound_to_reserved_default_scope(
    async_database,
) -> None:
    compatibility = RetentionPolicyRepository(async_database)
    named = RetentionPolicyRepository.scoped(
        async_database,
        NullWorkspaceScopeContext(tenant_id="tenant-a"),
    )
    await named.upsert(RetentionPolicy(tenant_id="tenant-a", run_ttl_seconds=11))

    assert compatibility.tenant_id == "default"
    compatibility_policy = await compatibility.get()
    assert compatibility_policy is not None
    assert compatibility_policy.tenant_id == "default"
    named_policy = await named.get()
    assert named_policy is not None
    assert named_policy.run_ttl_seconds == 11


async def test_legacy_policy_repository_cannot_write_named_tenant(async_database) -> None:
    compatibility = RetentionPolicyRepository(async_database)

    with pytest.raises(ValueError, match="tenant_id does not match bound scope"):
        await compatibility.upsert(RetentionPolicy(tenant_id="tenant-a"))


async def test_legacy_worker_derives_named_owner_from_bound_repository(async_database) -> None:
    repository = RetentionPolicyRepository.scoped(
        async_database,
        NullWorkspaceScopeContext(tenant_id="tenant-a"),
    )
    await repository.upsert(RetentionPolicy(tenant_id="tenant-a", run_ttl_seconds=1))
    service = _RecordingErasureService()
    worker = RetentionPurgeWorker(service, repository)  # type: ignore[arg-type]

    await worker.sweep_once()

    assert service.run_tenants == ["tenant-a"]
    assert service.audit_tenants == ["tenant-a"]
