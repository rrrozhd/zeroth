"""Cancellation must win races with resolved approval continuation."""

from __future__ import annotations

import asyncio

import pytest

from zeroth.contracts.graph import (
    Edge,
    ExecutionSettings,
    Graph,
    HumanApprovalNode,
    HumanApprovalNodeData,
)
from zeroth.governance.approvals import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalRepository,
    ApprovalService,
)
from zeroth.governance.identity import ActorIdentity, AuthMethod
from zeroth.governance.audit import AuditRepository
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


class _PostCasReadBarrierRunRepository(RunRepository):
    """Pause the drive's first status read after approval is durably resumed."""

    def __init__(self, database) -> None:
        super().__init__(database, NullWorkspaceScopeContext.for_default_compatibility())
        self.drive_status_read = asyncio.Event()
        self.release_drive_status_read = asyncio.Event()
        self._approval_cas_done = False
        self._running_reads = 0

    async def put_if_status(self, run: Run, expected_status: RunStatus) -> Run:
        stored = await super().put_if_status(run, expected_status)
        if expected_status is RunStatus.WAITING_APPROVAL and run.status is RunStatus.RUNNING:
            self._approval_cas_done = True
        return stored

    async def get(self, run_id: str) -> Run | None:
        run = await super().get(run_id)
        if self._approval_cas_done and run is not None and run.status is RunStatus.RUNNING:
            self._running_reads += 1
            # resume_graph reloads once; the next read is GraphDriver.external_stop.
            if self._running_reads == 2:
                self.drive_status_read.set()
                await self.release_drive_status_read.wait()
        return run


class _PreCasWriteBarrierRunRepository(RunRepository):
    """Pause approval continuation after history preparation but before its CAS."""

    def __init__(self, database) -> None:
        super().__init__(database, NullWorkspaceScopeContext.for_default_compatibility())
        self.before_approval_cas = asyncio.Event()
        self.release_approval_cas = asyncio.Event()

    async def put_if_status(self, run: Run, expected_status: RunStatus) -> Run:
        if expected_status is RunStatus.WAITING_APPROVAL and run.status is RunStatus.RUNNING:
            self.before_approval_cas.set()
            await self.release_approval_cas.wait()
        return await super().put_if_status(run, expected_status)


class _RecordingAuditRepository:
    """Retain every attempted durable audit write for race assertions."""

    def __init__(self, database) -> None:
        self._inner = AuditRepository.for_default_compatibility(database)
        self.records = []

    async def write(self, record):
        self.records.append(record)
        return await self._inner.write(record)


def _actor() -> ActorIdentity:
    return ActorIdentity(
        subject="reviewer-1",
        roles=["reviewer"],
        tenant_id="default",
        auth_method=AuthMethod.API_KEY,
    )


def _graph(*, with_successor: bool = False) -> Graph:
    nodes = [
        HumanApprovalNode(
            node_id=NODE_ID,
            graph_version_ref="approval-cancellation-race:v1",
            human_approval=HumanApprovalNodeData(),
        )
    ]
    edges = []
    if with_successor:
        nodes.append(
            HumanApprovalNode(
                node_id="next-approval",
                graph_version_ref="approval-cancellation-race:v1",
                human_approval=HumanApprovalNodeData(),
            )
        )
        edges.append(
            Edge(edge_id="approval-next", source_node_id=NODE_ID, target_node_id="next-approval")
        )
    return Graph(
        graph_id="approval-cancellation-race",
        name="Approval cancellation race",
        entry_step=NODE_ID,
        execution_settings=ExecutionSettings(sequential_join_enabled=False),
        nodes=nodes,
        edges=edges,
    )


async def _resolved_service(
    database,
    decision: ApprovalDecision,
    run_repository: RunRepository | None = None,
) -> tuple[ApprovalService, RunRepository, str]:
    run_repository = run_repository or _ReadBarrierRunRepository(database)
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
        audit_repository=AuditRepository.for_default_compatibility(database),
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
    assert isinstance(repository, _ReadBarrierRunRepository)
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
    assert isinstance(repository, _ReadBarrierRunRepository)
    record = await service.get(approval_id)
    assert record is not None
    graph = _graph()
    orchestrator = RuntimeOrchestrator(
        run_repository=repository,
        audit_repository=AuditRepository.for_default_compatibility(dual_database),
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


@pytest.mark.parametrize("with_successor", [False, True], ids=["completion", "next-node"])
async def test_cancel_wins_after_inline_approval_cas_before_drive_write(
    dual_database,
    with_successor: bool,
) -> None:
    repository = _PostCasReadBarrierRunRepository(dual_database)
    service, _, approval_id = await _resolved_service(
        dual_database,
        ApprovalDecision.APPROVE,
        repository,
    )
    record = await service.get(approval_id)
    assert record is not None
    orchestrator = RuntimeOrchestrator(
        run_repository=repository,
        audit_repository=AuditRepository.for_default_compatibility(dual_database),
        agent_runners={},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
        approval_service=service,
    )

    continuation = asyncio.create_task(
        service.continue_run(
            approval_id,
            graph=_graph(with_successor=with_successor),
            orchestrator=orchestrator,
        )
    )
    await repository.drive_status_read.wait()
    await RunRepository.for_default_compatibility(dual_database).transition(
        record.run_id,
        RunStatus.FAILED,
        failure_state=RunFailureState(
            reason="admin_cancelled",
            message="run cancelled by administrator",
        ),
    )
    repository.release_drive_status_read.set()

    with pytest.raises(ValueError, match="RUNNING|running"):
        await continuation
    await _assert_cancelled(dual_database, record.run_id)


async def test_cancelled_inline_approval_does_not_publish_completed_node_audit(
    dual_database,
) -> None:
    repository = _PreCasWriteBarrierRunRepository(dual_database)
    service, _, approval_id = await _resolved_service(
        dual_database,
        ApprovalDecision.APPROVE,
        repository,
    )
    record = await service.get(approval_id)
    assert record is not None
    audit_repository = _RecordingAuditRepository(dual_database)
    orchestrator = RuntimeOrchestrator(
        run_repository=repository,
        audit_repository=audit_repository,
        agent_runners={},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
        approval_service=service,
    )

    continuation = asyncio.create_task(
        service.continue_run(approval_id, graph=_graph(), orchestrator=orchestrator)
    )
    await repository.before_approval_cas.wait()
    await RunRepository.for_default_compatibility(dual_database).transition(
        record.run_id,
        RunStatus.FAILED,
        failure_state=RunFailureState(
            reason="admin_cancelled",
            message="run cancelled by administrator",
        ),
    )
    repository.release_approval_cas.set()

    with pytest.raises(ValueError, match="WAITING_APPROVAL|waiting_approval"):
        await continuation
    assert not any(
        audit.node_id == NODE_ID and audit.status == "completed"
        for audit in audit_repository.records
    )


async def test_status_cas_does_not_recreate_a_deleted_run(dual_database) -> None:
    repository = RunRepository.for_default_compatibility(dual_database)
    run = await repository.create(
        Run(
            graph_version_ref="approval-cancellation-race:v1",
            deployment_ref=DEPLOYMENT,
        )
    )
    snapshot = await repository.get(run.run_id)
    assert snapshot is not None
    await repository.delete(run.run_id)
    snapshot.status = RunStatus.RUNNING
    snapshot.touch()

    with pytest.raises(ValueError):
        await repository.put_if_status(snapshot, RunStatus.PENDING)
    assert await repository.get(run.run_id) is None
