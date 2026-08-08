"""Audit recording for the orchestration runtime.

Every write the runtime makes to the audit repository goes through
:class:`RuntimeAuditRecorder`: a completed node's history entry, a failed or
rejected node execution, and the branch-scoped variants of both. It also owns
the two transforms every one of those writes applies — secret redaction and the
promotion of a runner audit record's tool calls and memory interactions to
typed, queryable columns.

The recorder receives the audit repository and the secret resolver explicitly.
It reads no orchestrator state, so a caller that has those two objects can
record an audit trail without an orchestrator at all.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from zeroth.contracts.graph import Node
from zeroth.governance.audit import AuditRepository, NodeAuditRecord
from zeroth.governance.audit.capture_vocabulary import normalize_reason_code
from zeroth.governance.audit.models import MemoryAccessRecord, TokenUsage, ToolCallRecord
from zeroth.platform.secrets import SecretResolver
from zeroth.runtime.parallel.models import BranchContext
from zeroth.runtime.runs import Run, RunHistoryEntry

logger = logging.getLogger(__name__)

# The enforcement modes that survive the audit capture boundary as decision
# metadata. The evaluation result they come from is a nested dict, and a nested
# dict is not a kind any allowlisted metadata key declares, so the modes are
# promoted to their own top-level keys or they are not persisted at all.
_ENFORCEMENT_MODE_KEYS = ("network_mode", "sandbox_strictness_mode")


def enforcement_audit_fields(
    context: Mapping[str, Any],
    *,
    applied: bool,
) -> dict[str, Any]:
    """Flatten a policy evaluation's decision metadata onto audit-retained keys.

    Args:
        context: The policy evaluation result, as ``model_dump(mode="json")``.
        applied: Whether the enforcement context was actually applied to the
            node's execution -- ``False`` on the denial path, where the node
            never ran.

    Returns:
        A flat mapping of ``enforcement_applied``, the evaluation ``decision``
        and whichever enforcement modes the result declared as strings.
    """
    fields: dict[str, Any] = {"enforcement_applied": applied}
    decision = context.get("decision")
    if type(decision) is str:
        fields["decision"] = decision
    for key in _ENFORCEMENT_MODE_KEYS:
        value = context.get(key)
        if type(value) is str:
            fields[key] = value
    return fields


def bare_error_audit_record(error: BaseException) -> dict[str, Any]:
    """Describe an error that carried no audit record, as decision metadata only.

    The exception's *message* is content -- it carries whatever the raising code
    was holding -- and the capture boundary replaces it. Its class *name* is the
    outcome, so it is normalized into a stable lower-case reason code that the
    metadata allowlist retains.
    """
    return {"reason_code": normalize_reason_code(type(error).__name__) or "unknown_error"}


@dataclass(frozen=True, slots=True)
class RuntimeAuditRecorder:
    """Writes the runtime's audit records and run history entries.

    Both dependencies are optional and independently so: without an audit
    repository the recorder still numbers audit refs and appends run history
    (the unaudited-runtime path), and without a secret resolver redaction is a
    pass-through.
    """

    audit_repository: AuditRepository | None = None
    secret_resolver: SecretResolver | None = None

    def redact(self, value: Any) -> Any:
        """Redact any resolved secret values before persisting audit material."""
        resolver = self.secret_resolver
        if resolver is None:
            return value
        return resolver.redactor().redact(value)

    @staticmethod
    def stored_audit_id(run_id: str, audit_ref: str) -> str:
        """Namespace persisted audit IDs by run so append-only storage stays globally unique."""
        return f"{run_id}:{audit_ref}"

    @staticmethod
    def typed_fields(
        record: Mapping[str, Any],
    ) -> tuple[list[ToolCallRecord], list[MemoryAccessRecord]]:
        """Promote a runner audit record's tool calls / memory interactions to typed fields.

        These otherwise only live in ``execution_metadata.extra`` and the typed
        (queryable, evidence-summarized) ``tool_calls`` / ``memory_interactions``
        fields stay empty. Built from the already-redacted record so secrets never
        reach the typed columns, and tolerant of odd shapes (a redaction edge case
        coerces an argument/outcome into ``{"redacted": ...}`` rather than dropping
        the whole call).
        """
        extra = record.get("extra")
        if not isinstance(extra, Mapping):
            return [], []

        def _as_dict(value: Any) -> dict[str, Any] | None:
            if value is None:
                return None
            return dict(value) if isinstance(value, Mapping) else {"redacted": value}

        tool_calls: list[ToolCallRecord] = []
        for tc in extra.get("tool_calls") or []:
            if not isinstance(tc, Mapping):
                continue
            tool = tc.get("tool")
            tool = tool if isinstance(tool, Mapping) else {}
            error = tc.get("error")
            # ZER-26/AUD-006: the at-least-once marker must survive promotion —
            # the typed columns are the queryable durable record, and dropping
            # the marker there made every MCP call read as though the operation
            # guarantee applied. None stays None: an unmarked call must not
            # grow a marker.
            support = tc.get("operation_support")
            residual_risk = tc.get("operation_residual_duplicate_risk")
            try:
                tool_calls.append(
                    ToolCallRecord(
                        tool_ref=str(tool.get("executable_unit_ref") or tool.get("tool_ref") or ""),
                        alias=str(tool.get("alias") or ""),
                        arguments=_as_dict(tc.get("arguments")) or {},
                        outcome=_as_dict(tc.get("outcome")),
                        error=error if error is None else str(error),
                        operation_key=(
                            None if tc.get("operation_key") is None else str(tc["operation_key"])
                        ),
                        operation_target_ref=(
                            None
                            if tc.get("operation_target_ref") is None
                            else str(tc["operation_target_ref"])
                        ),
                        operation_support=None if support is None else str(support),
                        operation_state=(
                            None
                            if tc.get("operation_state") is None
                            else str(tc["operation_state"])
                        ),
                        operation_first_execution=(
                            None
                            if tc.get("operation_first_execution") is None
                            else bool(tc["operation_first_execution"])
                        ),
                        operation_replay_suppressed=(
                            None
                            if tc.get("operation_replay_suppressed") is None
                            else bool(tc["operation_replay_suppressed"])
                        ),
                        operation_reconciliation_required=(
                            None
                            if tc.get("operation_reconciliation_required") is None
                            else bool(tc["operation_reconciliation_required"])
                        ),
                        operation_reconciliation_exhausted=(
                            None
                            if tc.get("operation_reconciliation_exhausted") is None
                            else bool(tc["operation_reconciliation_exhausted"])
                        ),
                        operation_residual_duplicate_risk=(
                            None if residual_risk is None else bool(residual_risk)
                        ),
                    )
                )
            except Exception as exc:
                # A malformed tool-call record must not abort the whole audit
                # write — but log it, since silently dropping a call from the
                # typed/queryable columns under-reports in a governance system.
                logger.warning("audit: dropping malformed tool_call from typed fields: %s", exc)
                continue

        memory_interactions: list[MemoryAccessRecord] = []
        for mi in extra.get("memory_interactions") or []:
            if not isinstance(mi, Mapping):
                continue
            try:
                memory_interactions.append(MemoryAccessRecord.model_validate(dict(mi)))
            except Exception as exc:
                logger.warning(
                    "audit: dropping malformed memory_interaction from typed fields: %s", exc
                )
                continue
        return tool_calls, memory_interactions

    async def record_history(
        self,
        run: Run,
        node: Node,
        node_id: str,
        input_payload: Mapping[str, Any],
        output_payload: Mapping[str, Any],
        audit_record: Mapping[str, Any],
        *,
        started_at: datetime | None = None,
    ) -> None:
        """Save a record of this node's execution to the run history and audit log.

        Creates an audit entry (if an audit repository is configured) and
        appends a history entry to the run so you can see what happened
        at each step.
        """
        redacted_input = self.redact(dict(input_payload))
        redacted_output = self.redact(dict(output_payload))
        redacted_audit_record = self.redact(dict(audit_record))
        audit_refs = list(run.audit_refs)
        audit_ref = f"audit:{len(audit_refs) + 1}"
        audit_refs.append(audit_ref)
        run.audit_refs = audit_refs
        # started_at is the node's dispatch time (captured by the caller); without
        # it completed_at==started_at and the record reports a zero duration.
        completed_at = datetime.now(UTC)
        node_started_at = started_at or completed_at
        if self.audit_repository is not None:
            # Promote token_usage and cost fields from runner audit record
            # to top-level NodeAuditRecord fields for queryability.
            token_usage_data = redacted_audit_record.get("token_usage")
            token_usage = (
                TokenUsage.model_validate(token_usage_data)
                if token_usage_data is not None
                else None
            )
            tool_calls, memory_interactions = self.typed_fields(redacted_audit_record)
            await self.audit_repository.write(
                NodeAuditRecord(
                    audit_id=self.stored_audit_id(run.run_id, audit_ref),
                    run_id=run.run_id,
                    thread_id=run.thread_id,
                    tenant_id=run.tenant_id,
                    workspace_id=run.workspace_id,
                    node_id=node_id,
                    node_version=node.node_version,
                    graph_version_ref=run.graph_version_ref,
                    deployment_ref=run.deployment_ref,
                    attempt=1,
                    status="completed",
                    started_at=node_started_at,
                    completed_at=completed_at,
                    input_snapshot=redacted_input,
                    output_snapshot=redacted_output,
                    execution_metadata=redacted_audit_record,
                    token_usage=token_usage,
                    cost_usd=redacted_audit_record.get("cost_usd"),
                    cost_event_id=redacted_audit_record.get("cost_event_id"),
                    tool_calls=tool_calls,
                    memory_interactions=memory_interactions,
                )
            )
        run.execution_history.append(
            RunHistoryEntry(
                node_id=node_id,
                status="completed",
                input_snapshot=redacted_input,
                output_snapshot=redacted_output,
                audit_ref=audit_ref,
                # Promote per-node cost so _sum_run_cost can aggregate the run's
                # spend from its own history (basis for the per-run ceiling).
                cost_usd=redacted_audit_record.get("cost_usd"),
            )
        )
        run.completed_steps = [entry.node_id for entry in run.execution_history]

    async def record_failed_execution(
        self,
        run: Run,
        node: Node,
        node_id: str,
        input_payload: Mapping[str, Any],
        error: Exception,
        *,
        started_at: datetime | None = None,
    ) -> None:
        """Persist an audit record for execution failures that happen before completion."""
        if self.audit_repository is None:
            return
        carried_audit = getattr(error, "audit_record", None)
        # Errors that attach an audit_record (content blocks, integrity rejections,
        # paid-then-failed calls) are governance rejections. Bare infrastructure
        # errors (provider auth/network failures, dispatcher errors) carry nothing,
        # but still must leave a trail — a failed node with no audit record is
        # indistinguishable from a node that never ran.
        is_rejection = isinstance(carried_audit, Mapping)
        audit_record: dict[str, Any] = (
            dict(carried_audit) if is_rejection else bare_error_audit_record(error)
        )
        # ZER-26/AUD-008: a timed-out side effect attaches its operation facts
        # as ``operation_audit`` — the state is AMBIGUOUS and the effect may
        # have landed. Merging (not replacing) keeps the rejection/bare-error
        # classification while making the operation state durable.
        operation_audit = getattr(error, "operation_audit", None)
        if isinstance(operation_audit, Mapping):
            audit_record.update(operation_audit)
        audit_refs = list(run.audit_refs)
        audit_ref = f"audit:{len(audit_refs) + 1}"
        audit_refs.append(audit_ref)
        run.audit_refs = audit_refs
        completed_at = datetime.now(UTC)
        node_started_at = started_at or completed_at
        redacted_audit_record = self.redact(audit_record)
        # Promote cost/token fields so spend incurred before the failure -- a paid
        # LLM call that then failed validation or was content-blocked -- is not lost
        # from the audit trail (and stays visible to econ.waste.analyze_run).
        token_usage_data = redacted_audit_record.get("token_usage")
        token_usage = (
            TokenUsage.model_validate(token_usage_data) if token_usage_data is not None else None
        )
        tool_calls, memory_interactions = self.typed_fields(redacted_audit_record)
        await self.audit_repository.write(
            NodeAuditRecord(
                audit_id=self.stored_audit_id(run.run_id, audit_ref),
                run_id=run.run_id,
                thread_id=run.thread_id,
                tenant_id=run.tenant_id,
                workspace_id=run.workspace_id,
                node_id=node_id,
                node_version=node.node_version,
                graph_version_ref=run.graph_version_ref,
                deployment_ref=run.deployment_ref,
                attempt=1,
                status="rejected" if is_rejection else "failed",
                started_at=node_started_at,
                completed_at=completed_at,
                input_snapshot=self.redact(dict(input_payload)),
                output_snapshot={},
                execution_metadata=redacted_audit_record,
                token_usage=token_usage,
                cost_usd=redacted_audit_record.get("cost_usd"),
                cost_event_id=redacted_audit_record.get("cost_event_id"),
                error=self.redact(str(error)),
                tool_calls=tool_calls,
                memory_interactions=memory_interactions,
            )
        )

    async def record_policy_rejection(
        self,
        run: Run,
        node: Node,
        input_payload: Mapping[str, Any],
        decision_payload: Mapping[str, Any],
        reason: str | None,
    ) -> None:
        """Record a policy denial like a node attempt so operators can diagnose the stop.

        The audit ref is numbered and appended to the run even when no audit
        repository is configured, so the run's ref sequence is identical either
        way — matching the completed and failed paths.
        """
        audit_refs = list(run.audit_refs)
        audit_ref = f"audit:{len(audit_refs) + 1}"
        audit_refs.append(audit_ref)
        run.audit_refs = audit_refs
        if self.audit_repository is None:
            return
        await self.audit_repository.write(
            NodeAuditRecord(
                audit_id=self.stored_audit_id(run.run_id, audit_ref),
                run_id=run.run_id,
                thread_id=run.thread_id,
                tenant_id=run.tenant_id,
                workspace_id=run.workspace_id,
                node_id=node.node_id,
                node_version=node.node_version,
                graph_version_ref=run.graph_version_ref,
                deployment_ref=run.deployment_ref,
                attempt=1,
                status="rejected",
                completed_at=datetime.now(UTC),
                input_snapshot=self.redact(dict(input_payload)),
                output_snapshot={},
                execution_metadata=self.redact(
                    enforcement_audit_fields(decision_payload, applied=False)
                ),
                error=reason,
            )
        )

    async def record_failed_branch_execution(
        self,
        run: Run,
        node: Node,
        node_id: str,
        input_payload: Mapping[str, Any],
        error: Exception,
        ctx: BranchContext,
    ) -> None:
        """Persist a branch-scoped audit record for a failed branch-node dispatch.

        Mirrors record_failed_execution: errors that attach an audit_record
        (content blocks, integrity rejections, paid-then-failed calls) are
        governance rejections; bare infrastructure errors still must leave a
        trail — a failed branch node with no audit record is indistinguishable
        from a node that never ran.
        """
        if self.audit_repository is None:
            return
        carried_audit = getattr(error, "audit_record", None)
        is_rejection = isinstance(carried_audit, Mapping)
        audit_record: dict[str, Any] = (
            dict(carried_audit) if is_rejection else bare_error_audit_record(error)
        )
        # Same ZER-26/AUD-008 merge as the non-branch path: a branch can run a
        # side-effect node, and its timeout facts must not be lost either.
        operation_audit = getattr(error, "operation_audit", None)
        if isinstance(operation_audit, Mapping):
            audit_record.update(operation_audit)
        audit_record["branch_id"] = ctx.branch_id
        audit_record["branch_index"] = ctx.branch_index
        audit_seq = len(ctx.audit_refs) + 1
        audit_ref = f"{run.run_id}:branch:{ctx.branch_index}:audit:{audit_seq}"
        ctx.audit_refs.append(audit_ref)
        redacted_audit_record = self.redact(audit_record)
        # Promote cost/token fields so spend incurred before the failure stays
        # visible in the audit trail (and to econ.waste.analyze_run).
        token_usage_data = redacted_audit_record.get("token_usage")
        token_usage = (
            TokenUsage.model_validate(token_usage_data) if token_usage_data is not None else None
        )
        tool_calls, memory_interactions = self.typed_fields(redacted_audit_record)
        await self.audit_repository.write(
            NodeAuditRecord(
                audit_id=audit_ref,
                run_id=run.run_id,
                thread_id=run.thread_id,
                node_id=node_id,
                node_version=node.node_version,
                graph_version_ref=run.graph_version_ref,
                deployment_ref=run.deployment_ref,
                attempt=1,
                status="rejected" if is_rejection else "failed",
                completed_at=datetime.now(UTC),
                input_snapshot=self.redact(dict(input_payload)),
                output_snapshot={},
                execution_metadata=redacted_audit_record,
                token_usage=token_usage,
                cost_usd=redacted_audit_record.get("cost_usd"),
                cost_event_id=redacted_audit_record.get("cost_event_id"),
                error=self.redact(str(error)),
                tool_calls=tool_calls,
                memory_interactions=memory_interactions,
            )
        )
