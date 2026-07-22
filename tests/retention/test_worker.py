"""WS-E: the retention purge worker mirrors the SLA-checker resilience pattern."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from zeroth.governance.retention.models import RetentionPolicy
from zeroth.governance.retention.worker import RetentionPurgeWorker


@dataclass
class _RecordingPolicyRepo:
    policies: list[RetentionPolicy]

    async def list_all_enabled(self) -> list[RetentionPolicy]:
        return list(self.policies)


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


async def test_worker_survives_single_tenant_failure() -> None:
    """One tenant's purge blowing up must not stop the others or the loop."""
    policies = [
        RetentionPolicy(tenant_id="tenant-a"),
        RetentionPolicy(tenant_id="tenant-boom"),
        RetentionPolicy(tenant_id="tenant-c"),
    ]
    service = _FlakyErasureService(failing_tenant="tenant-boom")
    worker = RetentionPurgeWorker(
        erasure_service=service,  # type: ignore[arg-type]
        policy_repository=_RecordingPolicyRepo(policies),  # type: ignore[arg-type]
        poll_interval=0.01,
    )

    task = asyncio.create_task(worker.poll_loop())
    # Let at least one full sweep run.
    for _ in range(200):
        if "tenant-c" in service.purged:
            break
        await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Both healthy tenants were purged despite the failing one in the middle.
    assert "tenant-a" in service.purged
    assert "tenant-c" in service.purged
    assert "tenant-boom" not in service.purged


async def test_worker_cancellation_propagates() -> None:
    """CancelledError from the sleep must re-raise, not get swallowed."""
    worker = RetentionPurgeWorker(
        erasure_service=_FlakyErasureService("none"),  # type: ignore[arg-type]
        policy_repository=_RecordingPolicyRepo([]),  # type: ignore[arg-type]
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
    policies = [RetentionPolicy(tenant_id="tenant-a"), RetentionPolicy(tenant_id="tenant-b")]
    service = _HalfFailingService(fail_runs_for="tenant-a")
    worker = RetentionPurgeWorker(
        erasure_service=service,  # type: ignore[arg-type]
        policy_repository=_RecordingPolicyRepo(policies),  # type: ignore[arg-type]
        poll_interval=0.01,
    )

    task = asyncio.create_task(worker.poll_loop())
    for _ in range(200):
        if "tenant-b" in service.audit_swept:
            break
        await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "tenant-a" in service.audit_swept  # audit sweep survived the runs failure
    assert "tenant-b" in service.run_swept
    assert "tenant-a" not in service.run_swept
