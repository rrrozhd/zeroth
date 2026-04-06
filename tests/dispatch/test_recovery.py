"""Tests for approval resume and durable dispatch recovery semantics."""
from __future__ import annotations

from zeroth.approvals import ApprovalDecision, ApprovalRepository, ApprovalService
from zeroth.approvals.models import ApprovalRecord
from zeroth.audit import AuditRepository
from zeroth.dispatch.lease import LeaseManager
from zeroth.identity import ActorIdentity
from zeroth.identity.models import AuthMethod
from zeroth.runs import RunRepository, RunStatus
from zeroth.runs.models import Run

DEPLOYMENT = "recovery-test-deployment"


async def _make_run(run_repo: RunRepository) -> Run:
    run = Run(graph_version_ref="g:v1", deployment_ref=DEPLOYMENT)
    return await run_repo.create(run)


def _make_approval_service(sqlite_db) -> ApprovalService:
    return ApprovalService(
        repository=ApprovalRepository(sqlite_db),
        run_repository=RunRepository(sqlite_db),
        audit_repository=AuditRepository(sqlite_db),
    )


def _actor() -> ActorIdentity:
    return ActorIdentity(
        subject="reviewer-1",
        roles=["reviewer"],
        tenant_id="default",
        auth_method=AuthMethod.API_KEY,
    )


async def test_schedule_continuation_transitions_run_to_pending(
    sqlite_db,
) -> None:
    run_repo = RunRepository(sqlite_db)
    approval_service = _make_approval_service(sqlite_db)

    run = await _make_run(run_repo)
    await run_repo.transition(run.run_id, RunStatus.RUNNING)
    await run_repo.transition(run.run_id, RunStatus.WAITING_APPROVAL)

    # Create a minimal approval record directly.
    approval_record = ApprovalRecord(
        run_id=run.run_id,
        thread_id=run.thread_id,
        node_id="approval-node",
        graph_version_ref=run.graph_version_ref,
        deployment_ref=run.deployment_ref,
        tenant_id="default",
        summary="test approval",
        rationale="test rationale",
        allowed_actions=[ApprovalDecision.APPROVE],
    )
    await approval_service.repository.write(approval_record)
    approval_id = approval_record.approval_id

    # Resolve the approval.
    await approval_service.resolve(approval_id, decision=ApprovalDecision.APPROVE, actor=_actor())

    # schedule_continuation should transition run to PENDING.
    result = await approval_service.schedule_continuation(approval_id)

    assert result.status is RunStatus.PENDING
    assert result.metadata.get("approval_resolved_id") == approval_id


async def test_schedule_continuation_reject_marks_failed(sqlite_db) -> None:
    run_repo = RunRepository(sqlite_db)
    approval_service = _make_approval_service(sqlite_db)

    run = await _make_run(run_repo)
    await run_repo.transition(run.run_id, RunStatus.RUNNING)
    await run_repo.transition(run.run_id, RunStatus.WAITING_APPROVAL)

    approval_record = ApprovalRecord(
        run_id=run.run_id,
        thread_id=run.thread_id,
        node_id="approval-node",
        graph_version_ref=run.graph_version_ref,
        deployment_ref=run.deployment_ref,
        tenant_id="default",
        summary="test approval",
        rationale="test rationale",
        allowed_actions=[ApprovalDecision.APPROVE, ApprovalDecision.REJECT],
    )
    await approval_service.repository.write(approval_record)
    approval_id = approval_record.approval_id

    await approval_service.resolve(approval_id, decision=ApprovalDecision.REJECT, actor=_actor())
    result = await approval_service.schedule_continuation(approval_id)

    assert result.status is RunStatus.FAILED
    assert result.failure_state is not None
    assert result.failure_state.reason == "approval_rejected"


async def test_lease_is_cleared_after_schedule_continuation(sqlite_db) -> None:
    run_repo = RunRepository(sqlite_db)
    lease_manager = LeaseManager(sqlite_db)
    approval_service = _make_approval_service(sqlite_db)

    run = await _make_run(run_repo)
    # Claim the run (simulating worker ownership during initial execution).
    await lease_manager.claim_pending(DEPLOYMENT, "worker-old")
    await run_repo.transition(run.run_id, RunStatus.RUNNING)
    await run_repo.transition(run.run_id, RunStatus.WAITING_APPROVAL)
    # Manually clear lease (as the worker would after the approval pause).
    await lease_manager.release_lease(run.run_id, "worker-old")

    approval_record = ApprovalRecord(
        run_id=run.run_id,
        thread_id=run.thread_id,
        node_id="approval-node",
        graph_version_ref=run.graph_version_ref,
        deployment_ref=run.deployment_ref,
        tenant_id="default",
        summary="test approval",
        rationale="test rationale",
        allowed_actions=[ApprovalDecision.APPROVE],
    )
    await approval_service.repository.write(approval_record)
    approval_id = approval_record.approval_id

    await approval_service.resolve(approval_id, decision=ApprovalDecision.APPROVE, actor=_actor())
    await approval_service.schedule_continuation(approval_id)

    # New worker should be able to claim the PENDING run.
    claimed = await lease_manager.claim_pending(DEPLOYMENT, "worker-new")
    assert claimed == run.run_id
