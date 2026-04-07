"""High-level service for the approval workflow.

Ties together the repository, run management, and audit logging to provide
a simple interface for creating approval requests, resolving them, and
resuming the paused agent run afterward.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from zeroth.approvals.models import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalResolution,
    ApprovalStatus,
)
from zeroth.approvals.repository import ApprovalRepository
from zeroth.audit import (
    ApprovalActionRecord,
    AuditRedactionConfig,
    AuditRepository,
    NodeAuditRecord,
    PayloadSanitizer,
)
from zeroth.graph import Graph, HumanApprovalNode
from zeroth.identity import ActorIdentity
from zeroth.runs import Run, RunFailureState, RunRepository, RunStatus

if TYPE_CHECKING:
    from zeroth.orchestrator.runtime import RuntimeOrchestrator


class ApprovalService:
    """The main entry point for working with approvals.

    Handles the full lifecycle: creating a pending approval when a workflow
    pauses, letting a human resolve it (approve / reject / edit-and-approve),
    and then resuming the run with the decision applied.
    """

    def __init__(
        self,
        *,
        repository: ApprovalRepository,
        run_repository: RunRepository,
        audit_repository: AuditRepository | None = None,
        payload_sanitizer: PayloadSanitizer | None = None,
    ) -> None:
        self.repository = repository
        self.run_repository = run_repository
        self.audit_repository = audit_repository
        self.payload_sanitizer = payload_sanitizer or PayloadSanitizer(
            AuditRedactionConfig(redact_keys={"secret", "token", "password"})
        )

    async def create_pending(
        self,
        *,
        run: Run,
        node: HumanApprovalNode,
        input_payload: dict[str, Any],
    ) -> ApprovalRecord:
        """Create a new approval request and save it to the database.

        Called when a workflow reaches a node that needs human sign-off. Builds
        the approval record from the run and node details, sanitizes sensitive
        data out of the payload, and persists it as "pending".
        """
        allow_edits = bool(node.human_approval.approval_policy_config.get("allow_edits"))
        allowed_actions = [ApprovalDecision.APPROVE, ApprovalDecision.REJECT]
        if allow_edits:
            allowed_actions.append(ApprovalDecision.EDIT_AND_APPROVE)
        record = ApprovalRecord(
            run_id=run.run_id,
            thread_id=run.thread_id,
            node_id=node.node_id,
            graph_version_ref=run.graph_version_ref,
            deployment_ref=run.deployment_ref,
            tenant_id=run.tenant_id,
            workspace_id=run.workspace_id,
            requested_by=run.submitted_by,
            allowed_actions=allowed_actions,
            summary=f"Approval required for node {node.node_id}",
            rationale="Human review is required before execution can continue.",
            context_excerpt=self.payload_sanitizer.sanitize(input_payload),
            proposed_payload=dict(input_payload),
            urgency_metadata=dict(node.human_approval.pause_behavior_config),
            resolution_schema_ref=node.human_approval.resolution_schema_ref,
        )
        # SLA deadline from node config (D-09)
        if node.human_approval.sla_timeout_seconds is not None:
            record.sla_deadline = record.created_at + timedelta(
                seconds=node.human_approval.sla_timeout_seconds
            )
            record.escalation_action = node.human_approval.escalation_action
        # Store delegate info and timeout in urgency_metadata for later escalation
        if node.human_approval.delegate_identity:
            record.urgency_metadata["delegate_identity"] = node.human_approval.delegate_identity
        if node.human_approval.sla_timeout_seconds:
            record.urgency_metadata["sla_timeout_seconds"] = node.human_approval.sla_timeout_seconds
        return await self.repository.write(record)

    async def get(self, approval_id: str) -> ApprovalRecord | None:
        """Fetch a single approval record by its ID. Returns None if not found."""
        return await self.repository.get(approval_id)

    async def list_pending(
        self,
        *,
        run_id: str | None = None,
        thread_id: str | None = None,
        deployment_ref: str | None = None,
    ) -> list[ApprovalRecord]:
        """Return all approvals that are still waiting for a human decision.

        Optionally filter by run_id, thread_id, or deployment_ref.
        """
        return await self.repository.list_pending(
            run_id=run_id,
            thread_id=thread_id,
            deployment_ref=deployment_ref,
        )

    async def list(
        self,
        *,
        run_id: str | None = None,
        thread_id: str | None = None,
        deployment_ref: str | None = None,
    ) -> list[ApprovalRecord]:
        """Return approval records for a run, thread, or deployment."""
        return await self.repository.list(
            run_id=run_id,
            thread_id=thread_id,
            deployment_ref=deployment_ref,
        )

    async def escalate(self, approval_id: str) -> ApprovalRecord:
        """Escalate an overdue approval based on its configured escalation action.

        Supports three actions:
        - delegate: marks original ESCALATED, creates new approval for delegate
        - auto_reject: resolves original as REJECTED with system actor
        - alert (default): marks original ESCALATED (webhook emission by caller)

        If the approval is already ESCALATED, this is a no-op (prevents double-escalation).
        """
        record = await self._require(approval_id)
        if record.status is ApprovalStatus.ESCALATED:
            return record  # already escalated, no-op per pitfall 3

        action = record.escalation_action or "alert"

        if action == "delegate":
            record.status = ApprovalStatus.ESCALATED
            record.updated_at = datetime.now(UTC)
            await self.repository.write(record)

            delegate_identity_dict = record.urgency_metadata.get("delegate_identity")
            delegate_actor = (
                ActorIdentity(**delegate_identity_dict)
                if isinstance(delegate_identity_dict, dict)
                else None
            )
            delegate_record = ApprovalRecord(
                run_id=record.run_id,
                thread_id=record.thread_id,
                node_id=record.node_id,
                graph_version_ref=record.graph_version_ref,
                deployment_ref=record.deployment_ref,
                tenant_id=record.tenant_id,
                workspace_id=record.workspace_id,
                requested_by=delegate_actor or record.requested_by,
                interaction_type=record.interaction_type,
                allowed_actions=list(record.allowed_actions),
                summary=f"[Escalated] {record.summary}",
                rationale=f"Escalated from approval {record.approval_id} due to SLA timeout",
                context_excerpt=dict(record.context_excerpt),
                proposed_payload=dict(record.proposed_payload) if record.proposed_payload else None,
                urgency_metadata=dict(record.urgency_metadata),
                resolution_schema_ref=record.resolution_schema_ref,
                escalated_from_id=record.approval_id,
            )
            timeout_seconds = record.urgency_metadata.get("sla_timeout_seconds")
            if timeout_seconds:
                delegate_record.sla_deadline = delegate_record.created_at + timedelta(
                    seconds=timeout_seconds
                )
                delegate_record.escalation_action = record.escalation_action
            await self.repository.write(delegate_record)
            return record

        elif action == "auto_reject":
            from zeroth.identity import AuthMethod

            system_actor = ActorIdentity(
                subject="sla_enforcer",
                auth_method=AuthMethod.API_KEY,
            )
            return await self.resolve(
                approval_id,
                decision=ApprovalDecision.REJECT,
                actor=system_actor,
            )

        else:  # "alert" or unknown
            record.status = ApprovalStatus.ESCALATED
            record.updated_at = datetime.now(UTC)
            return await self.repository.write(record)

    async def resolve(
        self,
        approval_id: str,
        *,
        decision: ApprovalDecision,
        actor: ActorIdentity,
        edited_payload: dict[str, Any] | None = None,
    ) -> ApprovalRecord:
        """Record a human's decision on a pending approval.

        Validates that the decision is allowed, marks the approval as resolved,
        and writes an audit log entry. If the same exact decision was already
        recorded, this is treated as a safe no-op (idempotent).

        Raises ValueError if the approval is already resolved with a different
        decision, or if the chosen decision is not in the allowed actions list.
        """
        record = await self._require(approval_id)
        if record.status is ApprovalStatus.RESOLVED:
            current = record.resolution
            if current is None:
                raise ValueError("approval is resolved without resolution payload")
            if (
                current.decision is decision
                and current.actor == actor
                and current.edited_payload == edited_payload
            ):
                return record
            raise ValueError("approval already resolved")
        if decision not in record.allowed_actions:
            raise ValueError(f"decision {decision.value} is not allowed")
        if decision is ApprovalDecision.EDIT_AND_APPROVE and edited_payload is None:
            raise ValueError("edited payload is required for edit_and_approve")

        record.status = ApprovalStatus.RESOLVED
        record.resolution = ApprovalResolution(
            decision=decision,
            actor=actor,
            edited_payload=edited_payload,
        )
        record.updated_at = datetime.now(UTC)
        resolved = await self.repository.write(record)
        await self._record_api_audit(resolved)
        return resolved

    async def schedule_continuation(self, approval_id: str) -> Run:
        """Prepare a resolved approval for durable worker pick-up.

        Instead of driving the orchestrator inline (which conflicts with the
        worker-ownership model), this method:
          1. Prepares the run state (as continue_run would before calling the orchestrator).
          2. Transitions the run to PENDING so the worker's poll loop will claim it.
          3. Clears the lease so any worker can pick it up.

        The worker will call ``resume_graph`` on the next poll tick.
        Only call this from the approval HTTP endpoint when the durable worker is active.
        """
        record = await self._require(approval_id)
        if record.status is not ApprovalStatus.RESOLVED or record.resolution is None:
            raise ValueError("approval must be resolved before continuation")
        run = await self.run_repository.get(record.run_id)
        if run is None:
            raise KeyError(record.run_id)

        decision = record.resolution.decision
        if decision is ApprovalDecision.REJECT:
            run.failure_state = RunFailureState(
                reason="approval_rejected", message="approval rejected"
            )
            run.status = RunStatus.FAILED
            run.touch()
            persisted = await self.run_repository.put(run)
            await self._record_decision_audit(record, run, status="rejected", output_payload={})
            return persisted

        # Prepare run state so the worker can resume from the approval node.
        run.metadata.pop("pending_approval", None)
        run.pending_approval = None
        run.current_node_ids = [record.node_id]
        run.current_step = record.node_id

        # Store the resolved payload for the orchestrator to pick up after claiming.
        if decision is ApprovalDecision.EDIT_AND_APPROVE and record.resolution.edited_payload:
            run.metadata["approval_resolved_payload"] = record.resolution.edited_payload
        else:
            run.metadata["approval_resolved_payload"] = record.proposed_payload or {}
        run.metadata["approval_resolved_id"] = approval_id

        run.touch()
        await self.run_repository.put(run)
        # WAITING_APPROVAL -> PENDING so the worker's poll loop will re-claim it.
        run = await self.run_repository.transition(record.run_id, RunStatus.PENDING)
        return run

    async def continue_run(
        self,
        approval_id: str,
        *,
        graph: Graph,
        orchestrator: RuntimeOrchestrator,
    ) -> Run:
        """Resume a paused workflow run after an approval has been resolved.

        If the decision was REJECT, the run is marked as failed immediately.
        If APPROVE or EDIT_AND_APPROVE, the run is handed back to the
        orchestrator to continue executing from the approval node onward.
        """
        record = await self._require(approval_id)
        if record.status is not ApprovalStatus.RESOLVED or record.resolution is None:
            raise ValueError("approval must be resolved before continuation")
        run = await self.run_repository.get(record.run_id)
        if run is None:
            raise KeyError(record.run_id)

        node = next(
            node
            for node in graph.nodes
            if node.node_id == record.node_id and isinstance(node, HumanApprovalNode)
        )
        run.metadata.pop("pending_approval", None)
        run.pending_approval = None
        run.current_node_ids = [record.node_id]
        run.current_step = record.node_id
        run.status = RunStatus.RUNNING

        decision = record.resolution.decision
        if decision is ApprovalDecision.REJECT:
            run.failure_state = RunFailureState(
                reason="approval_rejected", message="approval rejected"
            )
            run.status = RunStatus.FAILED
            run.touch()
            persisted = await self.run_repository.put(run)
            await self._record_decision_audit(record, run, status="rejected", output_payload={})
            return persisted

        output_payload = record.proposed_payload or {}
        if decision is ApprovalDecision.EDIT_AND_APPROVE:
            output_payload = record.resolution.edited_payload or {}

        await orchestrator.record_approval_resolution(
            graph=graph,
            run=run,
            node=node,
            output_payload=output_payload,
            approval_record=record,
        )
        return await orchestrator.resume_graph(graph, run.run_id)

    async def _require(self, approval_id: str) -> ApprovalRecord:
        """Fetch an approval record by ID, raising KeyError if it does not exist."""
        record = await self.repository.get(approval_id)
        if record is None:
            raise KeyError(approval_id)
        return record

    async def _record_api_audit(self, record: ApprovalRecord) -> None:
        """Write an audit log entry for the API-level approval resolution."""
        if self.audit_repository is None or record.resolution is None:
            return
        await self.audit_repository.write(
            NodeAuditRecord(
                audit_id=f"approval-api:{record.approval_id}:{record.resolution.decision.value}",
                run_id=record.run_id,
                thread_id=record.thread_id,
                node_id=record.node_id,
                node_version=1,
                graph_version_ref=record.graph_version_ref,
                deployment_ref=record.deployment_ref,
                tenant_id=record.tenant_id,
                workspace_id=record.workspace_id,
                attempt=1,
                status="approval_api",
                actor=record.resolution.actor,
                execution_metadata={"resolution": record.resolution.model_dump(mode="json")},
                approval_actions=[
                    ApprovalActionRecord(
                        approval_id=record.approval_id,
                        action=record.resolution.decision.value,
                        actor=record.resolution.actor,
                    )
                ],
            )
        )

    async def _record_decision_audit(
        self,
        record: ApprovalRecord,
        run: Run,
        *,
        status: str,
        output_payload: dict[str, Any],
    ) -> None:
        """Write an audit log entry capturing the decision outcome and payload snapshots."""
        if self.audit_repository is None or record.resolution is None:
            return
        await self.audit_repository.write(
            NodeAuditRecord(
                audit_id=f"{run.run_id}:audit:{len(run.audit_refs) + 1}",
                run_id=run.run_id,
                thread_id=run.thread_id,
                node_id=record.node_id,
                node_version=1,
                graph_version_ref=record.graph_version_ref,
                deployment_ref=record.deployment_ref,
                tenant_id=record.tenant_id,
                workspace_id=record.workspace_id,
                attempt=1,
                status=status,
                actor=record.resolution.actor,
                input_snapshot=record.proposed_payload or {},
                output_snapshot=output_payload,
                approval_actions=[
                    ApprovalActionRecord(
                        approval_id=record.approval_id,
                        action=record.resolution.decision.value,
                        actor=record.resolution.actor,
                    )
                ],
            )
        )
