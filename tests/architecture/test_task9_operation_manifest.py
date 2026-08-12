"""Registry-derived proof for every Task 9 repository persistence operation."""

from __future__ import annotations

import inspect
from dataclasses import replace

import pytest

from zeroth.governance.retention.audit_log_repository import RetentionAuditLogRepository
from zeroth.governance.retention.cleanup_state_repository import CleanupStateRepository
from zeroth.governance.retention.coordination import RetentionCoordinator
from zeroth.governance.retention.legal_hold_repository import LegalHoldRepository
from zeroth.governance.retention.policy_repository import (
    EnabledPolicyMaintenanceReader,
    RetentionPolicyRepository,
)
from zeroth.governance.retention.workspace_reader import RetentionWorkspaceMaintenanceReader
from zeroth.governance.guardrails.rate_limit import QuotaEnforcer, TokenBucketRateLimiter
from zeroth.integrations.persistence.runs.checkpoint_store import CheckpointRowStore
from zeroth.integrations.persistence.runs.run_repository import RunRepository
from zeroth.integrations.persistence.runs.thread_repository import ThreadRepository
from zeroth.integrations.persistence.runs.token_snapshot_store import TokenSnapshotRowStore
from zeroth.platform.dispatch.operations import SideEffectOperationStore
from zeroth.platform.storage import (
    SERVICE_SCOPE_REGISTRY,
    ResourceOperation,
    ResourceScopeRegistry,
)
from zeroth.service.langgraph_gateway import enforcement as _enforcement  # noqa: F401
from zeroth.service.langgraph_gateway.enforcement_store import (
    LangGraphEnforcementRepository,
    StoredCapabilityEvidenceProvider,
)
from zeroth.service.webhooks.repository import WebhookRepository
from tests.task9_operation_driver_registry import TASK9_EXECUTABLE_DRIVERS

O = ResourceOperation  # noqa: E741 - compact operation matrix alias

# Each method is a real production surface. Multiple semantic operations are
# listed when one public transaction performs a lifecycle transition (for
# example an upsert or dead-letter move).
TASK9_OPERATION_MANIFEST: dict[str, dict[type, dict[str, frozenset[ResourceOperation]]]] = {
    "service.quota_counters": {
        QuotaEnforcer: {
            "check_and_increment": frozenset({O.CREATE, O.READ, O.UPDATE}),
            "get": frozenset({O.READ}),
        }
    },
    "service.rate_limit_buckets": {
        TokenBucketRateLimiter: {
            "check_and_consume": frozenset({O.CREATE, O.READ, O.UPDATE}),
            "get": frozenset({O.READ}),
        }
    },
    "service.runs": {
        RunRepository: {
            "create": frozenset({O.CREATE}),
            "get": frozenset({O.READ}),
            "list_runs": frozenset({O.ENUMERATE}),
            "list_dead_letter_runs": frozenset({O.ENUMERATE}),
            "put": frozenset({O.CREATE, O.UPDATE}),
            "transition": frozenset({O.READ, O.UPDATE}),
            "record_history": frozenset({O.READ, O.UPDATE}),
            "record_condition_result": frozenset({O.READ, O.UPDATE}),
            "increment_failure_count": frozenset({O.UPDATE}),
            "delete": frozenset({O.DELETE}),
            "count_pending": frozenset({O.ENUMERATE}),
            "redact_run": frozenset({O.READ, O.UPDATE}),
            "redact_run_in_transaction": frozenset({O.READ, O.UPDATE}),
            "erasure_payloads_in_transaction": frozenset({O.READ}),
            "tenant_id_for_run_in_transaction": frozenset({O.READ}),
            "list_erasable_run_ids": frozenset({O.ENUMERATE}),
            "lock_and_recheck_erasable_run": frozenset({O.READ}),
            "fence_token_snapshot_writes_in_transaction": frozenset({O.READ, O.UPDATE}),
        },
        RetentionWorkspaceMaintenanceReader: {
            "list_workspace_ids": frozenset({O.ENUMERATE}),
        },
    },
    "service.threads": {
        ThreadRepository: {
            "create": frozenset({O.CREATE}),
            "get": frozenset({O.READ}),
            "list": frozenset({O.ENUMERATE}),
            "update": frozenset({O.UPDATE}),
            "resolve": frozenset({O.CREATE, O.READ, O.UPDATE}),
            "attach_run": frozenset({O.READ, O.UPDATE}),
            "get_active_run_id": frozenset({O.READ}),
            "get_latest_run_id": frozenset({O.READ}),
            "list_run_ids": frozenset({O.READ}),
            "set_active_run_id": frozenset({O.UPDATE}),
        },
        RunRepository: {
            "get_active_run_id": frozenset({O.READ}),
            "get_latest_run_id": frozenset({O.READ}),
            "list_run_ids": frozenset({O.READ}),
            "set_active_run_id": frozenset({O.UPDATE}),
            "clear_active_run_id": frozenset({O.UPDATE}),
        },
    },
    "service.run_checkpoints": {
        CheckpointRowStore: {
            "write_row": frozenset({O.CREATE, O.UPDATE}),
            "write_row_in_connection": frozenset({O.CREATE, O.UPDATE}),
            "write_row_bound": frozenset({O.CREATE, O.UPDATE}),
            "get": frozenset({O.READ}),
            "latest_id_for_run": frozenset({O.READ}),
            "list_ids": frozenset({O.ENUMERATE}),
            "delete": frozenset({O.DELETE}),
        },
        RunRepository: {
            "write_checkpoint": frozenset({O.CREATE, O.UPDATE}),
            "get_checkpoint": frozenset({O.READ}),
            "get_latest_checkpoint": frozenset({O.READ}),
            "get_latest_checkpoint_id_for_run": frozenset({O.READ}),
            "list_checkpoints": frozenset({O.ENUMERATE}),
            "erase_checkpoints_for_run": frozenset({O.DELETE}),
            "erase_checkpoints_for_run_in_transaction": frozenset({O.DELETE}),
        },
    },
    "service.token_engine_snapshots": {
        TokenSnapshotRowStore: {
            "get": frozenset({O.READ}),
            "compare_and_swap": frozenset({O.CREATE, O.UPDATE}),
        },
        RunRepository: {
            "get_token_snapshot": frozenset({O.READ}),
            "compare_and_swap_token_snapshot": frozenset({O.CREATE, O.UPDATE}),
            "erase_token_snapshot_for_run_in_transaction": frozenset({O.DELETE}),
            "fence_and_erase_token_snapshot_for_run_in_transaction": frozenset({O.DELETE}),
        },
    },
    "service.side_effect_operations": {
        SideEffectOperationStore: {
            "get": frozenset({O.READ}),
            "state_of": frozenset({O.READ}),
            "pending_reconciliation": frozenset({O.ENUMERATE}),
            "claim": frozenset({O.CREATE, O.READ, O.UPDATE}),
            "complete": frozenset({O.UPDATE}),
            "fail": frozenset({O.UPDATE}),
            "mark_ambiguous": frozenset({O.UPDATE}),
            "record_reconciliation": frozenset({O.READ, O.UPDATE}),
            "erase_for_run": frozenset({O.ENUMERATE, O.DELETE}),
            "erase_for_run_in_transaction": frozenset({O.ENUMERATE, O.DELETE}),
        }
    },
    "service.webhook_subscriptions": {
        WebhookRepository: {
            "create_subscription": frozenset({O.CREATE}),
            "get_subscription": frozenset({O.READ}),
            "list_subscriptions": frozenset({O.ENUMERATE}),
            "list_subscriptions_for_event": frozenset({O.ENUMERATE}),
            "deactivate_subscription": frozenset({O.UPDATE}),
            "delete_subscription": frozenset({O.DELETE}),
        }
    },
    "service.webhook_deliveries": {
        WebhookRepository: {
            "enqueue_delivery": frozenset({O.CREATE}),
            "claim_pending_delivery": frozenset({O.READ, O.ENUMERATE, O.UPDATE}),
            "mark_delivered": frozenset({O.UPDATE}),
            "mark_failed": frozenset({O.READ, O.UPDATE}),
            "dead_letter": frozenset({O.READ, O.UPDATE}),
        }
    },
    "service.webhook_dead_letters": {
        WebhookRepository: {
            "dead_letter": frozenset({O.CREATE}),
            "list_dead_letters": frozenset({O.ENUMERATE}),
            "get_dead_letter": frozenset({O.READ}),
        }
    },
    "service.retention_policies": {
        RetentionPolicyRepository: {
            "get": frozenset({O.READ}),
            "resolve": frozenset({O.READ}),
            "upsert": frozenset({O.CREATE, O.READ, O.UPDATE}),
            "list_for_tenant": frozenset({O.ENUMERATE}),
        },
        EnabledPolicyMaintenanceReader: {
            "list_all_enabled_for_maintenance": frozenset({O.ENUMERATE}),
        },
    },
    "service.legal_holds": {
        LegalHoldRepository: {
            "place": frozenset({O.CREATE}),
            "release": frozenset({O.READ, O.UPDATE}),
            "place_in_transaction": frozenset({O.CREATE}),
            "release_in_transaction": frozenset({O.READ, O.UPDATE}),
            "get": frozenset({O.READ}),
            "list_for_tenant": frozenset({O.ENUMERATE}),
            "active_holds_for_tenant": frozenset({O.READ, O.ENUMERATE}),
            "active_holds_for_tenant_in_transaction": frozenset({O.READ, O.ENUMERATE}),
        }
    },
    "service.retention_audit_log": {
        RetentionAuditLogRepository: {
            "record": frozenset({O.CREATE}),
            "get": frozenset({O.READ}),
            "record_in_transaction": frozenset({O.CREATE}),
            "get_in_transaction": frozenset({O.READ}),
            "list_for_run_in_transaction": frozenset({O.ENUMERATE}),
            "list_for_tenant": frozenset({O.ENUMERATE}),
            "list_for_run": frozenset({O.ENUMERATE}),
        }
    },
    "service.retention_cleanup_state": {
        CleanupStateRepository: {
            "initialize_in_transaction": frozenset({O.CREATE}),
            "get_state_in_transaction": frozenset({O.READ}),
            "claim_in_transaction": frozenset({O.READ, O.UPDATE}),
            "heartbeat_in_transaction": frozenset({O.READ, O.UPDATE}),
            "release_in_transaction": frozenset({O.READ, O.UPDATE}),
            "terminal_in_transaction": frozenset({O.READ, O.UPDATE}),
            "repair_terminal_in_transaction": frozenset({O.READ, O.UPDATE}),
        }
    },
    "service.retention_cleanup_operations": {
        CleanupStateRepository: {
            "initialize_in_transaction": frozenset({O.CREATE}),
            "get_operation_in_transaction": frozenset({O.READ}),
            "list_operations_in_transaction": frozenset({O.ENUMERATE}),
            "update_operation_in_transaction": frozenset({O.READ, O.UPDATE}),
        }
    },
    "service.retention_coordination": {
        RetentionCoordinator: {
            "transaction": frozenset({O.CREATE, O.READ}),
        }
    },
    "service.langgraph_decisions": {
        LangGraphEnforcementRepository: {
            "save_decision": frozenset({O.CREATE, O.READ}),
            "count_decisions": frozenset({O.ENUMERATE}),
        }
    },
    "service.langgraph_inventories": {
        LangGraphEnforcementRepository: {
            "register_inventory": frozenset({O.CREATE, O.UPDATE}),
            "get_inventory": frozenset({O.READ}),
            "heartbeat": frozenset({O.READ, O.UPDATE}),
        }
    },
    "service.langgraph_run_attestations": {
        LangGraphEnforcementRepository: {
            "save_attestation": frozenset({O.CREATE, O.READ}),
            "get_attestation": frozenset({O.ENUMERATE}),
            "get_attestation_by_run_id": frozenset({O.READ}),
        },
        StoredCapabilityEvidenceProvider: {
            "evidence_for_run": frozenset({O.ENUMERATE}),
            "evidence_for_governance_run": frozenset({O.READ}),
        },
    },
}


def _manifest_operations(resource_name: str) -> frozenset[ResourceOperation]:
    return frozenset(
        operation
        for methods in TASK9_OPERATION_MANIFEST[resource_name].values()
        for operations in methods.values()
        for operation in operations
    )


@pytest.mark.parametrize("resource_name", sorted(TASK9_OPERATION_MANIFEST))
def test_task9_manifest_exactly_matches_registry_operations(resource_name: str) -> None:
    """No registered operation lacks a real public method and none is extra."""
    declared = SERVICE_SCOPE_REGISTRY.definition_for_resource(resource_name).operations
    assert _manifest_operations(resource_name) == declared


def test_task9_manifest_names_real_public_repository_methods() -> None:
    for repositories in TASK9_OPERATION_MANIFEST.values():
        for repository_type, methods in repositories.items():
            for method_name in methods:
                method = vars(repository_type).get(method_name)
                assert method is not None, f"{repository_type.__name__}.{method_name} is not public"
                assert inspect.isfunction(method), f"{repository_type.__name__}.{method_name}"


def test_task9_guardrail_manifest_matches_production_operation_metadata() -> None:
    for resource_name in ("service.quota_counters", "service.rate_limit_buckets"):
        for repository_type, methods in TASK9_OPERATION_MANIFEST[resource_name].items():
            for method_name, operations in methods.items():
                method = vars(repository_type)[method_name]
                assert getattr(method, "__persistence_operations__", frozenset()) == operations


def test_task9_manifest_covers_every_public_persistence_method() -> None:
    """Only explicitly non-persistent helpers may be absent from the manifest."""
    repository_types = {
        repository_type
        for repositories in TASK9_OPERATION_MANIFEST.values()
        for repository_type in repositories
    }
    non_persistent_helpers = {
        RunRepository: {"install_fence", "clear_fence"},
        CheckpointRowStore: {"encrypt_state_json", "decrypt_state_json"},
    }
    for repository_type in repository_types:
        actual = {
            name
            for name, method in vars(repository_type).items()
            if not name.startswith("_") and inspect.isfunction(method)
        } - non_persistent_helpers.get(repository_type, set())
        declared = {
            name
            for repositories in TASK9_OPERATION_MANIFEST.values()
            for candidate, methods in repositories.items()
            if candidate is repository_type
            for name in methods
        }
        assert actual == declared, repository_type.__name__


def test_task9_manifest_detects_a_removed_real_operation() -> None:
    """Mutation check: narrowing a declared real operation must fail completeness."""
    definition = SERVICE_SCOPE_REGISTRY.definition_for_resource("service.runs")
    narrowed = replace(definition, operations=definition.operations - {O.DELETE})
    registry = ResourceScopeRegistry(
        narrowed if item.resource_name == definition.resource_name else item
        for item in SERVICE_SCOPE_REGISTRY.definitions
    )
    assert (
        _manifest_operations("service.runs")
        != registry.definition_for_resource("service.runs").operations
    )


def _manifest_operation_pairs() -> set[tuple[str, ResourceOperation]]:
    return {
        (resource_name, operation)
        for resource_name in TASK9_OPERATION_MANIFEST
        for operation in _manifest_operations(resource_name)
    }


def test_task9_executable_driver_keys_exactly_match_manifest_pairs() -> None:
    assert set(TASK9_EXECUTABLE_DRIVERS) == _manifest_operation_pairs()


def test_task9_driver_completeness_rejects_one_removed_key() -> None:
    drivers = dict(TASK9_EXECUTABLE_DRIVERS)
    drivers.pop(next(iter(drivers)), None)
    assert set(drivers) != _manifest_operation_pairs()
