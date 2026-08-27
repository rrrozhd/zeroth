"""Local-only evidence fixture for the ambiguous-operation resolution UI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from zeroth.contracts.governed import RunStatus
from zeroth.governance.audit.models import NodeAuditRecord, ToolCallRecord
from zeroth.governance.audit.repository import AuditRepository
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.platform.dispatch.operations import OperationState, SideEffectOperationStore
from zeroth.platform.storage import NullWorkspaceScopeContext, ScopeContext
from zeroth.runtime.runs import Run, RunHistoryEntry


@dataclass(frozen=True, slots=True)
class AmbiguousOperationDemo:
    """Identities and durable records emitted by the demo seed."""

    run: Run
    audit: NodeAuditRecord
    operation_key: str
    operation: dict[str, Any]


def _scope(tenant_id: str, workspace_id: str | None):
    if workspace_id is None:
        if tenant_id == "default":
            return NullWorkspaceScopeContext.for_default_compatibility()
        return NullWorkspaceScopeContext(tenant_id)
    if tenant_id == "default":
        return ScopeContext.for_default_compatibility(workspace_id=workspace_id)
    return ScopeContext(tenant_id, workspace_id)


async def seed_ambiguous_operation_demo(
    database,
    *,
    tenant_id: str,
    workspace_id: str | None,
    deployment_ref: str,
    graph_version_ref: str,
    signer,
    fixture_id: str,
) -> AmbiguousOperationDemo:
    """Seed one inert run, signed tool-call audit, and AMBIGUOUS operation.

    This function never dispatches a runner or action adapter. It only uses the
    same persistence repositories as the service, making it suitable for a
    throwaway database copied from a real deployment.
    """
    scope = _scope(tenant_id, workspace_id)
    run_repository = RunRepository(database, scope)
    audit_repository = AuditRepository.scoped(database, scope, signer=signer)
    operation_store = SideEffectOperationStore(database, scope)

    run_id = f"ambiguous-demo-run-{fixture_id}"
    audit_id = f"ambiguous-demo-audit-{fixture_id}"
    operation_key = f"ambiguous-demo-operation-{fixture_id}"

    existing_run = await run_repository.get(run_id)
    existing_audit = await audit_repository.get(audit_id)
    existing_operation = await operation_store.get(operation_key)
    if existing_run is not None and existing_audit is not None and existing_operation is not None:
        return AmbiguousOperationDemo(
            run=existing_run,
            audit=existing_audit,
            operation_key=operation_key,
            operation=existing_operation,
        )

    if existing_run is None:
        run = await run_repository.create(
            Run(
                run_id=run_id,
                graph_version_ref=graph_version_ref,
                deployment_ref=deployment_ref,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                status=RunStatus.COMPLETED,
                execution_history=[
                    RunHistoryEntry(
                        node_id="ambiguous-operation-demo",
                        status="completed",
                        audit_ref=audit_id,
                        output_snapshot={"fixture": "ambiguous-operation-ui"},
                    )
                ],
                audit_refs=[audit_id],
                final_output={"fixture": "ambiguous-operation-ui"},
                metadata={"fixture": "ambiguous-operation-ui", "external_calls": False},
            )
        )
    else:
        run = existing_run

    if existing_operation is None:
        await operation_store.claim(
            operation_key,
            run_id=run_id,
            dispatch_id=f"ambiguous-demo-dispatch-{fixture_id}",
            idempotency_key=f"ambiguous-demo-idempotency-{fixture_id}",
            target_ref="fixture://no-external-action",
        )
        await operation_store.mark_ambiguous(
            operation_key,
            reason="deterministic local UI fixture; no action was dispatched",
        )
        existing_operation = await operation_store.get(operation_key)
        assert existing_operation is not None

    if existing_audit is None:
        audit = await audit_repository.write(
            NodeAuditRecord(
                audit_id=audit_id,
                run_id=run_id,
                thread_id=run.thread_id,
                node_id="ambiguous-operation-demo",
                graph_version_ref=graph_version_ref,
                deployment_ref=deployment_ref,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                status="completed",
                execution_metadata={"fixture": "ambiguous-operation-ui", "external_calls": False},
                tool_calls=[
                    ToolCallRecord(
                        tool_ref="fixture://no-external-action",
                        alias="Local ambiguous-operation fixture",
                        operation_key=operation_key,
                        operation_target_ref="fixture://no-external-action",
                        operation_support="at_least_once",
                        operation_state=OperationState.AMBIGUOUS.value,
                        operation_first_execution=True,
                        operation_replay_suppressed=False,
                        operation_reconciliation_required=True,
                        operation_reconciliation_exhausted=False,
                        operation_residual_duplicate_risk=True,
                    )
                ],
                output_snapshot={"fixture": "ambiguous-operation-ui"},
                cost_usd=0.0,
                estimated_cost_usd=0.0,
            )
        )
    else:
        audit = existing_audit

    return AmbiguousOperationDemo(
        run=run,
        audit=audit,
        operation_key=operation_key,
        operation=existing_operation,
    )


__all__ = ["AmbiguousOperationDemo", "seed_ambiguous_operation_demo"]
