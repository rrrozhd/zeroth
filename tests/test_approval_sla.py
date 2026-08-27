"""Tests for approval SLA enforcement: overdue queries, escalation, and SLA checker."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from zeroth.governance.approvals.models import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalResolution,
    ApprovalStatus,
)
from zeroth.governance.approvals.repository import ApprovalRepository
from zeroth.governance.approvals.service import ApprovalService
from zeroth.governance.identity import ActorIdentity, AuthMethod, ServiceRole
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.platform.storage import ScopeContext

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
    resolution: ApprovalResolution | None = None,
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
        resolution=resolution,
    )


def _resolution(decision: ApprovalDecision = ApprovalDecision.APPROVE) -> ApprovalResolution:
    """Build the payload a human decision leaves behind on a RESOLVED approval."""
    return ApprovalResolution(
        decision=decision,
        actor=ActorIdentity(subject="human-reviewer", auth_method=AuthMethod.API_KEY),
    )


# ---------------------------------------------------------------------------
# ApprovalRepository.list_overdue
# ---------------------------------------------------------------------------


class TestApprovalRepositoryListOverdue:
    """Tests for deployment-owner-local overdue approval reads."""

    @pytest.fixture
    async def stores(self, async_database):
        repository = ApprovalRepository.scoped_for_deployment(
            async_database,
            ScopeContext(tenant_id="tenant-1", workspace_id="ws-1"),
            "deploy-1",
        )
        return repository, repository

    async def test_returns_pending_past_deadline(self, stores):
        """list_overdue returns PENDING approvals whose sla_deadline is in the past."""
        past = datetime.now(UTC) - timedelta(minutes=5)
        record = _make_record(sla_deadline=past)
        repository, reader = stores
        await repository.write(record)

        overdue = await reader.list_overdue()
        assert len(overdue) == 1
        assert overdue[0].approval_id == record.approval_id

    async def test_excludes_resolved(self, stores):
        """list_overdue does NOT return RESOLVED approvals."""
        past = datetime.now(UTC) - timedelta(minutes=5)
        record = _make_record(
            status=ApprovalStatus.RESOLVED,
            sla_deadline=past,
        )
        repository, reader = stores
        await repository.write(record)

        overdue = await reader.list_overdue()
        assert len(overdue) == 0

    async def test_excludes_escalated(self, stores):
        """list_overdue does NOT return ESCALATED approvals."""
        past = datetime.now(UTC) - timedelta(minutes=5)
        record = _make_record(
            approval_id="appr-esc",
            status=ApprovalStatus.ESCALATED,
            sla_deadline=past,
        )
        repository, reader = stores
        await repository.write(record)

        overdue = await reader.list_overdue()
        assert len(overdue) == 0

    async def test_excludes_no_sla_deadline(self, stores):
        """list_overdue does NOT return approvals with sla_deadline=None."""
        record = _make_record(sla_deadline=None)
        repository, reader = stores
        await repository.write(record)

        overdue = await reader.list_overdue()
        assert len(overdue) == 0

    async def test_excludes_future_deadline(self, stores):
        """list_overdue does NOT return approvals whose deadline is in the future."""
        future = datetime.now(UTC) + timedelta(hours=1)
        record = _make_record(sla_deadline=future)
        repository, reader = stores
        await repository.write(record)

        overdue = await reader.list_overdue()
        assert len(overdue) == 0

    async def test_non_default_overdue_record_escalates_through_exact_scope(self, async_database):
        repository = ApprovalRepository(async_database)
        reader = ApprovalRepository.scoped_for_deployment(
            async_database,
            ScopeContext(tenant_id="tenant-1", workspace_id="ws-1"),
            "deploy-1",
        )
        service = ApprovalService(
            repository=repository,
            run_repository=AsyncMock(spec=RunRepository),
        )
        record = _make_record(
            approval_id="tenant-overdue",
            sla_deadline=datetime.now(UTC) - timedelta(minutes=5),
            escalation_action="alert",
        )
        await repository.write(record)

        overdue = await reader.list_overdue()
        assert overdue == [record]
        with pytest.raises(KeyError, match=record.approval_id):
            await service.escalate(
                record.approval_id,
                tenant_id=record.tenant_id,
                workspace_id="other-workspace",
                deployment_ref=record.deployment_ref,
                graph_version_ref=record.graph_version_ref,
            )
        escalated = await service.escalate(
            record.approval_id,
            tenant_id=record.tenant_id,
            workspace_id=record.workspace_id,
            deployment_ref=record.deployment_ref,
            graph_version_ref=record.graph_version_ref,
        )

        # Alert latch: the row stays PENDING (resolvable) but is fenced out of
        # the overdue sweep via a nulled deadline + the escalated marker.
        assert escalated.status is ApprovalStatus.PENDING
        assert escalated.sla_deadline is None
        assert escalated.urgency_metadata["escalated"] is True
        assert (
            await repository.get(
                record.approval_id,
                tenant_id=record.tenant_id,
                workspace_id=record.workspace_id,
            )
            == escalated
        )
        # And it is no longer overdue, so the checker will not re-escalate it.
        assert await reader.list_overdue() == []


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
        repo.resolve_pending = AsyncMock(side_effect=lambda r: r)
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
        claims = []
        repo._escalate_to_delegate = AsyncMock(
            side_effect=lambda original, delegate: (claims.append((original, delegate)), original)[
                1
            ]
        )

        result = await service.escalate(original.approval_id)

        assert result.status == ApprovalStatus.ESCALATED
        # The claim and the delegate go to the repository as one call, because
        # they are one transaction: a claim that committed without its delegate
        # would orphan the approval. The conditional PENDING -> ESCALATED
        # compare-and-set lives inside that call, so a concurrent resolution
        # still wins the row.
        assert len(claims) == 1
        claimed, delegate_record = claims[0]
        assert claimed.approval_id == original.approval_id
        assert claimed.status == ApprovalStatus.ESCALATED
        # No unconditional write of either row: the delegate is minted by the
        # same atomic call, never by a second trip to the database.
        assert writes == []
        repo.resolve_pending.assert_not_called()
        assert delegate_record.approval_id != original.approval_id
        assert delegate_record.status == ApprovalStatus.PENDING
        assert delegate_record.escalated_from_id == original.approval_id
        assert "[Escalated]" in delegate_record.summary

    async def test_auto_reject_resolves_as_rejected(self, service, repo, run_repo):
        """escalate with action=auto_reject resolves with REJECT and system actor."""
        original = _make_record(
            escalation_action="auto_reject",
            sla_deadline=datetime.now(UTC) - timedelta(minutes=5),
        )
        # `resolve` re-fetches, and the record it must find is still the
        # unresolved one: `service.resolve` raises "approval already resolved"
        # when the fetch returns a RESOLVED record. A discarded local built a
        # RESOLVED record here beside a comment claiming resolve would receive
        # it -- supplying it really does fail, so the comment described an
        # intent the service rejects. Returning `original` for both fetches is
        # the behaviour under test.
        repo.get = AsyncMock(return_value=original)
        repo.write = AsyncMock(side_effect=lambda r: r)

        result = await service.escalate(original.approval_id)

        assert result.status == ApprovalStatus.RESOLVED
        assert result.resolution is not None
        assert result.resolution.decision == ApprovalDecision.REJECT
        assert result.resolution.actor.subject == "sla_enforcer"
        assert result.resolution.actor.tenant_id == original.tenant_id
        assert result.resolution.actor.workspace_id == original.workspace_id

    async def test_alert_latches_pending_out_of_sla_sweep(self, service, repo):
        """escalate with action=alert keeps the row PENDING but latches it.

        An alert is a nudge, not a decision. Flipping the row to ESCALATED hides
        it from list/get and makes the run unresolvable forever; instead the
        alert nulls sla_deadline (so list_overdue stops matching) and sets
        urgency_metadata['escalated'] while leaving status PENDING so a human can
        still resolve it. (Renamed from test_alert_marks_escalated, which pinned
        the bug.)
        """
        original = _make_record(
            escalation_action="alert",
            sla_deadline=datetime.now(UTC) - timedelta(minutes=5),
        )
        repo.get = AsyncMock(return_value=original)
        repo.write = AsyncMock(side_effect=lambda r: r)

        result = await service.escalate(original.approval_id)

        assert result.status is ApprovalStatus.PENDING
        assert result.urgency_metadata["escalated"] is True
        assert result.sla_deadline is None

    async def test_alert_emits_one_correlated_event_only_for_the_cas_winner(
        self, service, repo
    ):
        """The approval transition, not the polling loop, owns escalation emission.

        Under the alert-latch design (0.23.13 line, kept at the 0.24.6 merge)
        the CAS winner's row deliberately STAYS ``PENDING`` -- an alert is a
        nudge, not a decision -- with ``sla_deadline`` nulled and the breached
        deadline preserved in ``urgency_metadata``. The property this test owns
        is unchanged: exactly one correlated event, emitted by the winning
        persisted transition only.
        """
        original = _make_record(
            escalation_action="alert",
            sla_deadline=datetime.now(UTC) - timedelta(minutes=5),
        )
        breached_deadline = original.sla_deadline.isoformat()
        # _claim_escalation mutates the record in place before resolve_pending,
        # so returning it IS the latched winner view; the second call loses the
        # CAS (None) and re-reads the already-latched row.
        repo.get = AsyncMock(side_effect=[original, original])
        repo.resolve_pending = AsyncMock(side_effect=[original, None])
        webhook_service = AsyncMock()
        webhook_service.emit_event = AsyncMock(return_value=[])
        service.webhook_service = webhook_service

        winner = await service.escalate(original.approval_id)
        loser = await service.escalate(original.approval_id)

        assert winner.status is ApprovalStatus.PENDING
        assert winner.sla_deadline is None
        assert winner.urgency_metadata.get("escalated") is True
        assert loser.status is ApprovalStatus.PENDING
        webhook_service.emit_event.assert_awaited_once()
        emitted = webhook_service.emit_event.await_args.kwargs
        assert emitted["event_type"] == "approval.escalated"
        assert emitted["data"] == {
            "approval_id": original.approval_id,
            "run_id": original.run_id,
            "thread_id": original.thread_id,
            "graph_version_ref": original.graph_version_ref,
            "node_id": original.node_id,
            "escalation_action": "alert",
            "sla_deadline": breached_deadline,
        }

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
        repo._escalate_to_delegate.assert_not_called()

    @pytest.mark.parametrize("action", ["delegate", "alert", "auto_reject"])
    async def test_resolved_is_never_reopened(self, service, repo, action):
        """escalate on a RESOLVED approval is a no-op for every escalation action.

        A decided approval must not be dragged back into the pending set by SLA
        enforcement: flipping its status would leave the surviving resolution
        payload contradicting the record, and ``delegate`` would additionally
        mint a second live approval for work a human already closed.
        """
        resolution = _resolution(ApprovalDecision.APPROVE)
        original = _make_record(
            status=ApprovalStatus.RESOLVED,
            escalation_action=action,
            sla_deadline=datetime.now(UTC) - timedelta(minutes=5),
            delegate_identity={"subject": "delegate-1", "auth_method": "api_key"},
            sla_timeout_seconds=300,
            resolution=resolution,
        )
        repo.get = AsyncMock(return_value=original)

        result = await service.escalate(original.approval_id)

        # The decision survives intact -- status and payload still agree.
        assert result.status == ApprovalStatus.RESOLVED
        assert result.resolution is not None
        assert result.resolution.decision == ApprovalDecision.APPROVE
        assert result.resolution.actor.subject == "human-reviewer"
        # No delegate approval, and no status write of any kind. The atomic
        # escalation is named explicitly: without it this assertion would keep
        # passing for the wrong reason once the delegate path stopped calling
        # ``write`` and ``resolve_pending``.
        repo.write.assert_not_called()
        repo.resolve_pending.assert_not_called()
        repo._escalate_to_delegate.assert_not_called()

    async def test_lost_escalation_race_writes_no_delegate(self, service, repo):
        """A checker that loses the compare-and-set must not mint a second delegate.

        The atomic escalation returns None when the stored row is no longer
        PENDING -- a concurrent SLA checker escalated it, or a human resolved it
        between our read and our write. Either way this caller has no claim on
        the approval, and because the delegate insert shares the losing
        transaction it is rolled back rather than left behind.
        """
        original = _make_record(
            escalation_action="delegate",
            sla_deadline=datetime.now(UTC) - timedelta(minutes=5),
            delegate_identity={"subject": "delegate-1", "auth_method": "api_key"},
            sla_timeout_seconds=300,
        )
        winner_view = _make_record(
            status=ApprovalStatus.ESCALATED,
            escalation_action="delegate",
            sla_deadline=original.sla_deadline,
        )
        # First read sees PENDING; the re-read after the lost CAS sees the
        # winner's committed ESCALATED row.
        repo.get = AsyncMock(side_effect=[original, winner_view])
        repo._escalate_to_delegate = AsyncMock(return_value=None)

        result = await service.escalate(original.approval_id)

        assert result.status == ApprovalStatus.ESCALATED
        repo.write.assert_not_called()
        repo.resolve_pending.assert_not_called()


# ---------------------------------------------------------------------------
# ApprovalService.escalate -- crash durability
# ---------------------------------------------------------------------------


class TestEscalateDelegateDurability:
    """The delegate escalation must survive a crash between its two writes.

    ``escalate`` claims the original (PENDING -> ESCALATED) and mints a delegate
    approval. Those two row changes are one governance fact: an approval whose
    SLA expired was handed to a delegate. Committing the claim separately from
    the delegate leaves a window where a crash strands the original as
    ESCALATED with no delegate -- and because ``escalate`` early-returns on
    ESCALATED, the SLA checker never retries it. The approval is then an orphan
    that no human will ever see again.

    Mocks cannot show this: only a real database distinguishes one transaction
    from two. These tests fail the delegate row write and assert the pair is
    all-or-nothing.
    """

    @pytest.fixture
    def delegate_record(self):
        return _make_record(
            approval_id="appr-durability",
            escalation_action="delegate",
            sla_deadline=datetime.now(UTC) - timedelta(minutes=5),
            delegate_identity={"subject": "delegate-1", "auth_method": "api_key"},
            sla_timeout_seconds=300,
        )

    @pytest.fixture
    def fail_delegate_row(self, monkeypatch):
        """Fail the INSERT of the delegate row, whichever method issues it.

        Patching at ``BoundStructuredTable.upsert`` rather than at
        ``ApprovalRepository.write`` keeps the injection valid across the fix:
        the delegate row is written by ``write`` before it and by the atomic
        escalation method after it, but it is the same upsert either way. A
        repository-method patch would silently stop firing once the call moved
        and the test would pass vacuously -- hence the call counter, which every
        test using this fixture asserts.
        """
        from zeroth.platform.storage.scoped_table import BoundStructuredTable

        original_upsert = BoundStructuredTable.upsert
        attempts: list[dict] = []

        async def failing_upsert(self, values, **kwargs):
            # The delegate row is the only approval row carrying a back-pointer
            # to the approval it was escalated from.
            if values.get("escalated_from_id"):
                attempts.append(values)
                raise RuntimeError("crash between the claim and the delegate write")
            return await original_upsert(self, values, **kwargs)

        monkeypatch.setattr(BoundStructuredTable, "upsert", failing_upsert)
        return attempts

    async def test_failed_delegate_write_leaves_no_orphan(
        self, async_database, delegate_record, fail_delegate_row
    ):
        """A crash before the delegate lands must not leave the original ESCALATED."""
        repository = ApprovalRepository(async_database)
        service = ApprovalService(
            repository=repository,
            run_repository=AsyncMock(spec=RunRepository),
        )
        await repository.write(delegate_record)

        with pytest.raises(RuntimeError, match="crash between the claim"):
            await service.escalate(
                delegate_record.approval_id,
                tenant_id=delegate_record.tenant_id,
                workspace_id=delegate_record.workspace_id,
                deployment_ref=delegate_record.deployment_ref,
                graph_version_ref=delegate_record.graph_version_ref,
            )

        # Guards against a vacuous pass: the failure must have been injected at
        # the delegate row write, not somewhere earlier.
        assert len(fail_delegate_row) == 1

        stored = await repository.get(
            delegate_record.approval_id,
            tenant_id=delegate_record.tenant_id,
            workspace_id=delegate_record.workspace_id,
        )
        assert stored is not None
        delegates = [
            record
            for record in await repository.list(
                tenant_id=delegate_record.tenant_id,
                workspace_id=delegate_record.workspace_id,
            )
            if record.escalated_from_id == delegate_record.approval_id
        ]

        # The invariant: the claim and the delegate are one fact, so the two
        # halves must agree. An ESCALATED original with no delegate is the
        # orphan -- nobody is holding this approval and nobody will retry it.
        assert not (stored.status is ApprovalStatus.ESCALATED and not delegates), (
            "orphan: original is ESCALATED but its delegate was never written"
        )
        # The delegate write failed, so the consistent outcome is that neither
        # half landed.
        assert stored.status is ApprovalStatus.PENDING
        assert delegates == []

    async def test_failed_delegate_write_leaves_the_approval_retryable(
        self, async_database, delegate_record, fail_delegate_row
    ):
        """After the crash the SLA checker must still see -- and be able to escalate -- it."""
        repository = ApprovalRepository(async_database)
        reader = ApprovalRepository.scoped_for_deployment(
            async_database,
            ScopeContext(
                tenant_id=delegate_record.tenant_id,
                workspace_id=delegate_record.workspace_id,
            ),
            delegate_record.deployment_ref,
        )
        service = ApprovalService(
            repository=repository,
            run_repository=AsyncMock(spec=RunRepository),
        )
        await repository.write(delegate_record)

        with pytest.raises(RuntimeError, match="crash between the claim"):
            await service.escalate(
                delegate_record.approval_id,
                tenant_id=delegate_record.tenant_id,
                workspace_id=delegate_record.workspace_id,
                deployment_ref=delegate_record.deployment_ref,
                graph_version_ref=delegate_record.graph_version_ref,
            )
        assert len(fail_delegate_row) == 1

        # ``list_overdue`` only returns PENDING rows, so this is the property
        # the orphan destroys: the next poll still picks the approval up.
        overdue = await reader.list_overdue()
        assert [record.approval_id for record in overdue] == [delegate_record.approval_id]

    async def test_retry_after_a_failed_delegate_write_escalates_cleanly(
        self, async_database, delegate_record, monkeypatch
    ):
        """The retry that follows the crash produces exactly one delegate."""
        from zeroth.platform.storage.scoped_table import BoundStructuredTable

        repository = ApprovalRepository(async_database)
        service = ApprovalService(
            repository=repository,
            run_repository=AsyncMock(spec=RunRepository),
        )
        await repository.write(delegate_record)

        original_upsert = BoundStructuredTable.upsert
        attempts: list[dict] = []

        async def failing_upsert(self, values, **kwargs):
            if values.get("escalated_from_id"):
                attempts.append(values)
                raise RuntimeError("crash between the claim and the delegate write")
            return await original_upsert(self, values, **kwargs)

        monkeypatch.setattr(BoundStructuredTable, "upsert", failing_upsert)
        with pytest.raises(RuntimeError, match="crash between the claim"):
            await service.escalate(
                delegate_record.approval_id,
                tenant_id=delegate_record.tenant_id,
                workspace_id=delegate_record.workspace_id,
                deployment_ref=delegate_record.deployment_ref,
                graph_version_ref=delegate_record.graph_version_ref,
            )
        assert len(attempts) == 1
        monkeypatch.setattr(BoundStructuredTable, "upsert", original_upsert)

        escalated = await service.escalate(
            delegate_record.approval_id,
            tenant_id=delegate_record.tenant_id,
            workspace_id=delegate_record.workspace_id,
            deployment_ref=delegate_record.deployment_ref,
            graph_version_ref=delegate_record.graph_version_ref,
        )

        assert escalated.status is ApprovalStatus.ESCALATED
        stored = await repository.list(
            tenant_id=delegate_record.tenant_id,
            workspace_id=delegate_record.workspace_id,
        )
        delegates = [
            record for record in stored if record.escalated_from_id == delegate_record.approval_id
        ]
        assert len(delegates) == 1
        assert delegates[0].status is ApprovalStatus.PENDING
        assert "[Escalated]" in delegates[0].summary

    async def test_atomic_escalation_still_loses_to_a_human_resolution(
        self, async_database, delegate_record
    ):
        """Making the pair atomic must not cost the claim its status precondition.

        An unconditional atomic write would trade the durability gap for a lost
        update -- strictly worse, because it would bury a decision a human
        already made. This drives the repository method directly with a stale
        PENDING view of a row that has since been resolved: the compare-and-set
        must match nothing, the delegate must not be minted, and the human's
        resolution must survive untouched.
        """
        repository = ApprovalRepository(async_database)
        await repository.write(delegate_record)
        resolved = delegate_record.model_copy(
            update={
                "status": ApprovalStatus.RESOLVED,
                "resolution": _resolution(ApprovalDecision.APPROVE),
                "updated_at": datetime.now(UTC),
            }
        )
        assert await repository.resolve_pending(resolved) is not None

        # What an SLA checker holds after reading the row a moment too early.
        stale_claim = delegate_record.model_copy(
            update={"status": ApprovalStatus.ESCALATED, "updated_at": datetime.now(UTC)}
        )
        delegate = _make_record(approval_id="appr-durability-delegate").model_copy(
            update={"escalated_from_id": delegate_record.approval_id}
        )

        assert await repository._escalate_to_delegate(stale_claim, delegate) is None

        stored = await repository.get(
            delegate_record.approval_id,
            tenant_id=delegate_record.tenant_id,
            workspace_id=delegate_record.workspace_id,
        )
        assert stored is not None
        assert stored.status is ApprovalStatus.RESOLVED
        assert stored.resolution is not None
        assert stored.resolution.decision is ApprovalDecision.APPROVE
        assert stored.resolution.actor.subject == "human-reviewer"
        assert [
            record
            for record in await repository.list(
                tenant_id=delegate_record.tenant_id,
                workspace_id=delegate_record.workspace_id,
            )
            if record.escalated_from_id == delegate_record.approval_id
        ] == []


# ---------------------------------------------------------------------------
# ApprovalSLAChecker
# ---------------------------------------------------------------------------


class TestApprovalSLAChecker:
    """Tests for the poll loop and webhook emission."""

    async def test_poll_loop_escalates_overdue(self):
        """poll_loop calls list_overdue and escalates each overdue approval."""
        from zeroth.governance.approvals.sla_checker import ApprovalSLAChecker

        overdue = _make_record(
            sla_deadline=datetime.now(UTC) - timedelta(minutes=5),
            escalation_action="alert",
        )

        service = AsyncMock(spec=ApprovalService)
        service.repository = AsyncMock(spec=ApprovalRepository)
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
        service.escalate.assert_called_with(
            overdue.approval_id,
            tenant_id=overdue.tenant_id,
            workspace_id=overdue.workspace_id,
            deployment_ref=overdue.deployment_ref,
            graph_version_ref=overdue.graph_version_ref,
        )

    async def test_auto_reject_schedules_the_resolved_run_for_worker_continuation(self):
        from zeroth.governance.approvals.sla_checker import ApprovalSLAChecker

        overdue = _make_record(
            sla_deadline=datetime.now(UTC) - timedelta(minutes=5),
            escalation_action="auto_reject",
        )
        auto_rejected = _make_record(
            status=ApprovalStatus.RESOLVED,
            escalation_action="auto_reject",
            resolution=ApprovalResolution(
                decision=ApprovalDecision.REJECT,
                actor=ActorIdentity(subject="sla_enforcer", auth_method=AuthMethod.API_KEY),
            ),
        )
        service = AsyncMock(spec=ApprovalService)
        service.repository = AsyncMock(spec=ApprovalRepository)
        service.repository.list_overdue = AsyncMock(return_value=[overdue])
        service.escalate = AsyncMock(return_value=auto_rejected)
        service.schedule_continuation = AsyncMock()
        checker = ApprovalSLAChecker(approval_service=service, poll_interval=0.1)

        task = asyncio.create_task(checker.poll_loop())
        await asyncio.sleep(0.03)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        service.schedule_continuation.assert_awaited_once_with(auto_rejected.approval_id)

    async def test_human_resolution_won_at_deadline_is_not_rescheduled_by_sla_checker(self):
        from zeroth.governance.approvals.sla_checker import ApprovalSLAChecker

        overdue = _make_record(
            sla_deadline=datetime.now(UTC) - timedelta(minutes=5),
            escalation_action="auto_reject",
        )
        human_resolved = _make_record(
            status=ApprovalStatus.RESOLVED,
            escalation_action="auto_reject",
            resolution=_resolution(ApprovalDecision.APPROVE),
        )
        service = AsyncMock(spec=ApprovalService)
        service.repository = AsyncMock(spec=ApprovalRepository)
        service.repository.list_overdue = AsyncMock(return_value=[overdue])
        service.escalate = AsyncMock(return_value=human_resolved)
        checker = ApprovalSLAChecker(approval_service=service, poll_interval=0.01)

        task = asyncio.create_task(checker.poll_loop())
        await asyncio.sleep(0.03)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        service.schedule_continuation.assert_not_awaited()

    async def test_escalation_event_is_owned_by_approval_service(self):
        """The checker requests the transition and never publishes a second event."""
        from zeroth.governance.approvals.sla_checker import ApprovalSLAChecker

        overdue = _make_record(
            sla_deadline=datetime.now(UTC) - timedelta(minutes=5),
            escalation_action="alert",
        )

        service = AsyncMock(spec=ApprovalService)
        service.repository = AsyncMock(spec=ApprovalRepository)
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

        service.escalate.assert_awaited()
        webhook_service.emit_event.assert_not_awaited()
