"""Production discovery metadata for tenant-scoped service repositories."""

from __future__ import annotations

import importlib
from collections.abc import Iterable

from zeroth.platform.storage.scoping import (
    PersistenceSurface,
    ResourceOperation,
    register_persistence_surface,
)

_O = {operation.value[0]: operation for operation in ResourceOperation}
_O["n"] = ResourceOperation.ENUMERATE

# resource, module, class, method=operation initials. This is production
# discovery metadata, not a test case list: cases are the registry definition
# cross product and semantic test executors are located by convention.
_SURFACES = (
    (
        "service.quota_counters",
        "zeroth.governance.guardrails.rate_limit",
        "QuotaEnforcer",
        "check_and_increment=cru,get=r",
    ),
    (
        "service.rate_limit_buckets",
        "zeroth.governance.guardrails.rate_limit",
        "TokenBucketRateLimiter",
        "check_and_consume=cru,get=r",
    ),
    (
        "service.runs",
        "zeroth.integrations.persistence.runs.run_repository",
        "RunRepository",
        "create=c,get=r,list_runs=n,list_dead_letter_runs=n,put=cu,transition=ru,record_history=ru,record_condition_result=ru,increment_failure_count=u,delete=d,count_pending=n,redact_run=ru,redact_run_in_transaction=ru,erasure_payloads_in_transaction=r,tenant_id_for_run_in_transaction=r,list_erasable_run_ids=n,lock_and_recheck_erasable_run=r,fence_token_snapshot_writes_in_transaction=ru",
    ),
    (
        "service.runs",
        "zeroth.governance.retention.workspace_reader",
        "RetentionWorkspaceMaintenanceReader",
        "list_workspace_ids=n",
    ),
    (
        "service.threads",
        "zeroth.integrations.persistence.runs.thread_repository",
        "ThreadRepository",
        "create=c,get=r,list=n,update=u,resolve=cru,attach_run=ru,get_active_run_id=r,get_latest_run_id=r,list_run_ids=r,set_active_run_id=u",
    ),
    (
        "service.threads",
        "zeroth.integrations.persistence.runs.run_repository",
        "RunRepository",
        "get_active_run_id=r,get_latest_run_id=r,list_run_ids=r,set_active_run_id=u,clear_active_run_id=u",
    ),
    (
        "service.run_checkpoints",
        "zeroth.integrations.persistence.runs.checkpoint_store",
        "CheckpointRowStore",
        "write_row=cu,write_row_in_connection=cu,write_row_bound=cu,get=r,latest_id_for_run=r,list_ids=n,delete=d",
    ),
    (
        "service.run_checkpoints",
        "zeroth.integrations.persistence.runs.run_repository",
        "RunRepository",
        "write_checkpoint=cu,get_checkpoint=r,get_latest_checkpoint=r,get_latest_checkpoint_id_for_run=r,list_checkpoints=n,erase_checkpoints_for_run=d,erase_checkpoints_for_run_in_transaction=d",
    ),
    (
        "service.token_engine_snapshots",
        "zeroth.integrations.persistence.runs.token_snapshot_store",
        "TokenSnapshotRowStore",
        "get=r,compare_and_swap=cu",
    ),
    (
        "service.token_engine_snapshots",
        "zeroth.integrations.persistence.runs.run_repository",
        "RunRepository",
        "get_token_snapshot=r,compare_and_swap_token_snapshot=cu,erase_token_snapshot_for_run_in_transaction=d,fence_and_erase_token_snapshot_for_run_in_transaction=d",
    ),
    (
        "service.side_effect_operations",
        "zeroth.platform.dispatch.operations",
        "SideEffectOperationStore",
        "get=r,state_of=r,pending_reconciliation=n,claim=cru,complete=u,fail=u,mark_ambiguous=u,record_reconciliation=ru,erase_for_run=nd,erase_for_run_in_transaction=nd",
    ),
    (
        "service.webhook_subscriptions",
        "zeroth.service.webhooks.repository",
        "WebhookRepository",
        "create_subscription=c,get_subscription=r,list_subscriptions=n,list_subscriptions_for_event=n,deactivate_subscription=u,delete_subscription=d",
    ),
    (
        "service.webhook_deliveries",
        "zeroth.service.webhooks.repository",
        "WebhookRepository",
        "enqueue_delivery=c,claim_pending_delivery=rnu,mark_delivered=u,mark_failed=ru,dead_letter=ru",
    ),
    (
        "service.webhook_dead_letters",
        "zeroth.service.webhooks.repository",
        "WebhookRepository",
        "dead_letter=c,list_dead_letters=n,get_dead_letter=r",
    ),
    (
        "service.retention_policies",
        "zeroth.governance.retention.policy_repository",
        "RetentionPolicyRepository",
        "get=r,resolve=r,upsert=cru,list_for_tenant=n",
    ),
    (
        "service.retention_policies",
        "zeroth.governance.retention.policy_repository",
        "EnabledPolicyMaintenanceReader",
        "list_all_enabled_for_maintenance=n",
    ),
    (
        "service.legal_holds",
        "zeroth.governance.retention.legal_hold_repository",
        "LegalHoldRepository",
        "place=c,release=ru,place_in_transaction=c,release_in_transaction=ru,get=r,list_for_tenant=n,active_holds_for_tenant=rn,active_holds_for_tenant_in_transaction=rn",
    ),
    (
        "service.retention_audit_log",
        "zeroth.governance.retention.audit_log_repository",
        "RetentionAuditLogRepository",
        "record=c,get=r,record_in_transaction=c,get_in_transaction=r,list_for_run_in_transaction=n,list_for_tenant=n,list_for_run=n",
    ),
    (
        "service.retention_cleanup_state",
        "zeroth.governance.retention.cleanup_state_repository",
        "CleanupStateRepository",
        "initialize_in_transaction=c,get_state_in_transaction=r,claim_in_transaction=ru,heartbeat_in_transaction=ru,release_in_transaction=ru,terminal_in_transaction=ru,repair_terminal_in_transaction=ru",
    ),
    (
        "service.retention_cleanup_operations",
        "zeroth.governance.retention.cleanup_state_repository",
        "CleanupStateRepository",
        "initialize_in_transaction=c,get_operation_in_transaction=r,list_operations_in_transaction=n,update_operation_in_transaction=ru",
    ),
    (
        "service.retention_coordination",
        "zeroth.governance.retention.coordination",
        "RetentionCoordinator",
        "transaction=cr",
    ),
    (
        "service.langgraph_decisions",
        "zeroth.service.langgraph_gateway.enforcement_store",
        "LangGraphEnforcementRepository",
        "save_decision=cr,count_decisions=n",
    ),
    (
        "service.langgraph_inventories",
        "zeroth.service.langgraph_gateway.enforcement_store",
        "LangGraphEnforcementRepository",
        "register_inventory=cu,get_inventory=r,heartbeat=ru",
    ),
    (
        "service.langgraph_run_attestations",
        "zeroth.service.langgraph_gateway.enforcement_store",
        "LangGraphEnforcementRepository",
        "save_attestation=cr,get_attestation=n,get_attestation_by_run_id=r",
    ),
    (
        "service.langgraph_run_attestations",
        "zeroth.service.langgraph_gateway.enforcement_store",
        "StoredCapabilityEvidenceProvider",
        "evidence_for_run=n,evidence_for_governance_run=r",
    ),
)

NON_PERSISTENCE_PUBLIC_METHODS = {
    "RunRepository": frozenset({"install_fence", "clear_fence"}),
    "CheckpointRowStore": frozenset({"encrypt_state_json", "decrypt_state_json"}),
}


def _methods(encoded: str) -> dict[str, frozenset[ResourceOperation]]:
    return {
        name: frozenset(_O[initial] for initial in initials)
        for item in encoded.split(",")
        for name, initials in (item.split("=", 1),)
    }


def load_service_persistence_surfaces() -> tuple[PersistenceSurface, ...]:
    """Load and register every reviewed production service repository surface."""
    # The enforcement facade completes the gateway/store circular import in its
    # supported order before repository discovery imports the store directly.
    importlib.import_module("zeroth.service.langgraph_gateway.enforcement")
    registered: list[PersistenceSurface] = []
    for resource_name, module_name, class_name, encoded in _SURFACES:
        repository_type = getattr(importlib.import_module(module_name), class_name)
        registered.append(
            register_persistence_surface(
                resource_name,
                repository_type,
                operation_methods=_methods(encoded),
                non_persistence_public_methods=NON_PERSISTENCE_PUBLIC_METHODS.get(
                    class_name, frozenset()
                ),
            )
        )
    return tuple(
        sorted(
            registered,
            key=lambda item: (item.resource_name, item.repository_type.__qualname__),
        )
    )


def surface_operation_pairs(
    surfaces: Iterable[PersistenceSurface],
) -> frozenset[tuple[str, ResourceOperation]]:
    """Introspect case pairs solely from production method metadata."""
    return frozenset(
        (surface.resource_name, operation)
        for surface in surfaces
        for method_name in surface.operation_methods
        for operations in (
            getattr(surface.repository_type, method_name).__persistence_resource_operations__[
                surface.resource_name
            ],
        )
        for operation in operations
    )


__all__ = [
    "NON_PERSISTENCE_PUBLIC_METHODS",
    "load_service_persistence_surfaces",
    "surface_operation_pairs",
]
