from __future__ import annotations

import pytest

from zeroth.contracts.governed import RunStatus
from zeroth.contracts.graph import HumanApprovalNode, HumanApprovalNodeData
from zeroth.governance.approvals import (
    ApprovalDecision,
    ApprovalRepository,
    ApprovalService,
)
from zeroth.governance.audit import AuditRepository
from zeroth.governance.identity import ActorIdentity, AuthMethod
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.platform.signing import EnvHmacSigner
from zeroth.runtime.runs import Run


def _approval_node(node_id: str) -> HumanApprovalNode:
    return HumanApprovalNode(
        node_id=node_id,
        graph_version_ref="child-graph:v1",
        human_approval=HumanApprovalNodeData(),
    )


async def _paused_family(
    run_repository: RunRepository,
    *,
    parent_deployment: str = "parent-deployment",
    parent_graph: str = "parent-graph:v1",
) -> tuple[Run, Run]:
    parent = Run(
        run_id="parent-run",
        thread_id="parent-thread",
        graph_version_ref=parent_graph,
        deployment_ref=parent_deployment,
        status=RunStatus.WAITING_APPROVAL,
        metadata={
            "pending_subgraph": {
                "child_run_id": "child-run",
                "node_id": "approval-child",
                "graph_ref": "child-deployment",
                "version": 1,
            }
        },
    )
    child = Run(
        run_id="child-run",
        thread_id="child-thread",
        graph_version_ref="child-graph:v1",
        deployment_ref="child-deployment",
        parent_run_id=parent.run_id,
        status=RunStatus.WAITING_APPROVAL,
        metadata={"parent_run_id": parent.run_id, "subgraph_depth": 1},
    )
    return await run_repository.create(parent), await run_repository.create(child)


async def test_parent_deployment_can_only_see_pending_approvals_in_its_run_ancestry(
    sqlite_db,
) -> None:
    run_repository = RunRepository.for_default_compatibility(sqlite_db)
    approval_repository = ApprovalRepository(sqlite_db)
    service = ApprovalService(
        repository=approval_repository,
        run_repository=run_repository,
    )
    _parent, child = await _paused_family(run_repository)
    visible = await service.create_pending(
        run=child,
        node=_approval_node("subgraph:child-deployment:1:approval"),
        input_payload={"ticket": "visible"},
    )

    unrelated_parent = Run(
        run_id="unrelated-parent",
        thread_id="unrelated-parent-thread",
        graph_version_ref="other-parent:v1",
        deployment_ref="other-parent",
        status=RunStatus.WAITING_APPROVAL,
    )
    unrelated_child = Run(
        run_id="unrelated-child",
        thread_id="unrelated-child-thread",
        graph_version_ref="child-graph:v1",
        deployment_ref="child-deployment",
        parent_run_id=unrelated_parent.run_id,
        status=RunStatus.WAITING_APPROVAL,
    )
    await run_repository.create(unrelated_parent)
    unrelated_child = await run_repository.create(unrelated_child)
    hidden = await service.create_pending(
        run=unrelated_child,
        node=_approval_node("subgraph:child-deployment:1:approval"),
        input_payload={"ticket": "hidden"},
    )

    records = await service.list_pending_visible_to_deployment(
        deployment_ref="parent-deployment",
        graph_version_ref="parent-graph:v1",
        tenant_id="default",
        workspace_id=None,
    )

    assert [record.approval_id for record in records] == [visible.approval_id]
    by_parent_run = await service.list_pending_visible_to_deployment(
        deployment_ref="parent-deployment",
        graph_version_ref="parent-graph:v1",
        run_id="parent-run",
        tenant_id="default",
        workspace_id=None,
    )
    by_parent_thread = await service.list_pending_visible_to_deployment(
        deployment_ref="parent-deployment",
        graph_version_ref="parent-graph:v1",
        thread_id="parent-thread",
        tenant_id="default",
        workspace_id=None,
    )
    assert [record.approval_id for record in by_parent_run] == [visible.approval_id]
    assert [record.approval_id for record in by_parent_thread] == [visible.approval_id]
    assert (
        await service.get_visible_to_deployment(
            visible.approval_id,
            deployment_ref="parent-deployment",
            graph_version_ref="parent-graph:v1",
            tenant_id="default",
            workspace_id=None,
        )
        == visible
    )
    assert (
        await service.get_visible_to_deployment(
            hidden.approval_id,
            deployment_ref="parent-deployment",
            graph_version_ref="parent-graph:v1",
            tenant_id="default",
            workspace_id=None,
        )
        is None
    )


async def test_child_resolution_atomically_schedules_one_signed_parent_continuation(
    sqlite_db,
) -> None:
    signer = EnvHmacSigner(key_id="child-continuation", keys={"child-continuation": b"test-key"})
    run_repository = RunRepository.for_default_compatibility(sqlite_db)
    audit_repository = AuditRepository.for_default_compatibility(sqlite_db, signer=signer)
    service = ApprovalService(
        repository=ApprovalRepository(sqlite_db),
        run_repository=run_repository,
        audit_repository=audit_repository,
    )
    parent, child = await _paused_family(run_repository)
    pending = await service.create_pending(
        run=child,
        node=_approval_node("subgraph:child-deployment:1:approval"),
        input_payload={"ticket": "one"},
    )
    resolved = await service.resolve(
        pending.approval_id,
        decision=ApprovalDecision.APPROVE,
        actor=ActorIdentity(subject="reviewer", auth_method=AuthMethod.API_KEY),
    )

    scheduled = await service.schedule_ancestor_continuation(
        resolved.approval_id,
        deployment_ref=parent.deployment_ref,
        graph_version_ref=parent.graph_version_ref,
    )
    replay = await service.schedule_ancestor_continuation(
        resolved.approval_id,
        deployment_ref=parent.deployment_ref,
        graph_version_ref=parent.graph_version_ref,
    )

    assert scheduled.run_id == replay.run_id == parent.run_id
    assert scheduled.status is RunStatus.PENDING
    marker = scheduled.metadata["child_approval_continuation"]
    assert marker == {
        "approval_id": resolved.approval_id,
        "child_run_id": child.run_id,
        "child_deployment_ref": child.deployment_ref,
    }
    persisted_child = await run_repository.get(child.run_id)
    assert persisted_child is not None
    assert persisted_child.status is RunStatus.WAITING_APPROVAL

    notifications = [
        record
        for record in await audit_repository.list_by_run(parent.run_id)
        if record.status == "child_approval_continuation_scheduled"
    ]
    assert len(notifications) == 1
    [notification] = notifications
    assert notification.record_signature is not None
    assert notification.approval_actions[0].approval_id == resolved.approval_id
    assert notification.execution_metadata["child_run_id"] == child.run_id
    evidence_records = await service.list_visible_to_deployment(
        deployment_ref=parent.deployment_ref,
        graph_version_ref=parent.graph_version_ref,
        run_id=parent.run_id,
        tenant_id="default",
        workspace_id=None,
    )
    assert [record.approval_id for record in evidence_records] == [resolved.approval_id]


async def test_unsigned_child_notification_rolls_parent_schedule_back(sqlite_db) -> None:
    run_repository = RunRepository.for_default_compatibility(sqlite_db)
    service = ApprovalService(
        repository=ApprovalRepository(sqlite_db),
        run_repository=run_repository,
        audit_repository=AuditRepository.for_default_compatibility(sqlite_db),
    )
    parent, child = await _paused_family(run_repository)
    pending = await service.create_pending(
        run=child,
        node=_approval_node("subgraph:child-deployment:1:approval"),
        input_payload={"ticket": "unsigned"},
    )
    resolved = await service.resolve(
        pending.approval_id,
        decision=ApprovalDecision.APPROVE,
        actor=ActorIdentity(subject="reviewer", auth_method=AuthMethod.API_KEY),
    )

    with pytest.raises(RuntimeError, match="signed audit"):
        await service.schedule_ancestor_continuation(
            resolved.approval_id,
            deployment_ref=parent.deployment_ref,
            graph_version_ref=parent.graph_version_ref,
        )

    persisted_parent = await run_repository.get(parent.run_id)
    assert persisted_parent is not None
    assert persisted_parent.status is RunStatus.WAITING_APPROVAL
    assert "child_approval_continuation" not in persisted_parent.metadata
