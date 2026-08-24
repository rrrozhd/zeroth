from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

import zeroth.governance.approvals.models as approval_models
from zeroth.governance.approvals import (
    ApprovalDecision,
    ApprovalRepository,
    ApprovalService,
    ApprovalStatus,
)
from zeroth.governance.audit import AuditRepository
from zeroth.contracts.governed import RunStatus
from zeroth.contracts.graph import HumanApprovalNode, HumanApprovalNodeData
from zeroth.governance.identity import ActorIdentity, AuthMethod, ServiceRole
from zeroth.runtime.runs import Run
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.platform.primitives import utc_now
from zeroth.platform.storage import NullWorkspaceScopeContext


def test_approval_models_consume_platform_clock_per_instance() -> None:
    assert approval_models.ApprovalResolution.model_fields["resolved_at"].default_factory is utc_now
    assert approval_models.ApprovalRecord.model_fields["created_at"].default_factory is utc_now
    assert approval_models.ApprovalRecord.model_fields["updated_at"].default_factory is utc_now

    first = approval_models.ApprovalRecord(
        run_id="run-1",
        node_id="node-1",
        graph_version_ref="graph@1",
        deployment_ref="deployment-1",
        summary="Review",
        rationale="Policy requires approval",
    )
    second = approval_models.ApprovalRecord(
        run_id="run-2",
        node_id="node-2",
        graph_version_ref="graph@1",
        deployment_ref="deployment-1",
        summary="Review again",
        rationale="Policy requires approval",
    )

    assert first.created_at.tzinfo is UTC
    assert first.created_at is not second.created_at


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
        run_repository=RunRepository.for_default_compatibility(sqlite_db),
        audit_repository=AuditRepository.for_default_compatibility(sqlite_db),
    )
    run = await RunRepository.for_default_compatibility(sqlite_db).create(_run())

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


async def test_approval_service_sanitizes_proposed_payload_before_persistence(sqlite_db) -> None:
    repository = ApprovalRepository(sqlite_db)
    service = ApprovalService(
        repository=repository,
        run_repository=RunRepository.for_default_compatibility(sqlite_db),
    )
    run = await RunRepository.for_default_compatibility(sqlite_db).create(_run())

    record = await service.create_pending(
        run=run,
        node=_node(),
        input_payload={"Api-Key": "secret-value", "value": 2},
    )
    persisted = await repository.get(record.approval_id)

    assert record.context_excerpt == {"Api-Key": "***REDACTED***", "value": 2}
    assert record.proposed_payload == record.context_excerpt
    assert persisted is not None
    assert persisted.proposed_payload == record.context_excerpt


async def test_resolved_approval_cannot_be_escalated(sqlite_db) -> None:
    repository = ApprovalRepository(sqlite_db)
    service = ApprovalService(
        repository=repository,
        run_repository=RunRepository.for_default_compatibility(sqlite_db),
    )
    run = await RunRepository.for_default_compatibility(sqlite_db).create(_run())
    pending = await service.create_pending(run=run, node=_node(), input_payload={"value": 2})
    resolved = await service.resolve(
        pending.approval_id,
        decision=ApprovalDecision.APPROVE,
        actor=ActorIdentity(subject="reviewer", auth_method=AuthMethod.API_KEY),
    )

    escalated = await service.escalate(pending.approval_id)

    assert escalated == resolved
    assert (await repository.get(pending.approval_id)) == resolved


async def test_resolution_wins_escalation_read_write_race(sqlite_db, monkeypatch) -> None:
    repository = ApprovalRepository(sqlite_db)
    service = ApprovalService(
        repository=repository,
        run_repository=RunRepository.for_default_compatibility(sqlite_db),
    )
    run = await RunRepository.for_default_compatibility(sqlite_db).create(_run())
    pending = await service.create_pending(run=run, node=_node(), input_payload={"value": 2})
    service.notifier = AsyncMock()
    original_resolve_pending = repository.resolve_pending
    resolution_committed = asyncio.Event()

    async def resolution_first(record):
        # The alert escalation now writes a still-PENDING row (latched via the
        # 'escalated' marker), not an ESCALATED one, so key the escalation write
        # off that marker rather than off status.
        if record.urgency_metadata.get("escalated"):
            await resolution_committed.wait()
            return await original_resolve_pending(record)
        resolved = await original_resolve_pending(record)
        resolution_committed.set()
        return resolved

    monkeypatch.setattr(repository, "resolve_pending", resolution_first)
    actor = ActorIdentity(subject="reviewer", auth_method=AuthMethod.API_KEY)
    escalated, resolved = await asyncio.gather(
        service.escalate(pending.approval_id),
        service.resolve(
            pending.approval_id,
            decision=ApprovalDecision.APPROVE,
            actor=actor,
        ),
    )

    assert escalated == resolved
    assert resolved.status is ApprovalStatus.RESOLVED
    assert await repository.get(pending.approval_id) == resolved
    service.notifier.notify.assert_not_awaited()


async def test_decision_audit_id_cannot_collide_with_runtime_recorder(sqlite_db) -> None:
    audit_repository = AuditRepository.for_default_compatibility(sqlite_db)
    service = ApprovalService(
        repository=ApprovalRepository(sqlite_db),
        run_repository=RunRepository.for_default_compatibility(sqlite_db),
        audit_repository=audit_repository,
    )
    run = await RunRepository.for_default_compatibility(sqlite_db).create(_run())
    run.audit_refs = ["audit:existing"]
    pending = await service.create_pending(run=run, node=_node(), input_payload={"value": 2})
    resolved = await service.resolve(
        pending.approval_id,
        decision=ApprovalDecision.REJECT,
        actor=ActorIdentity(subject="reviewer", auth_method=AuthMethod.API_KEY),
    )

    await service._record_decision_audit(resolved, run, status="rejected", output_payload={})

    [decision] = [
        item for item in await audit_repository.list_by_run(run.run_id) if item.status == "rejected"
    ]
    assert decision.audit_id.startswith(f"{run.run_id}:approval-decision:")
    assert decision.audit_id != f"{run.run_id}:audit:{len(run.audit_refs) + 1}"


async def test_approval_service_resolves_and_is_idempotent(sqlite_db) -> None:
    service = ApprovalService(
        repository=ApprovalRepository(sqlite_db),
        run_repository=RunRepository.for_default_compatibility(sqlite_db),
        audit_repository=AuditRepository.for_default_compatibility(sqlite_db),
    )
    run = await RunRepository.for_default_compatibility(sqlite_db).create(_run())
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


async def test_concurrent_opposite_approval_decisions_publish_exactly_one_winner(
    sqlite_db, monkeypatch
) -> None:
    repository = ApprovalRepository(sqlite_db)
    service = ApprovalService(
        repository=repository, run_repository=RunRepository.for_default_compatibility(sqlite_db)
    )
    run = await RunRepository.for_default_compatibility(sqlite_db).create(_run())
    record = await service.create_pending(run=run, node=_node(), input_payload={"value": 2})
    original_get = repository.get
    both_read = asyncio.Event()
    read_count = 0
    read_lock = asyncio.Lock()

    async def synchronized_get(*args, **kwargs):
        nonlocal read_count
        result = await original_get(*args, **kwargs)
        async with read_lock:
            read_count += 1
            if read_count == 2:
                both_read.set()
        if read_count <= 2:
            await both_read.wait()
        return result

    monkeypatch.setattr(repository, "get", synchronized_get)
    actor = ActorIdentity(subject="reviewer", auth_method=AuthMethod.API_KEY)
    outcomes = await asyncio.gather(
        service.resolve(record.approval_id, decision=ApprovalDecision.APPROVE, actor=actor),
        service.resolve(record.approval_id, decision=ApprovalDecision.REJECT, actor=actor),
        return_exceptions=True,
    )

    winners = [item for item in outcomes if isinstance(item, approval_models.ApprovalRecord)]
    losers = [item for item in outcomes if isinstance(item, ValueError)]
    assert len(winners) == len(losers) == 1
    persisted = await original_get(record.approval_id)
    assert persisted is not None
    assert persisted.resolution == winners[0].resolution


async def test_concurrent_same_approval_decision_replays_one_stable_resolution(
    sqlite_db, monkeypatch
) -> None:
    repository = ApprovalRepository(sqlite_db)
    service = ApprovalService(
        repository=repository, run_repository=RunRepository.for_default_compatibility(sqlite_db)
    )
    run = await RunRepository.for_default_compatibility(sqlite_db).create(_run())
    record = await service.create_pending(run=run, node=_node(), input_payload={"value": 2})
    original_get = repository.get
    both_read = asyncio.Event()
    read_count = 0
    read_lock = asyncio.Lock()

    async def synchronized_get(*args, **kwargs):
        nonlocal read_count
        result = await original_get(*args, **kwargs)
        async with read_lock:
            read_count += 1
            current = read_count
            if read_count == 2:
                both_read.set()
        if current <= 2:
            await both_read.wait()
        return result

    monkeypatch.setattr(repository, "get", synchronized_get)
    actor = ActorIdentity(subject="reviewer", auth_method=AuthMethod.API_KEY)
    first, replay = await asyncio.gather(
        service.resolve(record.approval_id, decision=ApprovalDecision.APPROVE, actor=actor),
        service.resolve(record.approval_id, decision=ApprovalDecision.APPROVE, actor=actor),
    )

    assert first == replay
    assert first.resolution is not None
    assert first.resolution.decision is ApprovalDecision.APPROVE


# ---------------------------------------------------------------------------
# SLA escalation: alert must not wedge the run; auto_reject must fail it
# ---------------------------------------------------------------------------


def _sla_node(
    escalation_action: str,
    *,
    sla_timeout_seconds: int = 300,
    pause_behavior_config: dict | None = None,
) -> HumanApprovalNode:
    return HumanApprovalNode(
        node_id="approval",
        graph_version_ref="graph-approval:v1",
        human_approval=HumanApprovalNodeData(
            resolution_schema_ref="schema://resolution",
            approval_policy_config={"allow_edits": True},
            pause_behavior_config=pause_behavior_config or {},
            sla_timeout_seconds=sla_timeout_seconds,
            escalation_action=escalation_action,
        ),
    )


def _waiting_run() -> Run:
    return Run(
        run_id="run-1",
        thread_id="thread-1",
        graph_version_ref="graph-approval:v1",
        deployment_ref="graph-approval",
        status=RunStatus.WAITING_APPROVAL,
        pending_node_ids=["approval"],
    )


def _deployment_reader(sqlite_db) -> ApprovalRepository:
    """A deployment-scoped reader over the default-tenant/null-workspace owner."""
    return ApprovalRepository.scoped_for_deployment(
        sqlite_db, NullWorkspaceScopeContext.for_default_compatibility(), "graph-approval"
    )


async def test_alert_escalation_keeps_approval_resolvable(sqlite_db) -> None:
    """An alert escalation latches the row out of the sweep but leaves it PENDING
    and fully resolvable — the run never wedges in WAITING_APPROVAL."""
    repository = ApprovalRepository(sqlite_db)
    service = ApprovalService(
        repository=repository,
        run_repository=RunRepository.for_default_compatibility(sqlite_db),
    )
    run = await RunRepository.for_default_compatibility(sqlite_db).create(_run())
    record = await service.create_pending(
        run=run, node=_sla_node("alert"), input_payload={"value": 2}
    )
    record.sla_deadline = datetime.now(UTC) - timedelta(minutes=5)
    await repository.write(record)
    assert await _deployment_reader(sqlite_db).list_overdue() == [record]

    escalated = await service.escalate(record.approval_id)

    assert escalated.status is ApprovalStatus.PENDING
    stored = await repository.get(record.approval_id)
    assert stored.status is ApprovalStatus.PENDING
    assert stored.sla_deadline is None
    assert stored.urgency_metadata["escalated"] is True
    # Latched out of the overdue sweep, so it will not re-escalate.
    assert await _deployment_reader(sqlite_db).list_overdue() == []

    # The human decision still lands — no "approval already resolved" 409.
    resolved = await service.resolve(
        record.approval_id,
        decision=ApprovalDecision.APPROVE,
        actor=ActorIdentity(subject="human", auth_method=AuthMethod.API_KEY),
    )
    assert resolved.status is ApprovalStatus.RESOLVED
    assert resolved.resolution is not None
    assert resolved.resolution.decision is ApprovalDecision.APPROVE


async def test_alert_escalated_not_re_escalated_by_second_poll(sqlite_db) -> None:
    """A second poll tick over an already-alert-latched approval is a true no-op:
    the row is gone from the overdue sweep and no second notification fires."""
    repository = ApprovalRepository(sqlite_db)
    service = ApprovalService(
        repository=repository,
        run_repository=RunRepository.for_default_compatibility(sqlite_db),
    )
    run = await RunRepository.for_default_compatibility(sqlite_db).create(_run())
    record = await service.create_pending(
        run=run, node=_sla_node("alert"), input_payload={"value": 2}
    )
    record.sla_deadline = datetime.now(UTC) - timedelta(minutes=5)
    await repository.write(record)
    service.notifier = AsyncMock()

    first = await service.escalate(record.approval_id)
    assert await _deployment_reader(sqlite_db).list_overdue() == []
    second = await service.escalate(record.approval_id)

    assert first.status is ApprovalStatus.PENDING
    assert second.status is ApprovalStatus.PENDING
    assert second.urgency_metadata["escalated"] is True
    # The latch fired exactly once; the second escalate() did not re-notify.
    assert service.notifier.notify.await_count == 1


async def test_alert_latch_marker_is_not_graph_author_writable(sqlite_db) -> None:
    """A node cannot pre-seed the service-owned latch marker to bypass the fence.

    urgency_metadata is seeded from the node's pause_behavior_config, so a graph
    that sets pause_behavior_config={'escalated': True} would otherwise
    short-circuit the first escalate(), leave sla_deadline live, and re-open the
    SLA webhook storm. create_pending reserves the namespace, and the latch keys
    off the persisted invariant (sla_deadline is None), so the fence still holds.
    """
    repository = ApprovalRepository(sqlite_db)
    service = ApprovalService(
        repository=repository,
        run_repository=RunRepository.for_default_compatibility(sqlite_db),
    )
    run = await RunRepository.for_default_compatibility(sqlite_db).create(_run())
    record = await service.create_pending(
        run=run,
        node=_sla_node("alert", pause_behavior_config={"escalated": True}),
        input_payload={"value": 2},
    )
    # The attacker-controlled marker is stripped at creation.
    assert "escalated" not in record.urgency_metadata
    record.sla_deadline = datetime.now(UTC) - timedelta(minutes=5)
    await repository.write(record)
    service.notifier = AsyncMock()

    await service.escalate(record.approval_id)
    await service.escalate(record.approval_id)

    stored = await repository.get(record.approval_id)
    # The fence actually engaged: deadline nulled, marker set by the service.
    assert stored.sla_deadline is None
    assert stored.urgency_metadata["escalated"] is True
    assert await _deployment_reader(sqlite_db).list_overdue() == []
    # Exactly one real escalation despite the pre-seed + two escalate() calls.
    assert service.notifier.notify.await_count == 1


async def test_auto_reject_fails_the_waiting_run(sqlite_db) -> None:
    """auto_reject SLA escalation both rejects the approval AND fails the run
    (reason=approval_rejected), recording the decision audit."""
    repository = ApprovalRepository(sqlite_db)
    run_repository = RunRepository.for_default_compatibility(sqlite_db)
    audit_repository = AuditRepository.for_default_compatibility(sqlite_db)
    service = ApprovalService(
        repository=repository,
        run_repository=run_repository,
        audit_repository=audit_repository,
    )
    run = await run_repository.put(_waiting_run())
    record = await service.create_pending(
        run=run, node=_sla_node("auto_reject"), input_payload={"value": 2}
    )
    record.sla_deadline = datetime.now(UTC) - timedelta(minutes=5)
    await repository.write(record)

    resolved = await service.escalate(record.approval_id)

    assert resolved.status is ApprovalStatus.RESOLVED
    assert resolved.resolution is not None
    assert resolved.resolution.decision is ApprovalDecision.REJECT

    failed = await run_repository.get(run.run_id)
    assert failed is not None
    assert failed.status is RunStatus.FAILED
    assert failed.failure_state is not None
    assert failed.failure_state.reason == "approval_rejected"

    rejected = [
        item for item in await audit_repository.list_by_run(run.run_id) if item.status == "rejected"
    ]
    assert len(rejected) == 1
