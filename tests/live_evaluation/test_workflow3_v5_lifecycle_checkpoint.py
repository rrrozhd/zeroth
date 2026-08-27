from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from release.live_evaluation import workflow3_v5_lifecycle_checkpoint as checkpoint


def _proof() -> dict[str, object]:
    return {
        "workflow": {
            "id": checkpoint.WORKFLOW_ID,
            "status": "published",
            "version": 5,
            "nodes": [{"id": f"n{index}"} for index in range(4)],
            "edges": [{"id": f"e{index}"} for index in range(4)],
        },
        "diff": {
            "left_graph_id": checkpoint.WORKFLOW_ID,
            "left_version": 4,
            "right_graph_id": checkpoint.WORKFLOW_ID,
            "right_version": 5,
            "node_changes": [],
            "edge_changes": [],
            "contract_changes": [],
            "policy_changes": [],
            "condition_changes": [],
            "memory_connector_changes": [],
            "executable_unit_binding_changes": [],
        },
        "fresh_deployment": {
            "deployment_ref": checkpoint.FRESH_DEPLOYMENT,
            "version": 1,
            "graph_version_ref": checkpoint.FRESH_GRAPH,
            "status": "active",
            "serving": False,
        },
        "stable_deployment": {
            "deployment_ref": checkpoint.STABLE_DEPLOYMENT,
            "version": 3,
            "graph_version_ref": checkpoint.STABLE_GRAPH,
            "status": "active",
            "serving": True,
        },
        "health": {
            "status": "ok",
            "deployment_ref": checkpoint.STABLE_DEPLOYMENT,
            "deployment_version": 3,
            "graph_version_ref": checkpoint.STABLE_GRAPH,
            "campaign_id": checkpoint.TENANT,
        },
        "run": {
            "run_id": checkpoint.RUN_ID,
            "thread_id": checkpoint.RUN_ID,
            "status": "failed",
            "deployment_ref": checkpoint.FRESH_DEPLOYMENT,
            "graph_version_ref": checkpoint.FRESH_GRAPH,
            "tenant_id": checkpoint.TENANT,
            "workspace_id": None,
            "failure_state": {"reason": "approval_rejected"},
        },
        "approval": {
            "approval_id": checkpoint.APPROVAL_ID,
            "run_id": checkpoint.RUN_ID,
            "thread_id": checkpoint.RUN_ID,
            "deployment_ref": checkpoint.FRESH_DEPLOYMENT,
            "graph_version_ref": checkpoint.FRESH_GRAPH,
            "tenant_id": checkpoint.TENANT,
            "workspace_id": None,
            "status": "resolved",
            "created_at": "2026-08-24T22:00:00Z",
            "sla_deadline": "2026-08-24T22:00:05Z",
            "urgency_metadata": {"sla_timeout_seconds": 5},
            "resolution": {
                "decision": "reject",
                "resolved_at": "2026-08-24T22:00:05Z",
                "actor": {
                    "subject": "sla_enforcer",
                    "tenant_id": checkpoint.TENANT,
                    "workspace_id": None,
                },
            },
        },
        "audits": [
            {
                "audit_id": f"audit-{sequence}",
                "run_id": checkpoint.RUN_ID,
                "thread_id": checkpoint.RUN_ID,
                "deployment_ref": checkpoint.FRESH_DEPLOYMENT,
                "graph_version_ref": checkpoint.FRESH_GRAPH,
                "tenant_id": checkpoint.TENANT,
                "workspace_id": None,
                "chain_sequence": sequence,
                "record_digest": "digest",
                "record_signature": "signature",
                "signing_key_id": "key-id",
                "signing_algorithm": "hmac-sha256",
            }
            for sequence in range(1, 4)
        ],
        "verification": {
            "verified": True,
            "signature_verified": True,
            "record_count": 3,
            "unsigned_record_count": 0,
        },
        "side_effect_operation_rows": 0,
        "execution_event_rows": 0,
        "reservation_rows": 0,
        "action_markers_created_since_run": 0,
        "audit_readiness": {
            "ready": True,
            "state": "signed",
            "signer_available": True,
        },
    }


def test_validate_observations_accepts_exact_restored_v5_lifecycle() -> None:
    checkpoint.validate_observations(_proof())


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("workflow", "version"), 4),
        (("workflow", "nodes"), [{"id": "only-one"}]),
        (("diff", "node_changes"), [{"id": "changed"}]),
        (("fresh_deployment", "serving"), True),
        (("stable_deployment", "serving"), False),
        (("health", "graph_version_ref"), checkpoint.FRESH_GRAPH),
        (("run", "status"), "succeeded"),
        (("run", "failure_state", "reason"), "runtime_error"),
        (("approval", "urgency_metadata", "sla_timeout_seconds"), 10),
        (("approval", "resolution", "actor", "subject"), "operator"),
        (("audits",), []),
        (("verification", "signature_verified"), False),
        (("side_effect_operation_rows",), 1),
        (("execution_event_rows",), 1),
        (("reservation_rows",), 1),
        (("action_markers_created_since_run",), 1),
        (("audit_readiness", "state"), "local_unsigned"),
    ],
)
def test_validate_observations_fails_closed_on_broken_invariant(
    path: tuple[str, ...], value: object
) -> None:
    proof = deepcopy(_proof())
    cursor: object = proof
    for field in path[:-1]:
        assert isinstance(cursor, dict)
        cursor = cursor[field]
    assert isinstance(cursor, dict)
    cursor[path[-1]] = value

    with pytest.raises(RuntimeError):
        checkpoint.validate_observations(proof)


def test_required_native_artifacts_include_ax_menu_and_adjacent_screenshots() -> None:
    names = checkpoint.required_native_artifacts()

    assert "workflow3-v5-node-menu-ax.txt" in names
    assert "workflow3-v5-node-menu.png" not in names
    assert "workflow3-editor-before-fix.png" in names
    assert "workflow3-editor-after-reload.png" in names
    assert "backend-restart-v5.txt" in names
    assert "backend-restore-v4.txt" in names
    assert "health-serving-v5.json" in names
    assert "health-restored-v4.json" in names
    assert "deployments-after-restore.json" in names
    assert "workflow3-v5-runs-painted-after-shell-fix.jpeg" in names
    assert "workflow3-v5-runs-painted-after-shell-fix-ax.txt" in names
    assert "workflow3-v5-chain-verified-painted-after-shell-fix.jpeg" in names
    assert "workflow3-v5-chain-verified-painted-after-shell-fix-ax.txt" in names


def test_secret_scan_flags_exact_service_key_without_exposing_it(tmp_path: Path) -> None:
    key = "unit-test-service-secret"
    safe = tmp_path / "safe.json"
    leaked = tmp_path / "leaked.json"
    safe.write_text('{"header":"[withheld]"}', encoding="utf-8")
    leaked.write_text(f'{{"credential":"{key}"}}', encoding="utf-8")

    assert checkpoint.find_secret_leaks(tmp_path, key) == ["leaked.json"]


def test_mislabeled_png_source_is_projected_as_jpeg() -> None:
    assert checkpoint._artifact_destination("native.png") == "screenshots/native.jpeg"
    assert checkpoint._artifact_destination("native.jpeg") == "screenshots/native.jpeg"


def test_ax_projection_omits_entire_credential_header_line() -> None:
    source = (
        "curl command\n"
        "  -H 'X-API-Key: $ZEROTH_API_KEY' \\\n\n"
        "  http://127.0.0.1:8122/v1/runs/example\n"
        "Authorization: Bearer placeholder-value\n"
        "semantic evidence remains\n"
    )

    sanitized = checkpoint.sanitize_ax_text(source)

    assert sanitized.count("[credential header omitted]") == 2
    assert "X-API-Key" not in sanitized
    assert "Authorization" not in sanitized
    assert "semantic evidence remains" in sanitized


def test_acceptance_is_exact_four_pass_criteria() -> None:
    assert checkpoint.ACCEPTED_CRITERIA == (
        "ui.publish-deploy-run",
        "workflow3.publish-deploy-restart",
        "workflow3.negative-sla-expiry",
        "workflow3.health-exact-graph-version",
    )
