"""Tests for webhook event emission from orchestrator and approval service.

Validates that run completion/failure and approval creation/resolution
trigger webhook events via the optional WebhookService.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zeroth.approvals.models import ApprovalDecision, ApprovalStatus
from zeroth.identity import ActorIdentity, AuthMethod, ServiceRole
from zeroth.orchestrator.runtime import RuntimeOrchestrator
from zeroth.runs import Run, RunRepository, RunStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_orchestrator(webhook_service=None) -> RuntimeOrchestrator:
    """Build a minimal RuntimeOrchestrator with mocked dependencies."""
    run_repo = AsyncMock(spec=RunRepository)
    orch = RuntimeOrchestrator(
        run_repository=run_repo,
        agent_runners={},
        executable_unit_runner=MagicMock(),
    )
    if webhook_service is not None:
        orch.webhook_service = webhook_service
    return orch


def _make_run(status=RunStatus.RUNNING) -> Run:
    """Build a minimal Run object for testing."""
    run = MagicMock(spec=Run)
    run.run_id = "run-1"
    run.graph_version_ref = "graph:v1"
    run.deployment_ref = "deploy-1"
    run.tenant_id = "tenant-1"
    run.status = status
    run.failure_state = None
    run.pending_node_ids = []
    run.current_node_ids = []
    run.final_output = None
    run.metadata = {}
    run.execution_history = []
    run.audit_refs = []
    run.node_visit_counts = {}
    run.completed_steps = []
    run.condition_results = []
    return run


# ---------------------------------------------------------------------------
# Orchestrator webhook emission
# ---------------------------------------------------------------------------


class TestOrchestratorWebhookEmission:
    """Tests for webhook events emitted from orchestrator _drive loop."""

    async def test_emits_run_completed(self):
        """Orchestrator emits run.completed webhook when run finishes successfully."""
        webhook_service = AsyncMock()
        webhook_service.emit_event = AsyncMock(return_value=[])
        orch = _make_orchestrator(webhook_service=webhook_service)

        run = _make_run()
        run.pending_node_ids = []  # No more nodes means completion
        run.metadata = {"last_output": {"result": "ok"}}

        # Mock the run_repository to return the run on put
        orch.run_repository.put = AsyncMock(return_value=run)
        orch.run_repository.write_checkpoint = AsyncMock()

        # Call _drive which should complete the run and emit event
        result = await orch._drive(MagicMock(execution_settings=MagicMock(max_total_steps=100, max_total_runtime_seconds=None)), run)

        assert result.status == RunStatus.COMPLETED
        webhook_service.emit_event.assert_called_once()
        call_kwargs = webhook_service.emit_event.call_args.kwargs
        assert call_kwargs["event_type"] == "run.completed"
        assert call_kwargs["deployment_ref"] == "deploy-1"
        assert call_kwargs["tenant_id"] == "tenant-1"
        assert call_kwargs["data"]["run_id"] == "run-1"

    async def test_emits_run_failed(self):
        """Orchestrator emits run.failed webhook when run fails."""
        webhook_service = AsyncMock()
        webhook_service.emit_event = AsyncMock(return_value=[])
        orch = _make_orchestrator(webhook_service=webhook_service)

        run = _make_run()

        orch.run_repository.put = AsyncMock(return_value=run)
        orch.run_repository.write_checkpoint = AsyncMock()

        # Call _fail_run directly
        run.status = RunStatus.FAILED
        result = await orch._fail_run(run, "test_failure", "something broke")

        webhook_service.emit_event.assert_called_once()
        call_kwargs = webhook_service.emit_event.call_args.kwargs
        assert call_kwargs["event_type"] == "run.failed"
        assert call_kwargs["data"]["failure_reason"] == "test_failure"

    async def test_no_webhook_service_no_error(self):
        """Orchestrator works fine without a webhook_service attribute."""
        orch = _make_orchestrator(webhook_service=None)
        run = _make_run()
        run.pending_node_ids = []
        run.metadata = {"last_output": {}}

        orch.run_repository.put = AsyncMock(return_value=run)
        orch.run_repository.write_checkpoint = AsyncMock()

        result = await orch._drive(MagicMock(execution_settings=MagicMock(max_total_steps=100, max_total_runtime_seconds=None)), run)
        assert result.status == RunStatus.COMPLETED


# ---------------------------------------------------------------------------
# Approval webhook emission
# ---------------------------------------------------------------------------


class TestApprovalWebhookEmission:
    """Tests for webhook events emitted from approval create_pending and resolve."""

    async def test_approval_created_emits_event(self):
        """create_pending emits approval.requested webhook."""
        from zeroth.approvals.service import ApprovalService
        from zeroth.approvals.repository import ApprovalRepository

        repo = AsyncMock(spec=ApprovalRepository)
        run_repo = AsyncMock(spec=RunRepository)

        # Make write return the record passed to it
        repo.write = AsyncMock(side_effect=lambda r: r)

        service = ApprovalService(repository=repo, run_repository=run_repo)
        webhook_service = AsyncMock()
        webhook_service.emit_event = AsyncMock(return_value=[])
        service.webhook_service = webhook_service

        run = MagicMock()
        run.run_id = "run-1"
        run.thread_id = "thread-1"
        run.graph_version_ref = "graph:v1"
        run.deployment_ref = "deploy-1"
        run.tenant_id = "tenant-1"
        run.workspace_id = "ws-1"
        run.submitted_by = ActorIdentity(
            subject="user-1", auth_method=AuthMethod.API_KEY
        )

        node = MagicMock()
        node.node_id = "node-1"
        node.human_approval.approval_policy_config = {}
        node.human_approval.pause_behavior_config = {}
        node.human_approval.resolution_schema_ref = None
        node.human_approval.sla_timeout_seconds = None
        node.human_approval.escalation_action = None
        node.human_approval.delegate_identity = None

        await service.create_pending(run=run, node=node, input_payload={"key": "val"})

        webhook_service.emit_event.assert_called_once()
        call_kwargs = webhook_service.emit_event.call_args.kwargs
        assert call_kwargs["event_type"] == "approval.requested"
        assert call_kwargs["data"]["run_id"] == "run-1"

    async def test_approval_resolved_emits_event(self):
        """resolve emits approval.resolved webhook."""
        from zeroth.approvals.models import ApprovalRecord
        from zeroth.approvals.service import ApprovalService
        from zeroth.approvals.repository import ApprovalRepository

        repo = AsyncMock(spec=ApprovalRepository)
        run_repo = AsyncMock(spec=RunRepository)

        record = ApprovalRecord(
            run_id="run-1",
            node_id="node-1",
            graph_version_ref="graph:v1",
            deployment_ref="deploy-1",
            tenant_id="tenant-1",
            summary="Test",
            rationale="Test",
            allowed_actions=[ApprovalDecision.APPROVE, ApprovalDecision.REJECT],
        )
        repo.get = AsyncMock(return_value=record)
        repo.write = AsyncMock(side_effect=lambda r: r)

        service = ApprovalService(repository=repo, run_repository=run_repo)
        webhook_service = AsyncMock()
        webhook_service.emit_event = AsyncMock(return_value=[])
        service.webhook_service = webhook_service

        actor = ActorIdentity(subject="reviewer-1", auth_method=AuthMethod.API_KEY)
        await service.resolve(
            record.approval_id,
            decision=ApprovalDecision.APPROVE,
            actor=actor,
        )

        webhook_service.emit_event.assert_called_once()
        call_kwargs = webhook_service.emit_event.call_args.kwargs
        assert call_kwargs["event_type"] == "approval.resolved"
        assert call_kwargs["data"]["decision"] == "approve"


# ---------------------------------------------------------------------------
# ServiceBootstrap wiring
# ---------------------------------------------------------------------------


class TestServiceBootstrapWebhookWiring:
    """Tests that ServiceBootstrap wires webhook and SLA components."""

    async def test_bootstrap_has_webhook_fields(self):
        """ServiceBootstrap dataclass includes webhook-related fields."""
        from zeroth.service.bootstrap import ServiceBootstrap

        fields = {f.name for f in ServiceBootstrap.__dataclass_fields__.values()}
        assert "webhook_service" in fields
        assert "webhook_repository" in fields
        assert "delivery_worker" in fields
        assert "sla_checker" in fields
        assert "webhook_http_client" in fields
