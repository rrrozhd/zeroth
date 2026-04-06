"""Tests for durable dispatch: run survives worker restart."""

from __future__ import annotations

import asyncio
import contextlib

from tests.service.helpers import agent_graph, deploy_service
from zeroth.dispatch import LeaseManager, RunWorker
from zeroth.runs import RunStatus
from zeroth.runs.models import Run

DEPLOYMENT = "durable-dispatch-test"


def _make_worker(service, deployment_ref: str) -> RunWorker:
    """Construct a fresh RunWorker with no background tasks started."""
    return RunWorker(
        deployment_ref=deployment_ref,
        run_repository=service.run_repository,
        orchestrator=service.orchestrator,
        graph=service.graph,
        lease_manager=service.lease_manager,
        max_concurrency=4,
    )


async def test_worker_picks_up_pending_run(sqlite_db) -> None:
    """A PENDING run created before the worker starts should be picked up."""
    service, _ = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-pickup"),
        deployment_ref=DEPLOYMENT + "-pickup",
    )

    # Create a run directly in the DB (simulate client posting before worker starts).
    run = await service.run_repository.create(
        Run(
            graph_version_ref=service.deployment.graph_version_ref,
            deployment_ref=service.deployment.deployment_ref,
        )
    )
    assert run.status is RunStatus.PENDING

    # Start a new worker and let it poll once.
    worker = _make_worker(service, service.deployment.deployment_ref)

    async def _run():
        task = asyncio.create_task(worker.poll_loop())
        # Give it time to claim and process the run.
        await asyncio.sleep(1.0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    await _run()

    final = await service.run_repository.get(run.run_id)
    assert final is not None
    # The run should have been claimed (moved to RUNNING or beyond).
    assert final.status is not RunStatus.PENDING


async def test_run_with_no_agent_runner_does_not_crash_worker(sqlite_db) -> None:
    """Worker handles runs gracefully when no agent runner is configured.

    When the orchestrator drives a run and the agent runner fails, the run
    should end up in FAILED (not leave the worker crashed).
    """
    service, _ = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-norunner"),
        deployment_ref=DEPLOYMENT + "-norunner",
    )

    run = await service.run_repository.create(
        Run(
            graph_version_ref=service.deployment.graph_version_ref,
            deployment_ref=service.deployment.deployment_ref,
        )
    )

    worker = _make_worker(service, service.deployment.deployment_ref)

    async def _run():
        task = asyncio.create_task(worker.poll_loop())
        await asyncio.sleep(1.5)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    await _run()

    final = await service.run_repository.get(run.run_id)
    assert final is not None
    # Either completed or failed — either is acceptable, but NOT pending (stuck).
    assert final.status is not RunStatus.PENDING


async def test_lease_prevents_double_claim(sqlite_db) -> None:
    """Two concurrent workers should not both claim the same run."""
    service, _ = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-double"),
        deployment_ref=DEPLOYMENT + "-double",
    )

    run = await service.run_repository.create(
        Run(
            graph_version_ref=service.deployment.graph_version_ref,
            deployment_ref=service.deployment.deployment_ref,
        )
    )

    lease_manager = LeaseManager(sqlite_db)
    claimed_run_ids: list[str] = []

    run_id_1 = await lease_manager.claim_pending(service.deployment.deployment_ref, "worker-1")
    run_id_2 = await lease_manager.claim_pending(service.deployment.deployment_ref, "worker-2")

    if run_id_1 is not None:
        claimed_run_ids.append(run_id_1)
    if run_id_2 is not None:
        claimed_run_ids.append(run_id_2)

    # Exactly one worker should have claimed the run.
    assert len(claimed_run_ids) == 1
    assert claimed_run_ids[0] == run.run_id
