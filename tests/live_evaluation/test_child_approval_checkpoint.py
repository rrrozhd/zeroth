from __future__ import annotations

import json
from pathlib import Path

import pytest

from release.live_evaluation.child_approval_checkpoint import (
    read_provisioning_manifest,
    read_staging_manifest,
    seal_child_approval_checkpoint,
    write_provisioning_manifest,
    write_staging_manifest,
    write_ui_result_manifest,
)
from release.live_evaluation.child_approval_live import (
    ProviderFreeChildApprovalFixture,
    StagedChildApproval,
    recover_child_approval_fixture,
)
from release.live_evaluation.evidence import EvidenceStore


def _fixture() -> ProviderFreeChildApprovalFixture:
    return ProviderFreeChildApprovalFixture(
        schema_version=1,
        fixture_id="d012-live-2",
        durable_workflow_id="durable-graph",
        durable_deployment_ref="d012-durable",
        approval_workflow_id="approval-graph",
        approval_deployment_ref="d012-approval",
        collector_workflow_id="collector-graph",
        collector_deployment_ref="d012-collector",
        parent_workflow_id="parent-graph",
        parent_graph_version_ref="parent-graph@1",
        parent_deployment_ref="d012-parent",
        parent_deployment_version=1,
        payload={"request": "d012-provider-free"},
    )


def _staged() -> StagedChildApproval:
    return StagedChildApproval(
        parent_run_id="parent-approve",
        approval_id="approval-approve",
        approval_child_run_id="child-approve",
        durable_child_run_id="durable-approve",
        container_started_at="before",
    )


def _raw_summary() -> dict[str, object]:
    return {
        "schema_version": 1,
        "provider_calls_performed": 0,
        "provider_economics_status": "blocked",
        "restart_count": 1,
        "approvals": [
            {
                "decision": decision,
                "reason": f"{decision} reason",
                "approval_id": f"approval-{decision}",
                "child_run_id": f"child-{decision}",
                "parent_run_id": f"parent-{decision}",
                "parent_status": "succeeded" if decision == "approve" else "failed",
                "durable_sibling_delivery_count_before": 1,
                "durable_sibling_delivery_count_after": 1,
                "continuation_audit_count": 1,
                "signed_audit": True,
                "priced_call_count": 0,
                "total_cost_usd": 0,
                "restored_after_refresh": True,
                "restored_after_restart": True,
            }
            for decision in ("approve", "reject")
        ],
    }


def test_existing_complete_fixture_can_be_imported_without_reprovisioning() -> None:
    names = {
        "D-012 durable child d012-live-2": "durable-graph",
        "D-012 approval child d012-live-2": "approval-graph",
        "D-012 collector child d012-live-2": "collector-graph",
        "D-012 structured child approval d012-live-2": "parent-graph",
    }

    class Response:
        status_code = 200
        text = "ok"

        def __init__(self, payload: object) -> None:
            self.payload = payload

        def json(self) -> object:
            return self.payload

    def request(method: str, path: str, payload: object = None) -> Response:
        assert method == "GET"
        assert payload is None
        if path == "/api/studio/v1/workflows":
            return Response([{"id": graph_id, "name": name} for name, graph_id in names.items()])
        if path == "/v1/deployments":
            return Response(
                [
                    {
                        "deployment_ref": f"provider-free-child-approval-d012-live-2-{kind}",
                        "version": 1,
                        "graph_version_ref": f"{graph_id}@1",
                    }
                    for kind, graph_id in (
                        ("durable", "durable-graph"),
                        ("approval", "approval-graph"),
                        ("collector", "collector-graph"),
                        ("parent", "parent-graph"),
                    )
                ]
            )
        raise AssertionError(path)

    recovered = recover_child_approval_fixture(request=request, fixture_id="d012-live-2")

    assert recovered.fixture_id == "d012-live-2"
    assert recovered.parent_workflow_id == "parent-graph"
    assert (
        recovered.parent_deployment_ref
        == "provider-free-child-approval-d012-live-2-parent"
    )
    assert recovered.parent_graph_version_ref == "parent-graph@1"
    assert recovered.provider_calls_performed == 0
    assert recovered.provider_economics_status == "blocked"


def test_provisioning_and_staging_manifests_round_trip_provider_free_identity(
    tmp_path: Path,
) -> None:
    fixture = _fixture()
    staged = _staged()
    provisioning = write_provisioning_manifest(tmp_path / "provisioning.json", fixture)
    staging = write_staging_manifest(tmp_path / "staging.json", fixture, staged)

    assert read_provisioning_manifest(provisioning) == fixture
    assert read_staging_manifest(staging) == (fixture, staged)
    assert json.loads(provisioning.read_text())["provider_economics_status"] == "blocked"

    tampered = json.loads(staging.read_text())
    tampered["provider_calls_performed"] = 1
    staging.write_text(json.dumps(tampered))
    with pytest.raises(RuntimeError, match="provider-free"):
        read_staging_manifest(staging)


def test_sealer_ingests_fixed_browser_artifacts_and_blocks_provider_economics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture()
    staged = _staged()
    provisioning = write_provisioning_manifest(tmp_path / "provisioning.json", fixture)
    staging = write_staging_manifest(tmp_path / "staging.json", fixture, staged)
    raw = _raw_summary()
    validated = {
        "parent_run_ids": ["parent-approve", "parent-reject"],
        "provider_calls_performed": 0,
        "aggregate_cost_usd": 0.0,
        "provider_economics_status": "blocked",
    }
    ui_result = write_ui_result_manifest(
        tmp_path / "ui-result.json",
        {**validated, "raw_summary": raw, "validated_summary": validated},
    )
    before = tmp_path / "before.sqlite3"
    after = tmp_path / "after.sqlite3"
    before.write_bytes(b"closed-before")
    after.write_bytes(b"closed-after")
    monkeypatch.setattr(
        "release.live_evaluation.child_approval_checkpoint.validate_child_approval_snapshots",
        lambda *args, **kwargs: {
            "partial_delivery_count_before_restart": 1,
            "durable_sibling_replay_count": 0,
            "signed_continuation_count": 2,
            "priced_call_count": 0,
            "total_cost_usd": 0.0,
            "provider_economics_status": "blocked",
        },
    )
    browser = tmp_path / "browser"
    artifacts = {
        "playwright-report/results.json": json.dumps({"status": "passed"}).encode(),
        "screenshots/01.png": b"\x89PNG\r\n\x1a\nfirst",
        "screenshots/02.png": b"\x89PNG\r\n\x1a\nsecond",
        "screenshots/03.png": b"\x89PNG\r\n\x1a\nthird",
        "screenshots/04.png": b"\x89PNG\r\n\x1a\nfourth",
        "videos/run.webm": b"\x1aE\xdf\xa3video",
        "network/sanitized-network.json": b'{"requests":[],"responses":[]}',
        "console/sanitized-console.json": b'{"messages":[]}',
    }
    indexed = []
    for relative, payload in artifacts.items():
        target = browser / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        indexed.append({"source": relative, "destination": relative})
    (browser / "results.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "completed": True,
                "fixed_title": (
                    "resolves exact child approve and reject after one coordinated restart"
                ),
                "artifacts": indexed,
            }
        )
    )

    destination = seal_child_approval_checkpoint(
        destination=tmp_path / "sealed",
        tenant_id="evaluation-studio-v1",
        provisioning_manifest=provisioning,
        staging_manifest=staging,
        ui_result_manifest=ui_result,
        before_snapshot=before,
        after_snapshot=after,
        browser_root=browser,
    )

    assert EvidenceStore(destination).is_sealed
    acceptance = json.loads((destination / "acceptance.json").read_text())
    economics = next(
        item
        for item in acceptance["criteria"]
        if item["criterion_id"] == "economics.provider-measured"
    )
    assert economics["status"] == "blocked"
    assert (destination / "runtime/raw-playwright-summary.json").is_file()
    assert (destination / "runtime/validated-playwright-summary.json").is_file()
    assert (destination / "console/sanitized-console.json").is_file()
    attestations = json.loads(
        (destination / "database-snapshots/closed-snapshot-attestations.json").read_text()
    )
    assert attestations["pre_restart"]["raw_snapshot_in_sealed_bundle"] is False
    assert attestations["post_restart"]["closed_snapshot_validated"] is True
