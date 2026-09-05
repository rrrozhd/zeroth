"""Ownership observation must not overwhelm the workload it measures."""

from types import SimpleNamespace

import pytest

from tests.load_release import workload_probe


@pytest.mark.asyncio
async def test_short_approval_claim_survives_release_without_database_polling(monkeypatch):
    async def execute(run_id, **kwargs):
        return run_id

    async def no_holder(*args, **kwargs):
        raise AssertionError("a completed captured claim must not need a live lease")

    runners = {}
    worker = SimpleNamespace(
        worker_id="approval-worker",
        poll_interval=0.005,
        _execute_leased_run=execute,
        lease_manager=SimpleNamespace(current_holder=no_holder),
    )
    service = SimpleNamespace(
        worker=worker,
        orchestrator=SimpleNamespace(agent_runners=runners),
        deployment=SimpleNamespace(tenant_id="tenant", workspace_id=None),
    )
    monkeypatch.setattr(workload_probe, "_CLAIMED_BY", {})
    workload_probe.install_runner(service, "approvals")
    await worker._execute_leased_run("short-approval")
    assert await workload_probe._observed_worker(service, "short-approval") == "approval-worker"
    assert runners == {}
    assert worker.poll_interval >= 0.04


@pytest.mark.asyncio
async def test_missing_worker_stays_fail_closed_without_busy_database_polling(monkeypatch):
    now = 0.0
    reads = 0

    async def sleep(delay):
        nonlocal now
        now += delay

    async def missing(*args, **kwargs):
        nonlocal reads
        reads += 1
        return None

    monkeypatch.setattr(workload_probe.time, "perf_counter", lambda: now)
    monkeypatch.setattr(workload_probe.asyncio, "sleep", sleep)
    monkeypatch.setattr(workload_probe, "_CLAIMED_BY", {})
    service = SimpleNamespace(
        worker=SimpleNamespace(lease_manager=SimpleNamespace(current_holder=missing)),
        deployment=SimpleNamespace(tenant_id="tenant", workspace_id=None),
    )
    with pytest.raises(AssertionError, match="executor was not observed"):
        await workload_probe._observed_worker(service, "never-claimed")
    assert 20 <= now < 20.1
    assert reads <= 401
