from __future__ import annotations

import importlib
import json
from copy import deepcopy
from pathlib import Path

from release.live_evaluation.evidence import EvidenceStore


def _module():
    return importlib.import_module("release.live_evaluation.workflow2_parent_pin_checkpoint")


def _responses() -> dict[str, object]:
    module = _module()
    return {
        "/health": {
            "status": "ok",
            "campaign_id": module.TENANT,
            "deployment_ref": module.PARENT_DEPLOYMENT,
            "deployment_version": 3,
            "graph_version_ref": module.PARENT_GRAPH,
        },
        f"/api/studio/v1/workflows/{module.PARENT_ID}": {
            "id": module.PARENT_ID,
            "version": 3,
            "status": "published",
            "nodes": [
                {
                    "id": "investigate-child",
                    "type": "subgraph",
                    "data": {
                        "config": {
                            "graph_ref": module.CHILD_DEPLOYMENT,
                            "version": 2,
                            "thread_participation": "isolated",
                            "max_depth": 2,
                        }
                    },
                }
            ],
        },
        "/v1/deployments": [
            {
                "deployment_ref": module.PARENT_DEPLOYMENT,
                "version": 3,
                "graph_version_ref": module.PARENT_GRAPH,
                "status": "active",
                "serving": True,
                "created_at": "2026-08-24T23:35:00Z",
            },
            {
                "deployment_ref": module.CHILD_DEPLOYMENT,
                "version": 2,
                "graph_version_ref": module.CHILD_GRAPH,
                "status": "active",
                "serving": False,
                "created_at": "2026-08-24T19:38:00Z",
            },
        ],
        "/v1/manifests": [
            {
                "manifest_ref": (
                    "subgraph:evaluation-studio-v1-batched-investigation-child-v1:"
                    "1:investigate"
                ),
                "kind": "agent_runner",
                "runtime": "python",
                "description": "child",
            },
            {
                "manifest_ref": "synthesize",
                "kind": "agent_runner",
                "runtime": "python",
                "description": "parent",
            },
        ],
    }


def _snapshot(module, *, source: str) -> dict[str, object]:
    return {
        "source": source,
        "graph_id": module.PARENT_ID,
        "graph_version": 3,
        "deployment_ref": module.PARENT_DEPLOYMENT,
        "deployment_version": 3,
        "subgraph": {
            "node_id": "investigate-child",
            "graph_ref": module.CHILD_DEPLOYMENT,
            "version": 2,
            "thread_participation": "isolated",
            "max_depth": 2,
        },
    }


def test_checkpoint_seals_exact_parent_child_pin_and_browser_evidence(tmp_path: Path) -> None:
    module = _module()
    responses = _responses()
    calls: list[str] = []

    def request(path: str):
        calls.append(path)
        return deepcopy(responses[path])

    screenshots: list[Path] = []
    for index, name in enumerate(("pin", "preflight", "published", "deployed"), start=1):
        path = tmp_path / f"{index:02d}-{name}-native-safari.jpg"
        path.write_bytes(b"\xff\xd8\xffsafe-jpeg")
        screenshots.append(path)

    destination = tmp_path / "sealed"
    result = module.build_checkpoint(
        destination=destination,
        request=request,
        graph_snapshot=_snapshot(module, source="graph_versions"),
        deployment_snapshot=_snapshot(module, source="deployment_versions"),
        screenshot_sources=screenshots,
    )

    assert result == destination
    assert EvidenceStore(destination).is_sealed
    assert calls == [
        "/health",
        f"/api/studio/v1/workflows/{module.PARENT_ID}",
        "/v1/deployments",
        "/v1/manifests",
    ]
    acceptance = json.loads((destination / "acceptance.json").read_text())
    assert [row["criterion_id"] for row in acceptance["criteria"]] == list(
        module.ACCEPTED_CRITERIA
    )
    assert all(row["status"] == "pass" for row in acceptance["criteria"])
    assert all(
        any(path.startswith("screenshots/") for path in row["evidence"])
        for row in acceptance["criteria"]
    )
    workflow = json.loads((destination / "runtime/workflow.json").read_text())
    assert workflow["subgraph"]["version"] == 2
    manifests = json.loads((destination / "runtime/runner-inventory.json").read_text())
    assert manifests["runner_refs"] == [
        "subgraph:evaluation-studio-v1-batched-investigation-child-v1:1:investigate",
        "synthesize",
    ]
    deployments = json.loads((destination / "runtime/deployments.json").read_text())
    assert all("created_at" not in row for row in deployments["active"])


def test_checkpoint_rejects_a_parent_snapshot_that_still_pins_child_v1(tmp_path: Path) -> None:
    module = _module()
    responses = _responses()
    bad = _snapshot(module, source="deployment_versions")
    bad["subgraph"] = {**bad["subgraph"], "version": 1}  # type: ignore[index]
    screenshots: list[Path] = []
    for index in range(4):
        path = tmp_path / f"{index}.jpg"
        path.write_bytes(b"\xff\xd8\xffsafe-jpeg")
        screenshots.append(path)

    import pytest

    with pytest.raises(RuntimeError, match="deployment snapshot"):
        module.build_checkpoint(
            destination=tmp_path / "bad",
            request=lambda path: deepcopy(responses[path]),
            graph_snapshot=_snapshot(module, source="graph_versions"),
            deployment_snapshot=bad,
            screenshot_sources=screenshots,
        )
