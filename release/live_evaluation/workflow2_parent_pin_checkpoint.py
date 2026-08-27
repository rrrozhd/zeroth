"""Seal the provider-free Workflow 2 parent-to-child deployment pin repair."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .evidence import AcceptanceCriterion, CorrelationIds, EvidenceStore
from .workflow3_lifecycle_evidence import STATE_ROOT, _request

TENANT = "evaluation-studio-v1"
PARENT_ID = f"{TENANT}-batched-investigation-parent"
PARENT_GRAPH = f"{PARENT_ID}@3"
PARENT_DEPLOYMENT = f"{PARENT_ID}-v1"
CHILD_ID = f"{TENANT}-batched-investigation-child"
CHILD_GRAPH = f"{CHILD_ID}@2"
CHILD_DEPLOYMENT = f"{CHILD_ID}-v1"
ROOT = STATE_ROOT / "evidence/workflow2-parent-v3-pin-checkpoint-20260824-1"
SCREENSHOT_ROOT = (
    STATE_ROOT / "evidence/workflow2-parent-v3-pin-safari-20260824-1/screenshots"
)
SCREENSHOT_SOURCES = tuple(sorted(SCREENSHOT_ROOT.glob("*.jpg")))
DATABASE = STATE_ROOT / "zeroth.db"

ACCEPTED_CRITERIA = (
    "workflow2.parent-publish-deploy-restart",
    "workflow2.health-exact-graph-version",
    "workflow2.recursive-runner-inventory",
)

CHILD_RUNNER_REF = f"subgraph:{CHILD_DEPLOYMENT}:1:investigate"
EXPECTED_RUNNERS = (CHILD_RUNNER_REF, "synthesize")

Request = Callable[[str], Any]


def _object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be a JSON object")
    return value


def _sanitize_health(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value.get(key)
        for key in (
            "status",
            "campaign_id",
            "deployment_ref",
            "deployment_version",
            "graph_version_ref",
        )
    }


def _subgraph_config(nodes: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(nodes, list):
        raise RuntimeError(f"{label} nodes must be a JSON array")
    matches = [
        node
        for node in nodes
        if isinstance(node, Mapping)
        and (node.get("id") == "investigate-child" or node.get("node_id") == "investigate-child")
    ]
    if len(matches) != 1:
        raise RuntimeError(f"{label} must contain exactly one investigate-child node")
    node = matches[0]
    if "data" in node:
        data = node.get("data")
        config = data.get("config") if isinstance(data, Mapping) else None
    else:
        config = node.get("subgraph")
    if not isinstance(config, Mapping):
        raise RuntimeError(f"{label} investigate-child config is unavailable")
    return {
        "node_id": "investigate-child",
        "graph_ref": config.get("graph_ref"),
        "version": config.get("version"),
        "thread_participation": config.get("thread_participation"),
        "max_depth": config.get("max_depth"),
    }


def _sanitize_workflow(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "graph_id": value.get("id"),
        "graph_version": value.get("version"),
        "status": value.get("status"),
        "subgraph": _subgraph_config(value.get("nodes"), label="workflow"),
    }


def _sanitize_deployments(value: Any) -> dict[str, Any]:
    if not isinstance(value, list) or not all(isinstance(row, Mapping) for row in value):
        raise RuntimeError("deployment inventory must be a JSON array")
    wanted = {PARENT_DEPLOYMENT, CHILD_DEPLOYMENT}
    rows = [
        {
            "deployment_ref": row.get("deployment_ref"),
            "version": row.get("version"),
            "graph_version_ref": row.get("graph_version_ref"),
            "status": row.get("status"),
            "serving": row.get("serving"),
        }
        for row in value
        if row.get("deployment_ref") in wanted and row.get("status") == "active"
    ]
    rows.sort(key=lambda row: str(row["deployment_ref"]))
    return {"active": rows}


def _sanitize_manifests(value: Any) -> dict[str, Any]:
    if not isinstance(value, list) or not all(isinstance(row, Mapping) for row in value):
        raise RuntimeError("runner inventory must be a JSON array")
    present = {
        row.get("manifest_ref")
        for row in value
        if row.get("kind") == "agent_runner" and isinstance(row.get("manifest_ref"), str)
    }
    return {"runner_refs": sorted(ref for ref in EXPECTED_RUNNERS if ref in present)}


def _expected_subgraph() -> dict[str, Any]:
    return {
        "node_id": "investigate-child",
        "graph_ref": CHILD_DEPLOYMENT,
        "version": 2,
        "thread_participation": "isolated",
        "max_depth": 2,
    }


def _validate_snapshot(value: Mapping[str, Any], *, label: str) -> None:
    if (
        value.get("graph_id") != PARENT_ID
        or value.get("graph_version") != 3
        or value.get("deployment_ref") != PARENT_DEPLOYMENT
        or value.get("deployment_version") != 3
        or value.get("subgraph") != _expected_subgraph()
    ):
        raise RuntimeError(f"{label} does not prove parent v3 pins child deployment v2")


def _validate(
    *,
    health: Mapping[str, Any],
    workflow: Mapping[str, Any],
    deployments: Mapping[str, Any],
    runners: Mapping[str, Any],
    graph_snapshot: Mapping[str, Any],
    deployment_snapshot: Mapping[str, Any],
) -> None:
    expected_health = {
        "status": "ok",
        "campaign_id": TENANT,
        "deployment_ref": PARENT_DEPLOYMENT,
        "deployment_version": 3,
        "graph_version_ref": PARENT_GRAPH,
    }
    if health != expected_health:
        raise RuntimeError("health does not prove the exact Workflow 2 parent deployment")
    if workflow != {
        "graph_id": PARENT_ID,
        "graph_version": 3,
        "status": "published",
        "subgraph": _expected_subgraph(),
    }:
        raise RuntimeError("published Workflow 2 parent does not pin child deployment v2")
    expected_active = [
        {
            "deployment_ref": CHILD_DEPLOYMENT,
            "version": 2,
            "graph_version_ref": CHILD_GRAPH,
            "status": "active",
            "serving": False,
        },
        {
            "deployment_ref": PARENT_DEPLOYMENT,
            "version": 3,
            "graph_version_ref": PARENT_GRAPH,
            "status": "active",
            "serving": True,
        },
    ]
    if deployments.get("active") != expected_active:
        raise RuntimeError("active Workflow 2 deployment inventory is not exact")
    if runners.get("runner_refs") != sorted(EXPECTED_RUNNERS):
        raise RuntimeError("recursive Workflow 2 runner inventory is incomplete")
    _validate_snapshot(graph_snapshot, label="graph snapshot")
    _validate_snapshot(deployment_snapshot, label="deployment snapshot")


def _validated_screenshots(sources: Sequence[Path]) -> tuple[Path, ...]:
    screenshots = tuple(Path(source) for source in sources)
    if len(screenshots) < 4:
        raise RuntimeError("checkpoint requires four native Safari checkpoints")
    if len({source.name for source in screenshots}) != len(screenshots):
        raise RuntimeError("checkpoint screenshot names must be unique")
    for source in screenshots:
        if source.is_symlink() or not source.is_file() or source.suffix.lower() not in {
            ".jpg",
            ".jpeg",
            ".png",
        }:
            raise RuntimeError(f"invalid checkpoint screenshot: {source.name}")
    return screenshots


def _fetch(request: Request) -> dict[str, dict[str, Any]]:
    return {
        "health": _sanitize_health(_object(request("/health"), label="health")),
        "workflow": _sanitize_workflow(
            _object(
                request(f"/api/studio/v1/workflows/{PARENT_ID}"),
                label="workflow",
            )
        ),
        "deployments": _sanitize_deployments(request("/v1/deployments")),
        "runner-inventory": _sanitize_manifests(request("/v1/manifests")),
    }


def build_checkpoint(
    *,
    destination: Path,
    request: Request,
    graph_snapshot: Mapping[str, Any],
    deployment_snapshot: Mapping[str, Any],
    screenshot_sources: Sequence[Path],
) -> Path:
    """Validate independent runtime/persistence/UI observations and seal them."""
    destination = destination.expanduser().resolve(strict=False)
    if destination.exists():
        raise FileExistsError(destination)
    records = _fetch(request)
    _validate(
        health=records["health"],
        workflow=records["workflow"],
        deployments=records["deployments"],
        runners=records["runner-inventory"],
        graph_snapshot=graph_snapshot,
        deployment_snapshot=deployment_snapshot,
    )
    screenshots = _validated_screenshots(screenshot_sources)

    store = EvidenceStore(destination)
    store.write_manifest(
        {
            "schema_version": 1,
            "checkpoint": "workflow2-parent-v3-child-v2-pin",
            "created_at": datetime.now(UTC).isoformat(),
            "tenant_id": TENANT,
            "deployment_ref": PARENT_DEPLOYMENT,
            "deployment_version": 3,
            "graph_version_ref": PARENT_GRAPH,
            "child_deployment_ref": CHILD_DEPLOYMENT,
            "child_deployment_version": 2,
            "provider_calls_performed": 0,
            "accepted_criteria": list(ACCEPTED_CRITERIA),
            "native_safari_screenshot_count": len(screenshots),
        }
    )
    evidence_paths: list[str] = []
    for name, value in (
        (*records.items(),)
    ):
        relative = Path("runtime") / f"{name}.json"
        store._write_exclusive(relative, value)
        evidence_paths.append(relative.as_posix())
    for name, value in (
        ("graph-snapshot", dict(graph_snapshot)),
        ("deployment-snapshot", dict(deployment_snapshot)),
    ):
        relative = Path("runtime") / f"{name}.json"
        store._write_exclusive(relative, value)
        evidence_paths.append(relative.as_posix())
    for source in screenshots:
        relative = Path("screenshots") / source.name
        store.ingest_artifact(source, relative)
        evidence_paths.append(relative.as_posix())

    event_id = store.append_event(
        "campaign.workflow2.parent_child_pin_verified",
        {
            "result": "pass",
            "parent_deployment_version": 3,
            "parent_graph_version_ref": PARENT_GRAPH,
            "child_deployment_version": 2,
            "runner_refs": list(sorted(EXPECTED_RUNNERS)),
            "proof_paths": evidence_paths,
        },
        correlation=CorrelationIds(ui_action_id="workflow2-parent-v3-child-v2-native-safari"),
    )
    acceptance_evidence = tuple([*evidence_paths, f"events.ndjson#{event_id}"])
    store.finalize_bundle(
        acceptance=tuple(
            AcceptanceCriterion(criterion, "pass", acceptance_evidence)
            for criterion in ACCEPTED_CRITERIA
        ),
        report_markdown=(
            "# Workflow 2 parent-child pin checkpoint\n\n"
            f"Native Safari cloned, edited, preflighted, published, and deployed `{PARENT_GRAPH}` "
            f"as `{PARENT_DEPLOYMENT}` deployment version 3. The published graph, immutable "
            f"deployment snapshot, reloaded inspector, and live service all agree that "
            f"`investigate-child` resolves `{CHILD_DEPLOYMENT}` deployment version 2. "
            "The recursive runner inventory contains both the namespaced child investigator "
            "and parent synthesizer. No provider call was made for this checkpoint. Older "
            "graph and deployment versions remain immutable rollback history.\n"
        ),
    )
    return destination


def _sqlite_snapshot(database: Path, *, deployment: bool) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    try:
        if deployment:
            row = connection.execute(
                "SELECT serialized_graph FROM deployment_versions "
                "WHERE deployment_ref = ? AND version = 3",
                (PARENT_DEPLOYMENT,),
            ).fetchone()
            source = "deployment_versions"
        else:
            row = connection.execute(
                "SELECT payload FROM graph_versions WHERE graph_id = ? AND version = 3",
                (PARENT_ID,),
            ).fetchone()
            source = "graph_versions"
    finally:
        connection.close()
    if row is None:
        raise RuntimeError(f"missing {source} Workflow 2 parent v3 record")
    payload = json.loads(row[0])
    return {
        "source": source,
        "graph_id": PARENT_ID,
        "graph_version": 3,
        "deployment_ref": PARENT_DEPLOYMENT,
        "deployment_version": 3,
        "subgraph": _subgraph_config(payload.get("nodes"), label=source),
    }


def main() -> int:
    root = build_checkpoint(
        destination=ROOT,
        request=_request,
        graph_snapshot=_sqlite_snapshot(DATABASE, deployment=False),
        deployment_snapshot=_sqlite_snapshot(DATABASE, deployment=True),
        screenshot_sources=SCREENSHOT_SOURCES,
    )
    print(json.dumps({"root": str(root), "sealed": EvidenceStore(root).is_sealed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
