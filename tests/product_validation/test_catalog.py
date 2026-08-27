from __future__ import annotations

import json
from pathlib import Path

from release.product_validation.catalog import ProductValidationCatalog


ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = ROOT / "release/product_validation/catalog-v1.json"
EVIDENCE_INDEX_PATH = ROOT / "release/product_validation/evidence-index-v1.json"


def test_catalog_covers_every_published_console_route_and_required_capability() -> None:
    catalog = ProductValidationCatalog.model_validate_json(CATALOG_PATH.read_text())

    assert catalog.schema_version == 1
    assert catalog.console_routes == {
        "/",
        "/approvals",
        "/artifacts",
        "/audit",
        "/connectors",
        "/cost",
        "/deployments",
        "/guide",
        "/metrics",
        "/regulus",
        "/regulus/capabilities",
        "/regulus/costing",
        "/regulus/enforcement",
        "/regulus/reconciliation",
        "/retention",
        "/rightsizing",
        "/runs",
        "/studio",
        "/studio/edit",
        "/templates",
        "/webhooks",
    }
    assert {
        "agent-tool-use",
        "artifacts",
        "audit",
        "approvals",
        "batching-and-joins",
        "connectors-and-retrieval",
        "context-windows-and-threads",
        "deployments-and-rollbacks",
        "economics-and-rightsizing",
        "inline-and-manifest-code",
        "loops",
        "retention-and-erasure",
        "subgraphs",
        "templates",
        "tenant-and-role-isolation",
        "webhooks",
    }.issubset(catalog.capability_ids)


def test_catalog_requires_control_and_checkpoint_contracts_for_every_capability() -> None:
    catalog = ProductValidationCatalog.model_validate_json(CATALOG_PATH.read_text())

    for capability in catalog.capabilities:
        assert capability.routes
        assert capability.control_patterns
        assert capability.backend_operations
        assert set(capability.checkpoints) >= {"configured", "result", "persistence"}
        assert capability.screenshot_required is True
        assert capability.runtime_evidence_required is True
        assert capability.roles


def test_catalog_pins_the_field_level_equivalence_contract() -> None:
    catalog = ProductValidationCatalog.model_validate_json(CATALOG_PATH.read_text())

    assert catalog.field_level_contract.select_options == "every_enabled_option"
    assert catalog.field_level_contract.checkbox_states == (False, True)
    assert catalog.field_level_contract.required_states == (
        "representative_valid",
        "required_empty",
        "type_or_syntax_invalid",
        "boundary_minimum",
        "boundary_maximum",
        "security_boundary",
        "save_refresh_reopen",
        "keyboard_and_focus",
        "role_denial_when_scoped",
    )


def test_catalog_backend_operations_cover_stable_public_openapi() -> None:
    catalog = ProductValidationCatalog.model_validate_json(CATALOG_PATH.read_text())
    document = json.loads((ROOT / "frontend/openapi.json").read_text())

    result = catalog.compare_openapi(document)

    assert result.unmapped == ()
    assert result.invalid_exclusions == ()


def test_product_evidence_index_targets_the_catalog_it_covers() -> None:
    catalog = ProductValidationCatalog.model_validate_json(CATALOG_PATH.read_text())
    index = json.loads(EVIDENCE_INDEX_PATH.read_text())

    assert index["schema_version"] == catalog.schema_version
    assert index["catalog_id"] == catalog.catalog_id


def test_product_evidence_index_has_exactly_one_entry_per_catalog_capability() -> None:
    catalog = ProductValidationCatalog.model_validate_json(CATALOG_PATH.read_text())
    index = json.loads(EVIDENCE_INDEX_PATH.read_text())
    indexed_ids = [item["capability_id"] for item in index["entries"]]

    assert len(indexed_ids) == len(set(indexed_ids))
    assert set(indexed_ids) == catalog.capability_ids


def test_runs_catalogs_authorized_ambiguous_operation_resolution() -> None:
    catalog = ProductValidationCatalog.model_validate_json(CATALOG_PATH.read_text())
    index = json.loads(EVIDENCE_INDEX_PATH.read_text())

    runs = next(item for item in catalog.capabilities if item.capability_id == "runs")
    assert "POST /v1/deployments/*/operations/*/resolve" in runs.backend_operations

    evidence = next(item for item in index["entries"] if item["capability_id"] == "runs")
    assert "ambiguous-operation-resolution-ui-accepted-20260826-1" in evidence[
        "supplemental_source_roots"
    ]
    assert {
        "runs.ambiguous-operation-control-conditional",
        "runs.ambiguous-operation-authorized-resolution",
        "runs.ambiguous-operation-signed-refresh-no-replay",
    }.issubset(evidence["evidence_criteria"])


def test_economics_catalogs_provider_independent_measured_and_estimated_truth() -> None:
    catalog = ProductValidationCatalog.model_validate_json(CATALOG_PATH.read_text())
    index = json.loads(EVIDENCE_INDEX_PATH.read_text())

    capability = next(
        item for item in catalog.capabilities if item.capability_id == "economics-and-rightsizing"
    )
    assert {
        "tenant-deployment-run-measured-reconciliation",
        "estimated-failed-run-tax-separation",
    }.issubset(capability.checkpoints)

    evidence = next(
        item for item in index["entries"] if item["capability_id"] == "economics-and-rightsizing"
    )
    assert "economics-provider-independent-ui-20260826-1" in evidence[
        "supplemental_source_roots"
    ]
    assert {
        "economics.tenant-deployment-run-measured-reconciliation",
        "economics.estimated-failed-run-tax-separation",
    }.issubset(evidence["evidence_criteria"])
    assert {
        "measured_live_provider_experiment",
        "candidate_and_judge_cost_reconciliation",
    }.issubset(evidence["remaining_checkpoints"])


def test_economics_maps_native_safari_boundary_without_claiming_paid_measurement() -> None:
    index = json.loads(EVIDENCE_INDEX_PATH.read_text())
    evidence = next(
        item for item in index["entries"] if item["capability_id"] == "economics-and-rightsizing"
    )

    assert "native-safari-rightsizing-boundary-accepted-20260826-1" in evidence[
        "supplemental_source_roots"
    ]
    assert "native_safari_boundary_submit_feedback" in evidence["passed_checkpoints"]
    assert "native_safari_boundary_submit_feedback" not in evidence["remaining_checkpoints"]
    assert evidence["remaining_checkpoints"] == [
        "measured_live_provider_experiment",
        "candidate_and_judge_cost_reconciliation",
    ]
    assert evidence["status"] == "blocked"


def test_templates_and_artifacts_catalog_exact_provider_independent_closure() -> None:
    catalog = ProductValidationCatalog.model_validate_json(CATALOG_PATH.read_text())
    index = json.loads(EVIDENCE_INDEX_PATH.read_text())

    artifacts = next(item for item in catalog.capabilities if item.capability_id == "artifacts")
    assert {
        "workflow-output",
        "preview-download",
        "tenant-isolation",
        "expiry",
        "erasure",
    }.issubset(artifacts.checkpoints)

    templates = next(item for item in catalog.capabilities if item.capability_id == "templates")
    assert {
        "secret-redaction",
        "field-validation",
        "versioning",
        "twin-tenant-scope-isolation",
        "refresh-persistence",
        "backend-restart-persistence",
        "delete-version",
        "keyboard",
        "accessibility",
    }.issubset(templates.checkpoints)

    for capability_id in ("artifacts", "templates"):
        evidence = next(item for item in index["entries"] if item["capability_id"] == capability_id)
        assert "templates-artifacts-product-exact-20260826-2" in evidence[
            "supplemental_source_roots"
        ]
    templates_evidence = next(
        item for item in index["entries"] if item["capability_id"] == "templates"
    )
    assert "live_template_backed_provider_execution" in templates_evidence[
        "remaining_checkpoints"
    ]


def test_product_evidence_index_statuses_reflect_executed_checkpoint_semantics() -> None:
    index = json.loads(EVIDENCE_INDEX_PATH.read_text())

    for entry in index["entries"]:
        assert entry["status"] in {"pass", "fail", "blocked", "not_run"}
        assert entry["source_root"]
        assert isinstance(entry["passed_checkpoints"], list)
        assert isinstance(entry["remaining_checkpoints"], list)

        if entry["status"] == "pass":
            assert entry["passed_checkpoints"]
            assert entry["remaining_checkpoints"] == []
        elif entry["status"] == "blocked":
            assert entry["passed_checkpoints"]
            assert entry["remaining_checkpoints"]
        elif entry["status"] == "fail":
            # A deterministic product failure may happen before any checkpoint
            # passes, or after partial progress.  In either case unresolved work
            # remains and the ledger must not collapse it into ``blocked``.
            assert entry["remaining_checkpoints"]
        else:
            assert entry["passed_checkpoints"] == []
            assert entry["remaining_checkpoints"]


def test_product_evidence_index_closes_resilient_http_in_all_three_browsers() -> None:
    index = json.loads(EVIDENCE_INDEX_PATH.read_text())
    entry = next(
        item for item in index["entries"] if item["capability_id"] == "resilient-http"
    )

    assert entry["source_root"] == "resilient-http-dual-browser-accepted-20260826-1"
    assert entry["supplemental_source_roots"] == [
        "resilient-http-accepted-20260826-1",
        "native-safari-resilient-http-accepted-20260826-1",
    ]
    assert entry["status"] == "pass"
    assert "chromium_and_webkit_functional_journeys" in entry["passed_checkpoints"]
    assert "native_safari_functional_journey" in entry["passed_checkpoints"]
    assert entry["remaining_checkpoints"] == []


def test_product_evidence_index_maps_retention_native_safari_checkpoint() -> None:
    index = json.loads(EVIDENCE_INDEX_PATH.read_text())

    assert index["schema_version"] == 1
    assert index["control_inventory"] == {
        "source_root": "product-surface-inventory-deep-accepted-20260826-1",
        "supplemental_source_roots": [
            "native-safari-docker-recovery-inventory-20260825-1",
            "native-safari-product-surface-recovery-20260825-1",
        ],
        "status": "pass",
        "route_count": 21,
        "unnamed_control_count": 0,
        "uncataloged_control_count": 0,
        "passed_checkpoints": [
            "persistent_docker_recovery",
            "all_routes_hot_after_recovery",
            "all_visible_controls_cataloged",
            "select_checkbox_and_radio_state_inventory",
            "native_safari_fresh_tab_render",
        ],
    }
    entry = next(
        item for item in index["entries"] if item["capability_id"] == "retention-and-erasure"
    )
    assert entry["source_root"] == ("native-safari-retention-validation-checkpoint-20260825-1")
    assert entry["supplemental_source_roots"] == [
        "retention-compliance-live-checkpoint-20260825-1",
        "identity-isolation-live-checkpoint-20260825-1",
        "native-safari-operator-denial-checkpoint-20260825-1",
        "retention-visual-matrix-checkpoint-20260825-1",
        "retention-dedicated-checkpoint-20260825-1",
        "playwright-retention-erasure-live-20260825-4",
        "native-safari-retention-erasure-20260825-1",
        "native-safari-retention-econ-erasure-20260825-1",
        "playwright-retention-ttl-boundary-live-20260825-4",
        "native-safari-retention-ttl-boundary-20260825-1",
        "retention-held-refusal-current-accepted-20260826-1",
        "retention-erasure-evidence-repair-accepted-20260826-1",
    ]
    assert entry["route"] == "/retention"
    assert entry["status"] == "pass"
    assert entry["passed_checkpoints"] == [
        "native_safari_paint",
        "invalid_ttl_validation",
        "refresh_restoration",
        "policy_boundary_and_disabled_persistence",
        "legal_hold_create_refresh_release",
        "legal_hold_role_denial",
        "retention_destructive_controls_hidden_on_denial",
        "tenant_isolation",
        "retention_cross_tenant_error_only_matrix",
        "native_safari_role_denial",
        "responsive_and_zoom_matrix",
        "webkit_axe_and_keyboard_matrix",
        "held_erasure_refusal",
        "nonheld_run_erasure",
        "tenant_erasure",
        "audit_chain_after_erasure",
        "economics_reconciliation_after_erasure",
        "configured_economics_adapter_erasure",
        "erasure_history_refresh_restoration",
        "ttl_blank_and_effective_maximum_persistence",
        "current_build_held_erasure_refusal_revalidated",
    ]
    assert "legal_hold_role_denial" not in entry["remaining_checkpoints"]
    assert "tenant_isolation" not in entry["remaining_checkpoints"]
    assert "responsive_and_zoom_matrix" not in entry["remaining_checkpoints"]
    assert "webkit_axe_and_keyboard_matrix" not in entry["remaining_checkpoints"]
    assert entry["evidence_criteria"] == [
        "product.retention.native-safari-paint",
        "product.retention.invalid-ttl-validation",
        "product.retention.refresh-restoration",
        "identity.retention-tenant-isolation",
        "product.identity.native-safari-role-denial",
        "product.retention.responsive-and-zoom",
        "product.retention.webkit-axe-and-keyboard",
        "retention-and-erasure.held-erasure-refusal",
        "retention-and-erasure.nonheld-run-erasure",
        "retention-and-erasure.tenant-erasure",
        "retention-and-erasure.audit-chain-after-erasure",
        "retention-and-erasure.economics-after-erasure",
        "retention.erasure.configured-econ-adapter-reached",
        "retention-and-erasure.boundary",
        "retention-and-erasure.persistence",
        "fields.retention-policy",
    ]
    assert entry["remaining_checkpoints"] == []
