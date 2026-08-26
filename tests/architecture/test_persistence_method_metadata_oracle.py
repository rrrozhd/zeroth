"""Static exact metadata oracle for all discovered production persistence methods."""

from __future__ import annotations

import importlib
import inspect
from types import MappingProxyType

import pytest

from zeroth.platform.storage import ResourceOperation, validate_persistence_surface
from zeroth.platform.storage.service_surfaces import load_service_persistence_surfaces

O = ResourceOperation  # noqa: E741
_CODES = MappingProxyType(
    {
        "C": O.CREATE,
        "R": O.READ,
        "N": O.ENUMERATE,
        "U": O.UPDATE,
        "D": O.DELETE,
    }
)

# Reviewed static oracle. Stable identities avoid coupling the expected values
# to imported class objects or to discovery output.
_ORACLE_ROWS = """\
zeroth.contracts.graph.repository|GraphRepository|archive|RU
zeroth.contracts.graph.repository|GraphRepository|clone_published_to_draft|CRU
zeroth.contracts.graph.repository|GraphRepository|create|CR
zeroth.contracts.graph.repository|GraphRepository|diff|R
zeroth.contracts.graph.repository|GraphRepository|get|R
zeroth.contracts.graph.repository|GraphRepository|get_latest_version|R
zeroth.contracts.graph.repository|GraphRepository|list|N
zeroth.contracts.graph.repository|GraphRepository|list_versions|N
zeroth.contracts.graph.repository|GraphRepository|publish|RU
zeroth.contracts.graph.repository|GraphRepository|save|CRU
zeroth.contracts.graph.repository|GraphRepository|update_status|RU
zeroth.contracts.registry.registry|ContractRegistry|delete|D
zeroth.contracts.registry.registry|ContractRegistry|get|R
zeroth.contracts.registry.registry|ContractRegistry|latest_version|R
zeroth.contracts.registry.registry|ContractRegistry|list_names|N
zeroth.contracts.registry.registry|ContractRegistry|list_versions|N
zeroth.contracts.registry.registry|ContractRegistry|register|CR
zeroth.contracts.registry.registry|ContractRegistry|register_schema|CR
zeroth.contracts.registry.registry|ContractRegistry|register_tool|CR
zeroth.contracts.registry.registry|ContractRegistry|resolve|R
zeroth.contracts.registry.registry|ContractRegistry|resolve_model_type|R
zeroth.governance.approvals.repository|ApprovalRepository|get|R
zeroth.governance.approvals.repository|ApprovalRepository|list|N
zeroth.governance.approvals.repository|ApprovalRepository|list_pending|N
zeroth.governance.approvals.repository|ApprovalRepository|resolve_pending|RU
zeroth.governance.approvals.repository|ApprovalRepository|write|CRU
zeroth.governance.approvals.repository|ApprovalRepository|list_overdue|N
zeroth.governance.attestations.heartbeat|HeartbeatRepository|latest_for_deployment|R
zeroth.governance.attestations.heartbeat|HeartbeatRepository|record|C
zeroth.governance.attestations.store|InventoryRegistrationRepository|latest_for_deployment|R
zeroth.governance.attestations.store|InventoryRegistrationRepository|register|C
zeroth.governance.attestations.store|RunAttestationRepository|find_by_correlation|R
zeroth.governance.attestations.store|RunAttestationRepository|find_for_deployment|R
zeroth.governance.attestations.store|RunAttestationRepository|record|CR
zeroth.governance.audit.repository|AuditRepository|crypto_erase|RU
zeroth.governance.audit.repository|AuditRepository|crypto_erase_in_transaction|RU
zeroth.governance.audit.repository|AuditRepository|get|R
zeroth.governance.audit.repository|AuditRepository|list|N
zeroth.governance.audit.repository|AuditRepository|list_by_deployment|N
zeroth.governance.audit.repository|AuditRepository|list_by_graph_version|N
zeroth.governance.audit.repository|AuditRepository|list_by_node|N
zeroth.governance.audit.repository|AuditRepository|list_by_run|N
zeroth.governance.audit.repository|AuditRepository|list_by_run_in_transaction|N
zeroth.governance.audit.repository|AuditRepository|list_by_thread|N
zeroth.governance.audit.repository|AuditRepository|list_erasable|N
zeroth.governance.audit.repository|AuditRepository|list_erasable_in_transaction|N
zeroth.governance.audit.repository|AuditRepository|write|CRU
zeroth.governance.audit.repository|AuditRepository|write_many|CRU
zeroth.governance.decisions.repository|DecisionRepository|find_by_idempotency_key|R
zeroth.governance.decisions.repository|DecisionRepository|find_replay|R
zeroth.governance.decisions.repository|DecisionRepository|record|CR
zeroth.governance.guardrails.rate_limit|QuotaEnforcer|check_and_increment|CRU
zeroth.governance.guardrails.rate_limit|QuotaEnforcer|get|R
zeroth.governance.guardrails.rate_limit|TokenBucketRateLimiter|check_and_consume|CRU
zeroth.governance.guardrails.rate_limit|TokenBucketRateLimiter|get|R
zeroth.governance.retention.audit_log_repository|RetentionAuditLogRepository|get|R
zeroth.governance.retention.audit_log_repository|RetentionAuditLogRepository|get_in_transaction|R
zeroth.governance.retention.audit_log_repository|RetentionAuditLogRepository|list_for_run|N
zeroth.governance.retention.audit_log_repository|RetentionAuditLogRepository|list_for_run_in_transaction|N
zeroth.governance.retention.audit_log_repository|RetentionAuditLogRepository|list_for_tenant|N
zeroth.governance.retention.audit_log_repository|RetentionAuditLogRepository|record|C
zeroth.governance.retention.audit_log_repository|RetentionAuditLogRepository|record_in_transaction|C
zeroth.governance.retention.cleanup_state_repository|CleanupStateRepository|claim_in_transaction|RU
zeroth.governance.retention.cleanup_state_repository|CleanupStateRepository|get_operation_in_transaction|R
zeroth.governance.retention.cleanup_state_repository|CleanupStateRepository|get_state_in_transaction|R
zeroth.governance.retention.cleanup_state_repository|CleanupStateRepository|heartbeat_in_transaction|RU
zeroth.governance.retention.cleanup_state_repository|CleanupStateRepository|initialize_in_transaction|C
zeroth.governance.retention.cleanup_state_repository|CleanupStateRepository|list_operations_in_transaction|N
zeroth.governance.retention.cleanup_state_repository|CleanupStateRepository|release_in_transaction|RU
zeroth.governance.retention.cleanup_state_repository|CleanupStateRepository|repair_terminal_in_transaction|RU
zeroth.governance.retention.cleanup_state_repository|CleanupStateRepository|terminal_in_transaction|RU
zeroth.governance.retention.cleanup_state_repository|CleanupStateRepository|update_operation_in_transaction|RU
zeroth.governance.retention.coordination|RetentionCoordinator|transaction|CR
zeroth.governance.retention.legal_hold_repository|LegalHoldRepository|active_holds_for_tenant|RN
zeroth.governance.retention.legal_hold_repository|LegalHoldRepository|active_holds_for_tenant_in_transaction|RN
zeroth.governance.retention.legal_hold_repository|LegalHoldRepository|get|R
zeroth.governance.retention.legal_hold_repository|LegalHoldRepository|list_for_tenant|N
zeroth.governance.retention.legal_hold_repository|LegalHoldRepository|place|C
zeroth.governance.retention.legal_hold_repository|LegalHoldRepository|place_in_transaction|C
zeroth.governance.retention.legal_hold_repository|LegalHoldRepository|release|RU
zeroth.governance.retention.legal_hold_repository|LegalHoldRepository|release_in_transaction|RU
zeroth.governance.retention.policy_repository|RetentionPolicyRepository|get|R
zeroth.governance.retention.policy_repository|RetentionPolicyRepository|list_for_tenant|N
zeroth.governance.retention.policy_repository|RetentionPolicyRepository|resolve|R
zeroth.governance.retention.policy_repository|RetentionPolicyRepository|upsert|CRU
zeroth.governance.retention.policy_repository|EnabledPolicyMaintenanceReader|list_all_enabled_for_maintenance|N
zeroth.governance.retention.workspace_reader|RetentionOwnerMaintenanceReader|list_tenant_ids|N
zeroth.governance.retention.workspace_reader|RetentionWorkspaceMaintenanceReader|list_workspace_ids|N
zeroth.integrations.memory.config_repository|MemoryConnectorConfigRepository|delete|D
zeroth.integrations.memory.config_repository|MemoryConnectorConfigRepository|get|R
zeroth.integrations.memory.config_repository|MemoryConnectorConfigRepository|list|N
zeroth.integrations.memory.config_repository|MemoryConnectorConfigRepository|upsert|CRU
zeroth.integrations.persistence.runs.checkpoint_store|CheckpointRowStore|delete|D
zeroth.integrations.persistence.runs.checkpoint_store|CheckpointRowStore|get|R
zeroth.integrations.persistence.runs.checkpoint_store|CheckpointRowStore|latest_id_for_run|R
zeroth.integrations.persistence.runs.checkpoint_store|CheckpointRowStore|list_ids|N
zeroth.integrations.persistence.runs.checkpoint_store|CheckpointRowStore|write_row|CU
zeroth.integrations.persistence.runs.checkpoint_store|CheckpointRowStore|write_row_bound|CU
zeroth.integrations.persistence.runs.checkpoint_store|CheckpointRowStore|write_row_in_connection|CU
zeroth.integrations.persistence.runs.run_repository|RunRepository|clear_active_run_id|U
zeroth.integrations.persistence.runs.run_repository|RunRepository|compare_and_swap_token_snapshot|CU
zeroth.integrations.persistence.runs.run_repository|RunRepository|count_pending|N
zeroth.integrations.persistence.runs.run_repository|RunRepository|create|C
zeroth.integrations.persistence.runs.run_repository|RunRepository|delete|D
zeroth.integrations.persistence.runs.run_repository|RunRepository|erase_checkpoints_for_run|D
zeroth.integrations.persistence.runs.run_repository|RunRepository|erase_checkpoints_for_run_in_transaction|D
zeroth.integrations.persistence.runs.run_repository|RunRepository|erase_token_snapshot_for_run_in_transaction|D
zeroth.integrations.persistence.runs.run_repository|RunRepository|erasure_payloads_in_transaction|R
zeroth.integrations.persistence.runs.run_repository|RunRepository|fence_and_erase_token_snapshot_for_run_in_transaction|D
zeroth.integrations.persistence.runs.run_repository|RunRepository|fence_token_snapshot_writes_in_transaction|RU
zeroth.integrations.persistence.runs.run_repository|RunRepository|get|R
zeroth.integrations.persistence.runs.run_repository|RunRepository|get_active_run_id|R
zeroth.integrations.persistence.runs.run_repository|RunRepository|get_checkpoint|R
zeroth.integrations.persistence.runs.run_repository|RunRepository|get_latest_checkpoint|R
zeroth.integrations.persistence.runs.run_repository|RunRepository|get_latest_checkpoint_id_for_run|R
zeroth.integrations.persistence.runs.run_repository|RunRepository|get_latest_run_id|R
zeroth.integrations.persistence.runs.run_repository|RunRepository|get_token_snapshot|R
zeroth.integrations.persistence.runs.run_repository|RunRepository|increment_failure_count|U
zeroth.integrations.persistence.runs.run_repository|RunRepository|list_checkpoints|N
zeroth.integrations.persistence.runs.run_repository|RunRepository|list_dead_letter_runs|N
zeroth.integrations.persistence.runs.run_repository|RunRepository|list_erasable_run_ids|N
zeroth.integrations.persistence.runs.run_repository|RunRepository|list_run_ids|R
zeroth.integrations.persistence.runs.run_repository|RunRepository|list_runs|N
zeroth.integrations.persistence.runs.run_repository|RunRepository|lock_and_recheck_erasable_run|R
zeroth.integrations.persistence.runs.run_repository|RunRepository|put|CU
zeroth.integrations.persistence.runs.run_repository|RunRepository|record_condition_result|RU
zeroth.integrations.persistence.runs.run_repository|RunRepository|record_history|RU
zeroth.integrations.persistence.runs.run_repository|RunRepository|redact_run|RU
zeroth.integrations.persistence.runs.run_repository|RunRepository|redact_run_in_transaction|RU
zeroth.integrations.persistence.runs.run_repository|RunRepository|set_active_run_id|U
zeroth.integrations.persistence.runs.run_repository|RunRepository|tenant_id_for_run_in_transaction|R
zeroth.integrations.persistence.runs.run_repository|RunRepository|transition|RU
zeroth.integrations.persistence.runs.run_repository|RunRepository|write_checkpoint|CU
zeroth.integrations.persistence.runs.thread_repository|ThreadRepository|attach_run|RU
zeroth.integrations.persistence.runs.thread_repository|ThreadRepository|create|C
zeroth.integrations.persistence.runs.thread_repository|ThreadRepository|get|R
zeroth.integrations.persistence.runs.thread_repository|ThreadRepository|get_active_run_id|R
zeroth.integrations.persistence.runs.thread_repository|ThreadRepository|get_latest_run_id|R
zeroth.integrations.persistence.runs.thread_repository|ThreadRepository|list|N
zeroth.integrations.persistence.runs.thread_repository|ThreadRepository|list_run_ids|R
zeroth.integrations.persistence.runs.thread_repository|ThreadRepository|resolve|CRU
zeroth.integrations.persistence.runs.thread_repository|ThreadRepository|set_active_run_id|U
zeroth.integrations.persistence.runs.thread_repository|ThreadRepository|update|U
zeroth.integrations.persistence.runs.token_snapshot_store|TokenSnapshotRowStore|compare_and_swap|CU
zeroth.integrations.persistence.runs.token_snapshot_store|TokenSnapshotRowStore|get|R
zeroth.platform.dispatch.operations|SideEffectOperationStore|claim|CRU
zeroth.platform.dispatch.operations|SideEffectOperationStore|complete|U
zeroth.platform.dispatch.operations|SideEffectOperationStore|erase_for_run|ND
zeroth.platform.dispatch.operations|SideEffectOperationStore|erase_for_run_in_transaction|ND
zeroth.platform.dispatch.operations|SideEffectOperationStore|fail|U
zeroth.platform.dispatch.operations|SideEffectOperationStore|get|R
zeroth.platform.dispatch.operations|SideEffectOperationStore|mark_ambiguous|U
zeroth.platform.dispatch.operations|SideEffectOperationStore|pending_reconciliation|N
zeroth.platform.dispatch.operations|SideEffectOperationStore|record_reconciliation|RU
zeroth.platform.dispatch.operations|SideEffectOperationStore|state_of|R
zeroth.service.deployments.repository|SQLiteDeploymentRepository|create|CRU
zeroth.service.deployments.repository|SQLiteDeploymentRepository|get|R
zeroth.service.deployments.repository|SQLiteDeploymentRepository|list|N
zeroth.service.deployments.repository|SQLiteDeploymentRepository|next_version|R
zeroth.service.github.repository|SQLiteGitHubRepository|get_installation|R
zeroth.service.github.repository|SQLiteGitHubRepository|get_repository|R
zeroth.service.github.repository|SQLiteGitHubRepository|list_installations|N
zeroth.service.github.repository|SQLiteGitHubRepository|list_repositories|N
zeroth.service.github.repository|SQLiteGitHubRepository|prune_deliveries|ND
zeroth.service.github.repository|SQLiteGitHubRepository|record_delivery|C
zeroth.service.github.repository|SQLiteGitHubRepository|set_installation_status|U
zeroth.service.github.repository|SQLiteGitHubRepository|set_repository_status|U
zeroth.service.github.repository|SQLiteGitHubRepository|upsert_installation|CRU
zeroth.service.github.repository|SQLiteGitHubRepository|upsert_repository|CRU
zeroth.service.langgraph_gateway.enforcement_store|LangGraphEnforcementRepository|count_decisions|N
zeroth.service.langgraph_gateway.enforcement_store|LangGraphEnforcementRepository|get_attestation|N
zeroth.service.langgraph_gateway.enforcement_store|LangGraphEnforcementRepository|get_attestation_by_run_id|R
zeroth.service.langgraph_gateway.enforcement_store|LangGraphEnforcementRepository|get_inventory|R
zeroth.service.langgraph_gateway.enforcement_store|LangGraphEnforcementRepository|heartbeat|RU
zeroth.service.langgraph_gateway.enforcement_store|LangGraphEnforcementRepository|register_inventory|CU
zeroth.service.langgraph_gateway.enforcement_store|LangGraphEnforcementRepository|save_attestation|CR
zeroth.service.langgraph_gateway.enforcement_store|LangGraphEnforcementRepository|save_decision|CR
zeroth.service.langgraph_gateway.enforcement_store|StoredCapabilityEvidenceProvider|evidence_for_governance_run|R
zeroth.service.langgraph_gateway.enforcement_store|StoredCapabilityEvidenceProvider|evidence_for_run|N
zeroth.service.repositories.repository|SQLiteRepoCheckoutRepository|create|C
zeroth.service.repositories.repository|SQLiteRepoCheckoutRepository|expire_stale|NU
zeroth.service.repositories.repository|SQLiteRepoCheckoutRepository|get|R
zeroth.service.repositories.repository|SQLiteRepoCheckoutRepository|list_checkouts|N
zeroth.service.repositories.repository|SQLiteRepoCheckoutRepository|record_attestation|U
zeroth.service.repositories.repository|SQLiteRepoCheckoutRepository|record_failure|U
zeroth.service.repositories.repository|SQLiteRepoCheckoutRepository|transition_state|U
zeroth.service.repositories.repository|SQLiteRepoRunRepository|cancel|U
zeroth.service.repositories.repository|SQLiteRepoRunRepository|claim_pending|RNU
zeroth.service.repositories.repository|SQLiteRepoRunRepository|create|C
zeroth.service.repositories.repository|SQLiteRepoRunRepository|fail|U
zeroth.service.repositories.repository|SQLiteRepoRunRepository|finish|U
zeroth.service.repositories.repository|SQLiteRepoRunRepository|get|R
zeroth.service.repositories.repository|SQLiteRepoRunRepository|list_runs|N
zeroth.service.webhooks.repository|WebhookRepository|claim_pending_delivery|RNU
zeroth.service.webhooks.repository|WebhookRepository|create_subscription|C
zeroth.service.webhooks.repository|WebhookRepository|deactivate_subscription|U
zeroth.service.webhooks.repository|WebhookRepository|dead_letter|CRU
zeroth.service.webhooks.repository|WebhookRepository|delete_subscription|D
zeroth.service.webhooks.repository|WebhookRepository|enqueue_delivery|C
zeroth.service.webhooks.repository|WebhookRepository|get_dead_letter|R
zeroth.service.webhooks.repository|WebhookRepository|get_subscription|R
zeroth.service.webhooks.repository|WebhookRepository|list_dead_letters|N
zeroth.service.webhooks.repository|WebhookRepository|list_subscriptions|N
zeroth.service.webhooks.repository|WebhookRepository|list_subscriptions_for_event|N
zeroth.service.webhooks.repository|WebhookRepository|mark_delivered|U
zeroth.service.webhooks.repository|WebhookRepository|mark_failed|RU
""".splitlines()

METHOD_METADATA_ORACLE = MappingProxyType(
    {
        (module_name, qualname, method_name): frozenset(_CODES[code] for code in codes)
        for row in _ORACLE_ROWS
        for module_name, qualname, method_name, codes in (row.split("|"),)
    }
)


def _identity(repository_type: type, method_name: str) -> tuple[str, str, str]:
    return repository_type.__module__, repository_type.__qualname__, method_name


def _discovered_method_operations():
    actual = {}
    for surface in load_service_persistence_surfaces():
        for method_name, operations in surface.operation_methods.items():
            key = _identity(surface.repository_type, method_name)
            actual[key] = actual.get(key, frozenset()) | operations
    return actual


def _runtime_method_metadata():
    repository_types = {surface.repository_type for surface in load_service_persistence_surfaces()}
    return {
        _identity(repository_type, method_name): method.__persistence_operations__
        for repository_type in repository_types
        for method_name, method in vars(repository_type).items()
        if not method_name.startswith("_")
        and inspect.isfunction(method)
        and hasattr(method, "__persistence_operations__")
    }


def _assert_exact_metadata(actual):
    assert actual == METHOD_METADATA_ORACLE


def _assert_runtime_metadata() -> None:
    for surface in load_service_persistence_surfaces():
        validate_persistence_surface(surface)
    _assert_exact_metadata(_runtime_method_metadata())


def test_discovered_repository_type_is_the_exported_class_identity() -> None:
    mismatches = []
    for repository_type in {
        surface.repository_type for surface in load_service_persistence_surfaces()
    }:
        exported = importlib.import_module(repository_type.__module__)
        for part in repository_type.__qualname__.split("."):
            exported = getattr(exported, part)
        if exported is not repository_type:
            mismatches.append((repository_type.__module__, repository_type.__qualname__))

    assert sorted(mismatches) == []


def test_exact_metadata_oracle_covers_every_discovered_method_identity() -> None:
    assert len(METHOD_METADATA_ORACLE) == 206
    _assert_exact_metadata(_discovered_method_operations())
    _assert_runtime_metadata()


def test_exact_metadata_oracle_rejects_an_undecorated_public_method(monkeypatch) -> None:
    repository_type = next(iter(load_service_persistence_surfaces())).repository_type
    monkeypatch.setattr(repository_type, "persist_new", lambda self: None, raising=False)

    with pytest.raises(AssertionError, match="persist_new"):
        _assert_runtime_metadata()


def test_exact_metadata_oracle_is_immutable() -> None:
    identity = next(iter(METHOD_METADATA_ORACLE))
    with pytest.raises(TypeError):
        METHOD_METADATA_ORACLE[identity] = frozenset({O.READ})


@pytest.mark.parametrize("mutation", ["missing", "extra", "wrong"])
def test_exact_metadata_oracle_rejects_inventory_mutations(mutation: str) -> None:
    actual = dict(METHOD_METADATA_ORACLE)
    approval_write = (
        "zeroth.governance.approvals.repository",
        "ApprovalRepository",
        "write",
    )
    if mutation == "missing":
        actual.pop(approval_write)
    elif mutation == "extra":
        actual[("zeroth.synthetic", "SyntheticRepository", "persist")] = frozenset({O.READ})
    else:
        actual[approval_write] = frozenset({O.CREATE})
    with pytest.raises(AssertionError):
        _assert_exact_metadata(actual)
