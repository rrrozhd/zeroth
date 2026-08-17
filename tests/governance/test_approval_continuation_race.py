"""Cancellation must win races with resolved approval continuation."""

from __future__ import annotations

import asyncio

import pytest

from zeroth.contracts.graph import Graph, HumanApprovalNode, HumanApprovalNodeData
from zeroth.governance.approvals import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalRepository,
    ApprovalService,
)
from zeroth.governance.identity import ActorIdentity, AuthMethod
from zeroth.integrations.execution import ExecutableUnitRegistry, ExecutableUnitRunner
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.platform.storage import NullWorkspaceScopeContext
from zeroth.runtime.orchestration import RuntimeOrchestrator
from zeroth.runtime.runs import Run, RunFailureState, RunStatus

DEPLOYMENT = "approval-cancellation-race"
NODE_ID = "approval-node"


class _ReadBarrierRunRepository(RunRepository):
    """Pause one run read after returning its snapshot to the continuation."""

    def __init__(self, database) -> None:
        super().__init__(database, NullWorkspaceScopeContext.for_default_compatibility())
        self.snapshot_read = asyncio.Event()
        self.release_snapshot = asyncio.Event()
        self._armed = False

    def arm(self) -> None:
        self._armed = True

    async def get(self, run_id: str) -> Run | None:
        run = await super().get(run_id)
        if self._armed:
            self._armed = False
            self.snapshot_read.set()
            await self.release_snapshot.wait()
        return run


def _actor() -> ActorIdentity:
    return ActorIdentity(
        subject="reviewer-1",
        roles=["reviewer"],
        tenant_id="default",
        auth_method=AuthMethod.API_KEY,
    )


def _graph() -> Graph:
    return Graph(
        graph_id="approval-cancellation-race",
        name="Approval cancellation race",
        entry_step=NODE_ID,
        nodes=[
            HumanApprovalNode(
                node_id=NODE_ID,
                graph_version_ref="approval-cancellation-race:v1",
                human_approval=HumanApprovalNodeData(),
            )
        ],
        edges=[],
    )


async def _resolved_service(
    database,
    decision: ApprovalDecision,
) -> tuple[ApprovalService, _ReadBarrierRunRepository, str]:
    run_repository = _ReadBarrierRunRepository(database)
    run = await run_repository.create(
        Run(
            graph_version_ref="approval-cancellation-race:v1",
            deployment_ref=DEPLOYMENT,
            pending_node_ids=[NODE_ID],
        )
    )
    await run_repository.transition(run.run_id, RunStatus.RUNNING)
    run = await run_repository.transition(run.run_id, RunStatus.WAITING_APPROVAL)
    service = ApprovalService(
        repository=ApprovalRepository(database),
        run_repository=run_repository,
    )
    approval = ApprovalRecord(
        run_id=run.run_id,
        thread_id=run.thread_id,
        node_id=NODE_ID,
        graph_version_ref=run.graph_version_ref,
        deployment_ref=run.deployment_ref,
        tenant_id="default",
        summary="test approval",
        rationale="test rationale",
        allowed_actions=[ApprovalDecision.APPROVE, ApprovalDecision.REJECT],
    )
    await service.repository.write(approval)
    await service.resolve(
        approval.approval_id,
        decision=decision,
        actor=_actor(),
    )
    return service, run_repository, approval.approval_id


async def _cancel_after_snapshot(
    database,
    repository: _ReadBarrierRunRepository,
    run_id: str,
) -> None:
    await repository.snapshot_read.wait()
    cancellation_repository = RunRepository.for_default_compatibility(database)
    await cancellation_repository.transition(
        run_id,
        RunStatus.FAILED,
        failure_state=RunFailureState(
            reason="admin_cancelled",
            message="run cancelled by administrator",
        ),
    )
    repository.release_snapshot.set()


async def _assert_cancelled(database, run_id: str) -> None:
    stored = await RunRepository.for_default_compatibility(database).get(run_id)
    assert stored is not None
    assert stored.status is RunStatus.FAILED
    assert stored.failure_state is not None
    assert stored.failure_state.reason == "admin_cancelled"


@pytest.mark.parametrize(
    "decision",
    [ApprovalDecision.APPROVE, ApprovalDecision.REJECT],
)
async def test_cancel_wins_durable_approval_continuation_race(
    dual_database,
    decision: ApprovalDecision,
) -> None:
    service, repository, approval_id = await _resolved_service(dual_database, decision)
    record = await service.get(approval_id)
    assert record is not None
    repository.arm()

    continuation = asyncio.create_task(service.schedule_continuation(approval_id))
    cancellation = asyncio.create_task(
        _cancel_after_snapshot(dual_database, repository, record.run_id)
    )
    with pytest.raises(ValueError, match="WAITING_APPROVAL|waiting_approval"):
        await continuation
    await cancellation

    await _assert_cancelled(dual_database, record.run_id)


@pytest.mark.parametrize(
    "decision",
    [ApprovalDecision.APPROVE, ApprovalDecision.REJECT],
)
async def test_cancel_wins_inline_approval_continuation_race(
    dual_database,
    decision: ApprovalDecision,
) -> None:
    service, repository, approval_id = await _resolved_service(dual_database, decision)
    record = await service.get(approval_id)
    assert record is not None
    graph = _graph()
    orchestrator = RuntimeOrchestrator(
        run_repository=repository,
        agent_runners={},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
        approval_service=service,
    )
    repository.arm()

    continuation = asyncio.create_task(
        service.continue_run(approval_id, graph=graph, orchestrator=orchestrator)
    )
    cancellation = asyncio.create_task(
        _cancel_after_snapshot(dual_database, repository, record.run_id)
    )
    with pytest.raises(ValueError, match="WAITING_APPROVAL|waiting_approval"):
        await continuation
    await cancellation

    await _assert_cancelled(dual_database, record.run_id)
