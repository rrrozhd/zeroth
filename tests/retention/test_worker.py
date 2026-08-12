"""WS-E: the retention purge worker mirrors the SLA-checker resilience pattern."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from zeroth.governance.retention.models import RetentionPolicy
from zeroth.governance.retention.worker import RetentionPurgeWorker


@dataclass
class _RecordingPolicyRepo:
    policy: RetentionPolicy

    async def resolve(self) -> RetentionPolicy:
        return self.policy


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


async def test_worker_is_bound_to_one_deployment_owner_scope() -> None:
    service_a = _FlakyErasureService("none")
    service_b = _FlakyErasureService("none")
    policy = RetentionPolicy(tenant_id="tenant-a", run_ttl_seconds=1)
    worker_a = RetentionPurgeWorker(
        tenant_id="tenant-a",
        policy_repository=_RecordingPolicyRepo(policy),  # type: ignore[arg-type]
        erasure_service=service_a,  # type: ignore[arg-type]
    )
    worker_b = RetentionPurgeWorker(
        tenant_id="tenant-a",
        policy_repository=_RecordingPolicyRepo(policy),  # type: ignore[arg-type]
        erasure_service=service_b,  # type: ignore[arg-type]
    )

    await worker_a.sweep_once()

    assert worker_b.erasure_service is service_b
    assert service_a.purged == ["tenant-a"]
    assert service_a.audit_swept == ["tenant-a"]
    assert service_b.purged == []
    assert service_b.audit_swept == []


async def test_worker_applies_bound_repository_default_policy() -> None:
    service = _FlakyErasureService("none")
    worker = RetentionPurgeWorker(
        tenant_id="tenant-default",
        policy_repository=_RecordingPolicyRepo(
            RetentionPolicy(tenant_id="tenant-default", run_ttl_seconds=123)
        ),  # type: ignore[arg-type]
        erasure_service=service,  # type: ignore[arg-type]
    )

    await worker.sweep_once()

    assert service.purged == ["tenant-default"]
    assert service.audit_swept == ["tenant-default"]


async def test_worker_skips_a_disabled_bound_policy() -> None:
    service = _FlakyErasureService("none")
    worker = RetentionPurgeWorker(
        tenant_id="tenant-disabled",
        policy_repository=_RecordingPolicyRepo(
            RetentionPolicy(tenant_id="tenant-disabled", enabled=False)
        ),  # type: ignore[arg-type]
        erasure_service=service,  # type: ignore[arg-type]
    )

    await worker.sweep_once()

    assert service.purged == []
    assert service.audit_swept == []


async def test_worker_survives_bound_tenant_failure() -> None:
    service = _FlakyErasureService(failing_tenant="tenant-boom")
    worker = RetentionPurgeWorker(
        tenant_id="tenant-boom",
        policy_repository=_RecordingPolicyRepo(RetentionPolicy(tenant_id="tenant-boom")),  # type: ignore[arg-type]
        erasure_service=service,  # type: ignore[arg-type]
        poll_interval=0.01,
    )

    task = asyncio.create_task(worker.poll_loop())
    await asyncio.sleep(0.03)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "tenant-boom" not in service.purged


async def test_worker_cancellation_propagates() -> None:
    """CancelledError from the sleep must re-raise, not get swallowed."""
    worker = RetentionPurgeWorker(
        tenant_id="tenant-a",
        policy_repository=_RecordingPolicyRepo(RetentionPolicy(tenant_id="tenant-a")),  # type: ignore[arg-type]
        erasure_service=_FlakyErasureService("none"),  # type: ignore[arg-type]
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


async def test_worker_sweeps_surfaces_independently() -> None:
    """A failing run sweep must not starve the same tenant's audit sweep."""
    service = _HalfFailingService(fail_runs_for="tenant-a")
    worker = RetentionPurgeWorker(
        tenant_id="tenant-a",
        policy_repository=_RecordingPolicyRepo(RetentionPolicy(tenant_id="tenant-a")),  # type: ignore[arg-type]
        erasure_service=service,  # type: ignore[arg-type]
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
