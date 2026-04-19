"""Tests for approval SLA enforcement: overdue queries, escalation, and SLA checker."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from zeroth.core.approvals.models import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalStatus,
)
from zeroth.core.approvals.repository import ApprovalRepository
from zeroth.core.approvals.service import ApprovalService
from zeroth.core.identity import ActorIdentity, AuthMethod, ServiceRole
from zeroth.core.runs import RunRepository

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_record(
    *,
    approval_id: str = "appr-1",
    status: ApprovalStatus = ApprovalStatus.PENDING,
    sla_deadline: datetime | None = None,
    escalation_action: str | None = None,
    delegate_identity: dict | None = None,
    sla_timeout_seconds: int | None = None,
) -> ApprovalRecord:
    urgency = {}
    if delegate_identity:
        urgency["delegate_identity"] = delegate_identity
    if sla_timeout_seconds is not None:
        urgency["sla_timeout_seconds"] = sla_timeout_seconds
    return ApprovalRecord(
        approval_id=approval_id,
        run_id="run-1",
        thread_id="thread-1",
        node_id="node-1",
        graph_version_ref="graph-v1",
        deployment_ref="deploy-1",
        tenant_id="tenant-1",
        workspace_id="ws-1",
        summary="Test approval",
        rationale="Test rationale",
        allowed_actions=[ApprovalDecision.APPROVE, ApprovalDecision.REJECT],
        status=status,
        sla_deadline=sla_deadline,
        escalation_action=escalation_action,
        urgency_metadata=urgency,
    )


# ---------------------------------------------------------------------------
# ApprovalRepository.list_overdue
# ---------------------------------------------------------------------------


class TestApprovalRepositoryListOverdue:
    """Tests for the list_overdue query on ApprovalRepository."""

    @pytest.fixture
    async def repo(self, async_database):
        return ApprovalRepository(async_database)

    async def test_returns_pending_past_deadline(self, repo):
        """list_overdue returns PENDING approvals whose sla_deadline is in the past."""
        past = datetime.now(UTC) - timedelta(minutes=5)
        record = _make_record(sla_deadline=past)
        await repo.write(record)

        overdue = await repo.list_overdue()
        assert len(overdue) == 1
        assert overdue[0].approval_id == record.approval_id

    async def test_excludes_resolved(self, repo):
        """list_overdue does NOT return RESOLVED approvals."""
        past = datetime.now(UTC) - timedelta(minutes=5)
        record = _make_record(
            status=ApprovalStatus.RESOLVED,
            sla_deadline=past,
        )
        await repo.write(record)

        overdue = await repo.list_overdue()
        assert len(overdue) == 0

    async def test_excludes_escalated(self, repo):
        """list_overdue does NOT return ESCALATED approvals."""
        past = datetime.now(UTC) - timedelta(minutes=5)
        record = _make_record(
            approval_id="appr-esc",
            status=ApprovalStatus.ESCALATED,
            sla_deadline=past,
        )
        await repo.write(record)

        overdue = await repo.list_overdue()
        assert len(overdue) == 0

    async def test_excludes_no_sla_deadline(self, repo):
        """list_overdue does NOT return approvals with sla_deadline=None."""
        record = _make_record(sla_deadline=None)
        await repo.write(record)

        overdue = await repo.list_overdue()
        assert len(overdue) == 0

    async def test_excludes_future_deadline(self, repo):
        """list_overdue does NOT return approvals whose deadline is in the future."""
        future = datetime.now(UTC) + timedelta(hours=1)
        record = _make_record(sla_deadline=future)
        await repo.write(record)

        overdue = await repo.list_overdue()
        assert len(overdue) == 0


# ---------------------------------------------------------------------------
# ApprovalService.create_pending SLA fields
# ---------------------------------------------------------------------------


class TestApprovalServiceCreatePendingSLA:
    """Tests for SLA-aware create_pending."""

    @pytest.fixture
    def repo(self):
        repo = AsyncMock(spec=ApprovalRepository)
        repo.write = AsyncMock(side_effect=lambda r: r)
        return repo

    @pytest.fixture
    def run_repo(self):
        return AsyncMock(spec=RunRepository)

    @pytest.fixture
    def service(self, repo, run_repo):
        return ApprovalService(repository=repo, run_repository=run_repo)

    def _make_run(self):
        run = MagicMock()
        run.run_id = "run-1"
        run.thread_id = "thread-1"
        run.graph_version_ref = "graph-v1"
        run.deployment_ref = "deploy-1"
        run.tenant_id = "tenant-1"
        run.workspace_id = "ws-1"
        run.submitted_by = ActorIdentity(
            subject="user-1",
            auth_method=AuthMethod.API_KEY,
            roles=[ServiceRole.REVIEWER],
            tenant_id="default",
        )
        return run

    def _make_node(self, sla_timeout_seconds=None, escalation_action=None, delegate_identity=None):
        node = MagicMock()
        node.node_id = "node-1"
        node.human_approval.approval_policy_config = {}
        node.human_approval.pause_behavior_config = {}
        node.human_approval.resolution_schema_ref = None
        node.human_approval.sla_timeout_seconds = sla_timeout_seconds
        node.human_approval.escalation_action = escalation_action
        node.human_approval.delegate_identity = delegate_identity
        return node

    async def test_sla_timeout_sets_deadline(self, service, repo):
        """create_pending with sla_timeout_seconds=300 sets sla_deadline."""
        run = self._make_run()
        node = self._make_node(sla_timeout_seconds=300, escalation_action="alert")

        record = await service.create_pending(run=run, node=node, input_payload={"key": "val"})

        assert record.sla_deadline is not None
        expected_delta = timedelta(seconds=300)
        actual_delta = record.sla_deadline - record.created_at
        assert abs(actual_delta - expected_delta) < timedelta(seconds=2)
        assert record.escalation_action == "alert"

    async def test_no_sla_timeout_leaves_none(self, service, repo):
        """create_pending with sla_timeout_seconds=None leaves sla_deadline=None."""
        run = self._make_run()
        node = self._make_node(sla_timeout_seconds=None)

        record = await service.create_pending(run=run, node=node, input_payload={"key": "val"})

        assert record.sla_deadline is None

    async def test_delegate_identity_stored_in_urgency(self, service, repo):
        """create_pending stores delegate_identity in urgency_metadata."""
        run = self._make_run()
        delegate = {"subject": "delegate-1", "auth_method": "api_key"}
        node = self._make_node(
            sla_timeout_seconds=600,
            escalation_action="delegate",
            delegate_identity=delegate,
        )

        record = await service.create_pending(run=run, node=node, input_payload={})

        assert record.urgency_metadata.get("delegate_identity") == delegate
        assert record.urgency_metadata.get("sla_timeout_seconds") == 600


# ---------------------------------------------------------------------------
# ApprovalService.escalate
# ---------------------------------------------------------------------------


class TestApprovalServiceEscalate:
    """Tests for the escalate method."""

    @pytest.fixture
    def repo(self):
        repo = AsyncMock(spec=ApprovalRepository)
        repo.write = AsyncMock(side_effect=lambda r: r)
        return repo

    @pytest.fixture
    def run_repo(self):
        return AsyncMock(spec=RunRepository)

    @pytest.fixture
    def service(self, repo, run_repo):
        return ApprovalService(repository=repo, run_repository=run_repo)

    async def test_delegate_creates_new_record(self, service, repo):
        """escalate with action=delegate creates a new approval for the delegate."""
        delegate = {"subject": "delegate-1", "auth_method": "api_key"}
        original = _make_record(
            escalation_action="delegate",
            sla_deadline=datetime.now(UTC) - timedelta(minutes=5),
            delegate_identity=delegate,
            sla_timeout_seconds=300,
        )
        repo.get = AsyncMock(return_value=original)
        writes = []
        repo.write = AsyncMock(side_effect=lambda r: (writes.append(r), r)[1])

        result = await service.escalate(original.approval_id)

        assert result.status == ApprovalStatus.ESCALATED
        # Should have written original (ESCALATED) + delegate record
        assert len(writes) == 2
        delegate_record = writes[1]
        assert delegate_record.escalated_from_id == original.approval_id
        assert "[Escalated]" in delegate_record.summary

    async def test_auto_reject_resolves_as_rejected(self, service, repo, run_repo):
        """escalate with action=auto_reject resolves with REJECT and system actor."""
        original = _make_record(
            escalation_action="auto_reject",
            sla_deadline=datetime.now(UTC) - timedelta(minutes=5),
        )
        repo.get = AsyncMock(return_value=original)

        # resolve needs to re-fetch the record
        resolved_record = _make_record(
            status=ApprovalStatus.RESOLVED,
            sla_deadline=original.sla_deadline,
            escalation_action="auto_reject",
        )
        # First call to _require (via escalate), second call within resolve
        repo.get = AsyncMock(return_value=original)
        repo.write = AsyncMock(side_effect=lambda r: r)

        result = await service.escalate(original.approval_id)

        assert result.status == ApprovalStatus.RESOLVED
        assert result.resolution is not None
        assert result.resolution.decision == ApprovalDecision.REJECT
        assert result.resolution.actor.subject == "sla_enforcer"

    async def test_alert_marks_escalated(self, service, repo):
        """escalate with action=alert marks as ESCALATED."""
        original = _make_record(
            escalation_action="alert",
            sla_deadline=datetime.now(UTC) - timedelta(minutes=5),
        )
        repo.get = AsyncMock(return_value=original)
        repo.write = AsyncMock(side_effect=lambda r: r)

        result = await service.escalate(original.approval_id)

        assert result.status == ApprovalStatus.ESCALATED

    async def test_already_escalated_is_noop(self, service, repo):
        """escalate on ESCALATED approval is a no-op."""
        original = _make_record(
            status=ApprovalStatus.ESCALATED,
            escalation_action="alert",
            sla_deadline=datetime.now(UTC) - timedelta(minutes=5),
        )
        repo.get = AsyncMock(return_value=original)

        result = await service.escalate(original.approval_id)

        assert result.status == ApprovalStatus.ESCALATED
        repo.write.assert_not_called()


# ---------------------------------------------------------------------------
# ApprovalSLAChecker
# ---------------------------------------------------------------------------


class TestApprovalSLAChecker:
    """Tests for the poll loop and webhook emission."""

    async def test_poll_loop_escalates_overdue(self):
        """poll_loop calls list_overdue and escalates each overdue approval."""
        from zeroth.core.approvals.sla_checker import ApprovalSLAChecker

        overdue = _make_record(
            sla_deadline=datetime.now(UTC) - timedelta(minutes=5),
            escalation_action="alert",
        )

        service = AsyncMock(spec=ApprovalService)
        service.repository = AsyncMock()
        service.repository.list_overdue = AsyncMock(return_value=[overdue])
        escalated_record = _make_record(status=ApprovalStatus.ESCALATED)
        service.escalate = AsyncMock(return_value=escalated_record)

        checker = ApprovalSLAChecker(
            approval_service=service,
            poll_interval=0.01,
        )

        # Run one iteration then cancel
        task = asyncio.create_task(checker.poll_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        service.repository.list_overdue.assert_called()
        service.escalate.assert_called_with(overdue.approval_id)

    async def test_emits_webhook_event(self):
        """SLA checker emits approval.escalated webhook event via optional WebhookService."""
        from zeroth.core.approvals.sla_checker import ApprovalSLAChecker

        overdue = _make_record(
            sla_deadline=datetime.now(UTC) - timedelta(minutes=5),
            escalation_action="alert",
        )

        service = AsyncMock(spec=ApprovalService)
        service.repository = AsyncMock()
        service.repository.list_overdue = AsyncMock(return_value=[overdue])
        escalated_record = _make_record(
            status=ApprovalStatus.ESCALATED,
            escalation_action="alert",
            sla_deadline=datetime.now(UTC) - timedelta(minutes=5),
        )
        service.escalate = AsyncMock(return_value=escalated_record)

        webhook_service = AsyncMock()
        webhook_service.emit_event = AsyncMock(return_value=[])

        checker = ApprovalSLAChecker(
            approval_service=service,
            webhook_service=webhook_service,
            poll_interval=0.01,
        )

        task = asyncio.create_task(checker.poll_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        webhook_service.emit_event.assert_called()
        call_kwargs = webhook_service.emit_event.call_args.kwargs
        assert call_kwargs["event_type"] == "approval.escalated"
        assert call_kwargs["deployment_ref"] == escalated_record.deployment_ref
