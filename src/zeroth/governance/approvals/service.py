"""High-level service for the approval workflow.

Ties together the repository, run management, and audit logging to provide
a simple interface for creating approval requests, resolving them, and
resuming the paused agent run afterward.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from zeroth.contracts.governed import RunStatus
from zeroth.contracts.graph import Graph, HumanApprovalNode
from zeroth.governance.approvals.models import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalResolution,
    ApprovalStatus,
)
from zeroth.governance.approvals.notifications import ApprovalNotification, Notifier
from zeroth.governance.approvals.repository import _UNSCOPED, ApprovalRepository
from zeroth.governance.audit import (
    ApprovalActionRecord,
    AuditRedactionConfig,
    AuditRepository,
    NodeAuditRecord,
    PayloadSanitizer,
)
from zeroth.governance.identity import ActorIdentity
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.runtime.runs import Run, RunFailureState

logger = logging.getLogger(__name__)


class ApprovalContinuation(Protocol):
    """The slice of the run orchestrator the approval workflow drives.

    Structural, so the runtime orchestrator satisfies it without the
    approvals domain importing the orchestrator implementation.
    """

    async def record_approval_resolution(
        self,
        *,
        graph: Graph,
        run: Run,
        node: HumanApprovalNode,
        output_payload: Mapping[str, Any],
        approval_record: ApprovalRecord,
    ) -> Run:
        """Record the result of a human approval decision on the run."""
        ...

    async def resume_graph(self, graph: Graph, run_id: str) -> Run:
        """Resume the paused run from its current node onward."""
        ...


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
        self.webhook_service: object | None = None
        self.notifier: Notifier | None = None

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
        sanitized_payload = self.payload_sanitizer.sanitize(input_payload)
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
            context_excerpt=sanitized_payload,
            proposed_payload=sanitized_payload,
            urgency_metadata=dict(node.human_approval.pause_behavior_config),
            resolution_schema_ref=node.human_approval.resolution_schema_ref,
        )
        # 'escalated' / 'escalated_sla_deadline' are a SERVICE-OWNED alert latch,
        # not graph-author input. urgency_metadata is seeded from the node's
        # pause_behavior_config above, so a node that pre-seeds these keys could
        # otherwise short-circuit the first escalate() and leave the SLA deadline
        # live -- re-opening the very webhook storm the latch exists to prevent.
        # Reserve the namespace at creation so only escalate() can write it.
        record.urgency_metadata.pop("escalated", None)
        record.urgency_metadata.pop("escalated_sla_deadline", None)
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
        result = await self.repository.write(record)
        await self._emit_webhook(
            "approval.requested",
            result,
            {
                "approval_id": result.approval_id,
                "run_id": result.run_id,
                "thread_id": result.thread_id,
                "graph_version_ref": result.graph_version_ref,
                "node_id": result.node_id,
                "sla_deadline": (result.sla_deadline.isoformat() if result.sla_deadline else None),
            },
        )
        await self._notify(result)
        return result

    async def _notify(self, record: ApprovalRecord, *, summary: str | None = None) -> None:
        if self.notifier is None:
            return
        try:
            await self.notifier.notify(
                ApprovalNotification(
                    approval_id=record.approval_id,
                    run_id=record.run_id,
                    node_id=record.node_id,
                    deployment_ref=record.deployment_ref,
                    tenant_id=record.tenant_id,
                    summary=summary or record.summary,
                    # After an alert latch the live column is cleared, so fall
                    # back to the breached deadline stashed in urgency_metadata
                    # rather than reporting a null SLA on the escalation notice.
                    sla_deadline=(
                        record.urgency_metadata.get("escalated_sla_deadline")
                        or (record.sla_deadline.isoformat() if record.sla_deadline else None)
                    ),
                )
            )
        except Exception:
            logger.exception("approval notification failed for %s", record.approval_id)

    async def get(
        self,
        approval_id: str,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None | object = _UNSCOPED,
        deployment_ref: str | None = None,
        graph_version_ref: str | None = None,
    ) -> ApprovalRecord | None:
        """Fetch a single approval record by its ID. Returns None if not found."""
        if tenant_id is None and workspace_id is _UNSCOPED:
            bound = self._run_scope()
            tenant_id = bound["tenant_id"]
            workspace_id = bound["workspace_id"]
        return await self.repository.get(
            approval_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            deployment_ref=deployment_ref,
            graph_version_ref=graph_version_ref,
        )

    async def list_pending(
        self,
        *,
        run_id: str | None = None,
        thread_id: str | None = None,
        deployment_ref: str | None = None,
        tenant_id: str | None = None,
        workspace_id: str | None | object = _UNSCOPED,
        graph_version_ref: str | None = None,
    ) -> list[ApprovalRecord]:
        """Return all approvals that are still waiting for a human decision.

        Optionally filter by run_id, thread_id, or deployment_ref.
        """
        return await self.repository.list_pending(
            run_id=run_id,
            thread_id=thread_id,
            deployment_ref=deployment_ref,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            graph_version_ref=graph_version_ref,
        )

    async def list(
        self,
        *,
        run_id: str | None = None,
        thread_id: str | None = None,
        deployment_ref: str | None = None,
        tenant_id: str | None = None,
        workspace_id: str | None | object = _UNSCOPED,
        graph_version_ref: str | None = None,
    ) -> list[ApprovalRecord]:
        """Return approval records for a run, thread, or deployment."""
        return await self.repository.list(
            run_id=run_id,
            thread_id=thread_id,
            deployment_ref=deployment_ref,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            graph_version_ref=graph_version_ref,
        )

    async def get_visible_to_deployment(
        self,
        approval_id: str,
        *,
        deployment_ref: str,
        graph_version_ref: str,
        tenant_id: str | None = None,
        workspace_id: str | None | object = _UNSCOPED,
    ) -> ApprovalRecord | None:
        """Return an approval owned by this deployment or one of its child runs.

        Child records keep their real child deployment/graph provenance.  The
        deployment becomes an authorized view only when the scoped run chain
        reaches an ancestor with this exact deployment and graph version.
        """
        record = await self.get(
            approval_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
        )
        if record is None:
            return None
        ancestor = await self.visible_ancestor_run(
            record,
            deployment_ref=deployment_ref,
            graph_version_ref=graph_version_ref,
        )
        return record if ancestor is not None else None

    async def list_pending_visible_to_deployment(
        self,
        *,
        deployment_ref: str,
        graph_version_ref: str,
        run_id: str | None = None,
        thread_id: str | None = None,
        tenant_id: str | None = None,
        workspace_id: str | None | object = _UNSCOPED,
    ) -> list[ApprovalRecord]:
        """List pending approvals whose scoped ancestry reaches a deployment."""
        records = await self.list_pending(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
        )
        return await self._filter_visible_records(
            records,
            deployment_ref=deployment_ref,
            graph_version_ref=graph_version_ref,
            run_id=run_id,
            thread_id=thread_id,
        )

    async def list_visible_to_deployment(
        self,
        *,
        deployment_ref: str,
        graph_version_ref: str,
        run_id: str | None = None,
        thread_id: str | None = None,
        tenant_id: str | None = None,
        workspace_id: str | None | object = _UNSCOPED,
    ) -> list[ApprovalRecord]:
        """List all approval evidence visible through scoped run ancestry."""
        records = await self.list(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
        )
        return await self._filter_visible_records(
            records,
            deployment_ref=deployment_ref,
            graph_version_ref=graph_version_ref,
            run_id=run_id,
            thread_id=thread_id,
        )

    async def _filter_visible_records(
        self,
        records: list[ApprovalRecord],
        *,
        deployment_ref: str,
        graph_version_ref: str,
        run_id: str | None,
        thread_id: str | None,
    ) -> list[ApprovalRecord]:
        """Apply ancestry authorization and parent/child identity filters."""
        visible: list[ApprovalRecord] = []
        for record in records:
            ancestor = await self.visible_ancestor_run(
                record,
                deployment_ref=deployment_ref,
                graph_version_ref=graph_version_ref,
            )
            if ancestor is None:
                continue
            # A deployment-scoped caller thinks in terms of the workflow run it
            # submitted, while the approval correctly names the child run that
            # owns the gate.  Accept either identity without broadening beyond
            # the already-proven ancestry.
            if run_id is not None and run_id not in {record.run_id, ancestor.run_id}:
                continue
            if thread_id is not None and thread_id not in {
                record.thread_id,
                ancestor.thread_id,
            }:
                continue
            visible.append(record)
        return visible

    async def visible_ancestor_run(
        self,
        record: ApprovalRecord,
        *,
        deployment_ref: str,
        graph_version_ref: str,
    ) -> Run | None:
        """Find the exact in-scope ancestor served by a deployment.

        The walk is bounded and cycle-detecting.  Every row comes through the
        deployment-bound ``RunRepository``, so a forged parent id cannot cross
        tenant/workspace scope even when the global run id is guessed.
        """
        current = await self.run_repository.get(record.run_id)
        seen: set[str] = set()
        for _ in range(64):
            if current is None or current.run_id in seen:
                return None
            seen.add(current.run_id)
            if (
                current.tenant_id != record.tenant_id
                or current.workspace_id != record.workspace_id
            ):
                return None
            if (
                current.deployment_ref == deployment_ref
                and current.graph_version_ref == graph_version_ref
            ):
                return current
            if current.parent_run_id is None:
                return None
            current = await self.run_repository.get(current.parent_run_id)
        return None

    async def schedule_ancestor_continuation(
        self,
        approval_id: str,
        *,
        deployment_ref: str,
        graph_version_ref: str,
    ) -> Run:
        """Atomically notify the deployment worker about a resolved child gate.

        The child stays paused under its own provenance.  The root deployment's
        run is re-queued with a signed linkage record in the same database
        transaction; its subgraph executor later resumes only the exact child
        named by the durable pause metadata.
        """
        record = await self._require(approval_id, **self._run_scope())
        if record.status is not ApprovalStatus.RESOLVED or record.resolution is None:
            raise ValueError("approval must be resolved before continuation")
        if self.audit_repository is None:
            raise RuntimeError("child continuation requires a signed audit repository")

        path = await self._run_ancestry(record.run_id)
        ancestor_index = next(
            (
                index
                for index, run in enumerate(path)
                if run.deployment_ref == deployment_ref
                and run.graph_version_ref == graph_version_ref
            ),
            None,
        )
        if ancestor_index is None:
            raise KeyError(approval_id)
        ancestor = path[ancestor_index]
        if ancestor_index == 0:
            return await self.schedule_continuation(approval_id)
        direct_child = path[ancestor_index - 1]
        child = path[0]
        audit = NodeAuditRecord(
            audit_id=f"{ancestor.run_id}:child-approval-continuation:{approval_id}",
            run_id=ancestor.run_id,
            thread_id=ancestor.thread_id,
            node_id=str(
                (ancestor.metadata.get("pending_subgraph") or {}).get("node_id")
                or (ancestor.metadata.get("pending_parallel_subgraph") or {}).get("node_id")
                or "__child_approval_continuation__"
            ),
            node_version=1,
            graph_version_ref=ancestor.graph_version_ref,
            deployment_ref=ancestor.deployment_ref,
            tenant_id=ancestor.tenant_id,
            workspace_id=ancestor.workspace_id,
            status="child_approval_continuation_scheduled",
            completed_at=datetime.now(UTC),
            cost_usd=0.0,
            estimated_cost_usd=0.0,
            cost_measurement="measured",
            actor=record.resolution.actor,
            execution_metadata={
                "approval_id": approval_id,
                "child_run_id": child.run_id,
                "child_deployment_ref_sha256": hashlib.sha256(
                    child.deployment_ref.encode("utf-8")
                ).hexdigest(),
                "direct_child_run_id": direct_child.run_id,
                "continuation_parent_run_id": ancestor.run_id,
            },
            approval_actions=[
                ApprovalActionRecord(
                    approval_id=approval_id,
                    action="child_continuation_scheduled",
                    actor=record.resolution.actor,
                    metadata={"child_run_id": child.run_id},
                )
            ],
        )
        async with self.run_repository.database.transaction(write_lock=True) as connection:
            scheduled, changed = (
                await self.run_repository.schedule_child_approval_continuation_in_transaction(
                    connection,
                    run_id=ancestor.run_id,
                    direct_child_run_id=direct_child.run_id,
                    approval_id=approval_id,
                    approval_child_run_id=child.run_id,
                    approval_child_deployment_ref=child.deployment_ref,
                    deployment_ref=deployment_ref,
                    graph_version_ref=graph_version_ref,
                )
            )
            if changed:
                written = await self.audit_repository.write_in_transaction(connection, audit)
                if written.record_signature is None:
                    raise RuntimeError("child continuation requires a signed audit record")
        persisted = await self.run_repository.get(scheduled.run_id)
        if persisted is None:  # pragma: no cover - transaction retained the row
            raise KeyError(scheduled.run_id)
        return persisted

    async def reconcile_ancestor_continuations(
        self,
        *,
        deployment_ref: str,
        graph_version_ref: str,
    ) -> list[Run]:
        """Repair resolved-child notifications after a process interruption.

        The resolved approval is itself the durable reconciliation source.  A
        deployment worker scans only records whose scoped ancestry reaches its
        exact active graph, and only requeues ancestors still waiting on that
        child.  The transactional CAS and deterministic audit id in
        ``schedule_ancestor_continuation`` make concurrent workers safe.
        """
        scheduled: list[Run] = []
        for record in await self.list_visible_to_deployment(
            deployment_ref=deployment_ref,
            graph_version_ref=graph_version_ref,
            **self._run_scope(),
        ):
            if record.status is not ApprovalStatus.RESOLVED or record.resolution is None:
                continue
            ancestor = await self.visible_ancestor_run(
                record,
                deployment_ref=deployment_ref,
                graph_version_ref=graph_version_ref,
            )
            if (
                ancestor is None
                or ancestor.run_id == record.run_id
                or ancestor.status is not RunStatus.WAITING_APPROVAL
            ):
                continue
            scheduled.append(
                await self.schedule_ancestor_continuation(
                    record.approval_id,
                    deployment_ref=deployment_ref,
                    graph_version_ref=graph_version_ref,
                )
            )
        return scheduled

    async def _run_ancestry(self, run_id: str) -> list[Run]:
        """Return child-to-root scoped ancestry, rejecting cycles and over-depth."""
        path: list[Run] = []
        seen: set[str] = set()
        current = await self.run_repository.get(run_id)
        for _ in range(64):
            if current is None:
                raise KeyError(run_id)
            if current.run_id in seen:
                raise ValueError("run ancestry contains a cycle")
            seen.add(current.run_id)
            path.append(current)
            if current.parent_run_id is None:
                return path
            current = await self.run_repository.get(current.parent_run_id)
        raise ValueError("run ancestry exceeds the continuation depth limit")

    async def escalate(
        self,
        approval_id: str,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None | object = _UNSCOPED,
        deployment_ref: str | None = None,
        graph_version_ref: str | None = None,
    ) -> ApprovalRecord:
        """Escalate an overdue approval based on its configured escalation action.

        Supports three actions:
        - delegate: marks original ESCALATED, creates new approval for delegate
        - auto_reject: resolves original as REJECTED with system actor, then
          continues the run so the rejection actually fails it
        - alert (default): keeps the original PENDING so a human can still
          resolve it, but latches it out of the SLA sweep (nulls sla_deadline
          and marks urgency_metadata['escalated']). Flipping it to ESCALATED
          would hide it from list/get and make the run unresolvable forever.

        If the approval is no longer pending, this is a no-op. That covers both
        double-escalation and, critically, a RESOLVED approval: SLA enforcement
        must not re-open a decision a human already made.
        """
        record = await self._require(
            approval_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            deployment_ref=deployment_ref,
            graph_version_ref=graph_version_ref,
        )
        # A decided approval carries a resolution payload; flipping it to ESCALATED
        # would leave status and payload contradicting each other, and ``delegate``
        # would mint a second live approval for work already closed. The
        # compare-and-set below is the authority -- this check only avoids the
        # round-trip in the common case.
        if record.status is not ApprovalStatus.PENDING:
            return record

        action = record.escalation_action or "alert"

        if action == "delegate":
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

            # Claim the approval and mint the delegate in one transaction. The
            # claim is a compare-and-set against the stored PENDING row, so
            # exactly one of N concurrent SLA checkers gets past this line and
            # exactly one delegate is created; losing the race -- to another
            # checker or to a human who resolved the approval since our read --
            # means we have no claim and create no delegate. Committing the two
            # rows together additionally rules out the crash-shaped orphan: a
            # failure after the claim but before the delegate would leave the
            # approval ESCALATED with nobody holding it, and the ESCALATED
            # short-circuit above means no later poll would ever retry it.
            escalated = await self._claim_escalation_with_delegate(record, delegate_record)
            if escalated is None:
                return await self._require(
                    approval_id,
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    deployment_ref=deployment_ref,
                    graph_version_ref=graph_version_ref,
                )
            # Notify only once both rows are committed: announcing a delegate
            # whose transaction rolled back would page a human about an approval
            # that does not exist.
            await self._notify(delegate_record)
            await self._emit_escalation_webhook(escalated)
            return escalated

        elif action == "auto_reject":
            from zeroth.governance.identity import AuthMethod

            system_actor = ActorIdentity(
                subject="sla_enforcer",
                auth_method=AuthMethod.API_KEY,
                tenant_id=record.tenant_id,
                workspace_id=record.workspace_id,
            )
            resolved, changed = await self._resolve_with_outcome(
                approval_id,
                decision=ApprovalDecision.REJECT,
                actor=system_actor,
                tenant_id=record.tenant_id,
                workspace_id=record.workspace_id,
                deployment_ref=record.deployment_ref,
                graph_version_ref=record.graph_version_ref,
            )
            if changed:
                await self._emit_escalation_webhook(resolved)
                # Resolving REJECT alone leaves the run parked in
                # WAITING_APPROVAL. schedule_continuation's REJECT branch marks
                # the run FAILED (reason='approval_rejected') and records the
                # decision audit, so the SLA rejection actually terminates the
                # run instead of wedging it. Gated on ``changed``: when another
                # resolver won the race, that resolver already scheduled the
                # continuation.
                await self.schedule_continuation(approval_id)
            return resolved

        else:  # "alert" or unknown
            if record.urgency_metadata.get("escalated") and record.sla_deadline is None:
                # Already alert-escalated on an earlier tick. The row is still
                # PENDING (that is the whole point of the alert latch), so the
                # short-circuit at the top of ``escalate`` cannot catch a repeat
                # call. The ``sla_deadline is None`` conjunct keys off the
                # PERSISTED latch invariant, not just the marker: a marker that
                # somehow accompanies a live deadline is treated as not-yet
                # latched and re-claimed, so the fence self-heals rather than
                # leaving the row matching ``list_overdue`` forever. Otherwise a
                # second escalate() (or a stray poll) is a true no-op.
                return record
            written = await self._claim_escalation(record)
            if written is None:
                return await self._require(
                    approval_id,
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    deployment_ref=deployment_ref,
                    graph_version_ref=graph_version_ref,
                )
            await self._notify(written, summary=f"[Escalated] {written.summary}")
            await self._emit_escalation_webhook(written)
            return written

    async def _emit_escalation_webhook(self, record: ApprovalRecord) -> None:
        """Publish the SLA event only from the winning persisted transition."""
        await self._emit_webhook(
            "approval.escalated",
            record,
            {
                "approval_id": record.approval_id,
                "run_id": record.run_id,
                "thread_id": record.thread_id,
                "graph_version_ref": record.graph_version_ref,
                "node_id": record.node_id,
                "escalation_action": record.escalation_action or "alert",
                # An alert escalation clears the live sla_deadline column (the
                # latch removes the row from the overdue sweep); the breached
                # deadline is preserved in urgency_metadata for this event.
                "sla_deadline": (
                    record.urgency_metadata.get("escalated_sla_deadline")
                    or (record.sla_deadline.isoformat() if record.sla_deadline else None)
                ),
            },
        )

    async def _claim_escalation(self, record: ApprovalRecord) -> ApprovalRecord | None:
        """Latch a still-pending alert escalation, or return None if it moved.

        The row deliberately STAYS ``PENDING``: an alert is a nudge, not a
        decision, so a human must still be able to resolve it and the run must
        not wedge in WAITING_APPROVAL. What changes is the SLA fence -- the
        deadline is nulled and ``urgency_metadata['escalated']`` is set -- so
        ``list_overdue`` (status=PENDING AND sla_deadline<now) stops matching the
        row and the checker does not re-escalate it every tick.

        ``ApprovalRepository.resolve_pending`` is a conditional write -- it updates
        the row only while its stored status is still PENDING and publishes under a
        write lock. Reusing it here (rather than an in-memory mutation followed by
        an unconditional ``write``) closes the window between the read in
        ``escalate`` and the write: a human resolution or a second SLA checker that
        lands inside that window makes the update match zero rows and this returns
        None instead of overwriting the newer state. The nulled ``sla_deadline``
        only latches once resolve_pending's write set carries the column.
        """
        if record.sla_deadline is not None:
            # Preserve the breached deadline for the escalation webhook/notice,
            # which now reads it from here since the column is cleared.
            record.urgency_metadata["escalated_sla_deadline"] = record.sla_deadline.isoformat()
        record.urgency_metadata["escalated"] = True
        record.sla_deadline = None
        record.updated_at = datetime.now(UTC)
        return await self.repository.resolve_pending(record)

    async def _claim_escalation_with_delegate(
        self, record: ApprovalRecord, delegate: ApprovalRecord
    ) -> ApprovalRecord | None:
        """Claim a pending approval and mint its delegate together, or neither.

        The delegate hand-off is one fact in two rows, so it needs one
        transaction. The service cannot compose that itself: each repository call
        opens its own connection, so an outer ``database.transaction()`` around
        two repository calls is provably two transactions -- and on SQLite the
        inner ``BEGIN IMMEDIATE`` would deadlock against the outer write lock.
        The repository owns the composite instead, and keeps the same
        compare-and-set predicate ``_claim_escalation`` relies on.
        """
        record.status = ApprovalStatus.ESCALATED
        record.updated_at = datetime.now(UTC)
        return await self.repository._escalate_to_delegate(record, delegate)  # noqa: SLF001

    async def resolve(
        self,
        approval_id: str,
        *,
        decision: ApprovalDecision,
        actor: ActorIdentity,
        edited_payload: dict[str, Any] | None = None,
        reason: str | None = None,
        tenant_id: str | None = None,
        workspace_id: str | None | object = _UNSCOPED,
        deployment_ref: str | None = None,
        graph_version_ref: str | None = None,
    ) -> ApprovalRecord:
        """Record a decision and return its durable approval record."""
        resolved, _changed = await self._resolve_with_outcome(
            approval_id,
            decision=decision,
            actor=actor,
            edited_payload=edited_payload,
            reason=reason,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            deployment_ref=deployment_ref,
            graph_version_ref=graph_version_ref,
        )
        return resolved

    async def _resolve_with_outcome(
        self,
        approval_id: str,
        *,
        decision: ApprovalDecision,
        actor: ActorIdentity,
        edited_payload: dict[str, Any] | None = None,
        reason: str | None = None,
        tenant_id: str | None = None,
        workspace_id: str | None | object = _UNSCOPED,
        deployment_ref: str | None = None,
        graph_version_ref: str | None = None,
    ) -> tuple[ApprovalRecord, bool]:
        """Record a human's decision on a pending approval.

        Validates that the decision is allowed, marks the approval as resolved,
        and writes an audit log entry. If the same exact decision was already
        recorded, this is treated as a safe no-op (idempotent).

        Raises ValueError if the approval is already resolved with a different
        decision, or if the chosen decision is not in the allowed actions list.
        """
        record = await self._require(
            approval_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            deployment_ref=deployment_ref,
            graph_version_ref=graph_version_ref,
        )
        if record.status is ApprovalStatus.RESOLVED:
            current = record.resolution
            if current is None:
                raise ValueError("approval is resolved without resolution payload")
            if (
                current.decision is decision
                and current.actor == actor
                and current.edited_payload == edited_payload
                and current.reason == reason
            ):
                return record, False
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
            reason=reason,
        )
        record.updated_at = datetime.now(UTC)
        resolved = await self.repository.resolve_pending(record)
        if resolved is None:
            current = await self._require(
                approval_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                deployment_ref=deployment_ref,
                graph_version_ref=graph_version_ref,
            )
            current_resolution = current.resolution
            if (
                current.status is ApprovalStatus.RESOLVED
                and current_resolution is not None
                and current_resolution.decision is decision
                and current_resolution.actor == actor
                and current_resolution.edited_payload == edited_payload
                and current_resolution.reason == reason
            ):
                return current, False
            raise ValueError("approval already resolved")
        await self._record_api_audit(resolved)
        await self._emit_webhook(
            "approval.resolved",
            resolved,
            {
                "approval_id": resolved.approval_id,
                "run_id": resolved.run_id,
                "thread_id": resolved.thread_id,
                "graph_version_ref": resolved.graph_version_ref,
                "node_id": resolved.node_id,
                "decision": (resolved.resolution.decision.value if resolved.resolution else None),
            },
        )
        return resolved, True

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
        record = await self._require(approval_id, **self._run_scope())
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
            persisted = await self.run_repository.put_if_status(run, RunStatus.WAITING_APPROVAL)
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

        run.status = RunStatus.PENDING
        run.touch()
        # Persist the resolved metadata and claimable status in one conditional
        # write so a concurrent terminal cancellation cannot be overwritten.
        return await self.run_repository.put_if_status(run, RunStatus.WAITING_APPROVAL)

    async def continue_run(
        self,
        approval_id: str,
        *,
        graph: Graph,
        orchestrator: ApprovalContinuation,
    ) -> Run:
        """Resume a paused workflow run after an approval has been resolved.

        If the decision was REJECT, the run is marked as failed immediately.
        If APPROVE or EDIT_AND_APPROVE, the run is handed back to the
        orchestrator to continue executing from the approval node onward.
        """
        record = await self._require(approval_id, **self._run_scope())
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

        decision = record.resolution.decision
        if decision is ApprovalDecision.REJECT:
            run.failure_state = RunFailureState(
                reason="approval_rejected", message="approval rejected"
            )
            run.status = RunStatus.FAILED
            run.touch()
            persisted = await self.run_repository.put_if_status(run, RunStatus.WAITING_APPROVAL)
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

    async def _emit_webhook(
        self,
        event_type: str,
        record: ApprovalRecord,
        data: dict[str, Any],
    ) -> None:
        """Emit a webhook event if a webhook service is configured."""
        ws = self.webhook_service
        if ws is None:
            return
        try:
            await ws.emit_event(
                event_type=event_type,
                deployment_ref=record.deployment_ref,
                tenant_id=record.tenant_id,
                data=data,
            )
        except Exception:
            logger.exception("failed to emit %s webhook", event_type)

    async def _require(
        self,
        approval_id: str,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None | object = _UNSCOPED,
        deployment_ref: str | None = None,
        graph_version_ref: str | None = None,
    ) -> ApprovalRecord:
        """Fetch an approval record by ID, raising KeyError if it does not exist."""
        record = await self.repository.get(
            approval_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            deployment_ref=deployment_ref,
            graph_version_ref=graph_version_ref,
        )
        if record is None:
            raise KeyError(approval_id)
        return record

    def _run_scope(self) -> dict[str, str | None]:
        """Return the trusted owner already bound to this service's run repository."""
        scope = self.run_repository.scope_context
        return {
            "tenant_id": scope.tenant_id,
            "workspace_id": getattr(scope, "workspace_id", None),
        }

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
                completed_at=datetime.now(UTC),
                cost_usd=0.0,
                estimated_cost_usd=0.0,
                cost_measurement="measured",
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
                audit_id=(
                    f"{run.run_id}:approval-decision:{record.approval_id}:"
                    f"{record.status.value}:{record.resolution.decision.value}:{status}"
                ),
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
                completed_at=datetime.now(UTC),
                cost_usd=0.0,
                estimated_cost_usd=0.0,
                cost_measurement="measured",
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
