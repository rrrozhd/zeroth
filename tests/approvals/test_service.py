from __future__ import annotations

import pytest

from zeroth.approvals import (
    ApprovalDecision,
    ApprovalRepository,
    ApprovalService,
    ApprovalStatus,
)
from zeroth.audit import AuditRepository
from zeroth.graph import HumanApprovalNode, HumanApprovalNodeData
from zeroth.identity import ActorIdentity, AuthMethod, ServiceRole
from zeroth.runs import Run, RunRepository


def _node() -> HumanApprovalNode:
    return HumanApprovalNode(
        node_id="approval",
        graph_version_ref="graph-approval:v1",
        human_approval=HumanApprovalNodeData(
            resolution_schema_ref="schema://resolution",
            approval_policy_config={"allow_edits": True},
        ),
    )


def _run() -> Run:
    return Run(
        run_id="run-1",
        thread_id="thread-1",
        graph_version_ref="graph-approval:v1",
        deployment_ref="graph-approval",
        pending_node_ids=["approval"],
    )


async def test_approval_service_creates_and_queries_pending_records(sqlite_db) -> None:
    service = ApprovalService(
        repository=ApprovalRepository(sqlite_db),
        run_repository=RunRepository(sqlite_db),
        audit_repository=AuditRepository(sqlite_db),
    )
    run = await RunRepository(sqlite_db).create(_run())

    record = await service.create_pending(
        run=run,
        node=_node(),
        input_payload={"secret": "hidden", "value": 2},
    )

    assert record.status is ApprovalStatus.PENDING
    assert record.allowed_actions == [
        ApprovalDecision.APPROVE,
        ApprovalDecision.REJECT,
        ApprovalDecision.EDIT_AND_APPROVE,
    ]
    assert await service.get(record.approval_id) == record
    assert [item.approval_id for item in await service.list_pending(run_id=run.run_id)] == [
        record.approval_id
    ]
    assert [item.approval_id for item in await service.list_pending(thread_id=run.thread_id)] == [
        record.approval_id
    ]
    assert [
        item.approval_id for item in await service.list_pending(deployment_ref=run.deployment_ref)
    ] == [record.approval_id]
    assert record.context_excerpt["secret"] == "***REDACTED***"


async def test_approval_service_resolves_and_is_idempotent(sqlite_db) -> None:
    service = ApprovalService(
        repository=ApprovalRepository(sqlite_db),
        run_repository=RunRepository(sqlite_db),
        audit_repository=AuditRepository(sqlite_db),
    )
    run = await RunRepository(sqlite_db).create(_run())
    record = await service.create_pending(run=run, node=_node(), input_payload={"value": 2})

    resolved = await service.resolve(
        record.approval_id,
        decision=ApprovalDecision.EDIT_AND_APPROVE,
        actor=ActorIdentity(
            subject="user-1",
            auth_method=AuthMethod.API_KEY,
            roles=[ServiceRole.REVIEWER],
            tenant_id="default",
        ),
        edited_payload={"value": 9},
    )
    repeat = await service.resolve(
        record.approval_id,
        decision=ApprovalDecision.EDIT_AND_APPROVE,
        actor=ActorIdentity(
            subject="user-1",
            auth_method=AuthMethod.API_KEY,
            roles=[ServiceRole.REVIEWER],
            tenant_id="default",
        ),
        edited_payload={"value": 9},
    )

    assert resolved.status is ApprovalStatus.RESOLVED
    assert resolved.resolution is not None
    assert resolved.resolution.edited_payload == {"value": 9}
    assert repeat == resolved

    with pytest.raises(ValueError):
        await service.resolve(
            record.approval_id,
            decision=ApprovalDecision.REJECT,
            actor=ActorIdentity(
                subject="user-2",
                auth_method=AuthMethod.API_KEY,
                roles=[ServiceRole.REVIEWER],
                tenant_id="default",
            ),
        )
