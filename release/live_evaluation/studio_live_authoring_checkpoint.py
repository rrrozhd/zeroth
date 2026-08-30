"""Seal the live Studio authoring-controls evidence checkpoint."""

from __future__ import annotations

import json
import sqlite3
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from .evidence import AcceptanceCriterion, EvidenceStore
from .runtime_paths import resolve_runtime_paths

STATE_ROOT = resolve_runtime_paths().state_root
SOURCE_ROOT = STATE_ROOT / "evidence/studio-live-authoring-controls-20260824-1"
ROOT = STATE_ROOT / "evidence/studio-live-authoring-checkpoint-20260824-1"
DATABASE = STATE_ROOT / "zeroth.db"
SERVICE_KEY_PATH = STATE_ROOT / "runtime-secrets/service-api-key"

WORKFLOW_ID = "fd2523b3-adf8-4abb-88d9-0e44d677047d"
ACCEPTED_CRITERIA = ("ui.node-menu", "ui.keyboard-shortcuts")
_WORKFLOW_PATH = f"/api/studio/v1/workflows/{WORKFLOW_ID}"
_ARTIFACT_TOP_LEVEL = {
    "accessibility",
    "console",
    "network",
    "playwright-report",
    "screenshots",
    "videos",
}

Request = Callable[[str], dict[str, Any]]


@dataclass(frozen=True)
class SourceArtifact:
    source: Path
    destination: Path


@dataclass(frozen=True)
class SourceEvidence:
    results: dict[str, Any]
    artifacts: tuple[SourceArtifact, ...]
    evidence: tuple[str, ...]
    runtime: dict[str, Any]


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid JSON source: {path.name}") from exc


def _load_json_object(path: Path) -> dict[str, Any]:
    value = _load_json(path)
    if not isinstance(value, dict):
        raise RuntimeError(f"JSON source is not an object: {path.name}")
    return value


def _safe_relative(value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"invalid source artifact {label}")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError(f"unsafe source artifact {label}")
    return relative


def _source_file(root: Path, relative: Path) -> Path:
    candidate = (root / relative).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("source artifact escapes its evidence root") from exc
    if candidate.is_symlink() or not candidate.is_file():
        raise RuntimeError(f"missing source artifact: {relative.as_posix()}")
    return candidate


def _load_source(root: Path) -> SourceEvidence:
    root = root.resolve(strict=True)
    EvidenceStore(root).scan_recursive()
    results = _load_json_object(root / "results.json")
    criteria = results.get("criteria")
    if (
        results.get("schema_version") != 1
        or results.get("completed") is not True
        or not isinstance(criteria, list)
        or len(criteria) != len(ACCEPTED_CRITERIA)
    ):
        raise RuntimeError("source results criteria do not match the checkpoint allowlist")
    dispositions = {
        row.get("criterion_id"): row.get("status")
        for row in criteria
        if isinstance(row, dict)
    }
    if dispositions != {criterion: "pass" for criterion in ACCEPTED_CRITERIA}:
        raise RuntimeError("source results criteria do not match the checkpoint allowlist")

    rows = results.get("artifacts")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("source results do not declare artifacts")
    artifacts: list[SourceArtifact] = []
    destinations: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("invalid source artifact declaration")
        source_relative = _safe_relative(row.get("source"), label="source")
        destination = _safe_relative(row.get("destination"), label="destination")
        if len(destination.parts) < 2 or destination.parts[0] not in _ARTIFACT_TOP_LEVEL:
            raise RuntimeError("invalid source artifact destination")
        destination_text = destination.as_posix()
        if destination_text in destinations:
            raise RuntimeError("duplicate source artifact destination")
        destinations.add(destination_text)
        artifacts.append(
            SourceArtifact(
                source=_source_file(root, source_relative),
                destination=destination,
            )
        )

    screenshots = [item for item in artifacts if item.destination.parts[0] == "screenshots"]
    videos = [item for item in artifacts if item.destination.parts[0] == "videos"]
    if len(screenshots) != 3 or any(
        item.destination.suffix.lower() != ".png" for item in screenshots
    ):
        raise RuntimeError("source must declare exactly three screenshots")
    if len(videos) != 1 or videos[0].destination.suffix.lower() != ".webm":
        raise RuntimeError("source must declare exactly one video")

    json_artifacts = [item for item in artifacts if item.source.suffix.lower() == ".json"]
    if not json_artifacts:
        raise RuntimeError("source is missing safe JSON evidence")
    decoded_json = [_load_json(item.source) for item in json_artifacts]
    runtime_rows = [
        value
        for value in decoded_json
        if isinstance(value, dict) and value.get("workflow_id") == WORKFLOW_ID
    ]
    if len(runtime_rows) != 1:
        raise RuntimeError("source is missing the authoring runtime JSON")
    runtime = runtime_rows[0]
    if (
        runtime.get("status") != "draft"
        or runtime.get("version") != 1
        or runtime.get("save_shortcut") != "Meta+s"
        or runtime.get("node_menu_opened") is not True
    ):
        raise RuntimeError("source authoring runtime JSON does not match the controls")

    criterion_evidence: list[tuple[str, ...]] = []
    for row in criteria:
        evidence = row.get("evidence") if isinstance(row, dict) else None
        if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
            raise RuntimeError("source criterion evidence is invalid")
        references = tuple(evidence)
        if not references or any(reference not in destinations for reference in references):
            raise RuntimeError("source criterion evidence is incomplete")
        criterion_evidence.append(references)
    if len(set(criterion_evidence)) != 1:
        raise RuntimeError("source criteria do not share the same evidence set")
    required_destinations = {
        item.destination.as_posix()
        for item in artifacts
        if item.destination.parts[0] != "playwright-report"
    }
    if set(criterion_evidence[0]) != required_destinations:
        raise RuntimeError("source criterion evidence is incomplete")
    return SourceEvidence(
        results=results,
        artifacts=tuple(artifacts),
        evidence=criterion_evidence[0],
        runtime=runtime,
    )


def _edge_rows(
    rows: object,
    *,
    id_field: str,
    source_field: str,
    target_field: str,
) -> tuple[tuple[str, str, str], ...]:
    if not isinstance(rows, list):
        raise RuntimeError("workflow edges are invalid")
    values: list[tuple[str, str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("workflow edges are invalid")
        value = (row.get(id_field), row.get(source_field), row.get(target_field))
        if not all(isinstance(item, str) and item for item in value):
            raise RuntimeError("workflow edges are invalid")
        values.append(value)  # type: ignore[arg-type]
    return tuple(sorted(values))


def _node_ids(rows: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(rows, list):
        raise RuntimeError("workflow nodes are invalid")
    values = [row.get(field) for row in rows if isinstance(row, dict)]
    if len(values) != len(rows) or not all(isinstance(value, str) and value for value in values):
        raise RuntimeError("workflow nodes are invalid")
    return tuple(sorted(values))  # type: ignore[arg-type]


def _database_workflow(path: Path) -> dict[str, Any]:
    try:
        connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT graph_id, version, status, payload, tenant_id "
                "FROM graph_versions WHERE graph_id = ? ORDER BY version",
                (WORKFLOW_ID,),
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        raise RuntimeError("failed to read the workflow persistence database") from exc
    if len(rows) != 1:
        raise RuntimeError("persistence must contain exactly draft workflow version 1")
    row = rows[0]
    try:
        payload = json.loads(row["payload"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("persistent workflow payload is invalid") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("persistent workflow payload is invalid")
    if (
        row["graph_id"] != WORKFLOW_ID
        or row["version"] != 1
        or row["status"] != "draft"
        or payload.get("graph_id") != WORKFLOW_ID
        or payload.get("version") != 1
        or payload.get("status") != "draft"
    ):
        raise RuntimeError("persistence must contain exactly draft workflow version 1")
    return {
        "id": row["graph_id"],
        "name": payload.get("name"),
        "version": row["version"],
        "status": row["status"],
        "tenant_id": row["tenant_id"],
        "node_ids": _node_ids(payload.get("nodes"), field="node_id"),
        "edges": _edge_rows(
            payload.get("edges"),
            id_field="edge_id",
            source_field="source_node_id",
            target_field="target_node_id",
        ),
    }


def _correlate_workflow(
    *,
    database: dict[str, Any],
    api: dict[str, Any],
    source: dict[str, Any],
) -> dict[str, Any]:
    api_observation = {
        "id": api.get("id"),
        "name": api.get("name"),
        "version": api.get("version"),
        "status": api.get("status"),
        "node_ids": _node_ids(api.get("nodes"), field="id"),
        "edges": _edge_rows(
            api.get("edges"),
            id_field="id",
            source_field="source",
            target_field="target",
        ),
    }
    expected = {
        "id": WORKFLOW_ID,
        "version": 1,
        "status": "draft",
    }
    if (
        any(database.get(key) != value for key, value in expected.items())
        or any(api_observation.get(key) != value for key, value in expected.items())
        or source.get("workflow_id") != WORKFLOW_ID
        or source.get("version") != 1
        or source.get("status") != "draft"
        or database.get("name") != api_observation.get("name")
        or database.get("node_ids") != api_observation.get("node_ids")
        or database.get("edges") != api_observation.get("edges")
        or len(database["node_ids"]) != 6
        or len(database["edges"]) != 6
    ):
        raise RuntimeError("DB/API graph correlation failed for draft workflow v1")
    return {
        "workflow_id": WORKFLOW_ID,
        "name": database["name"],
        "version": 1,
        "status": "draft",
        "tenant_id": database["tenant_id"],
        "node_count": len(database["node_ids"]),
        "edge_count": len(database["edges"]),
        "node_ids": list(database["node_ids"]),
        "edge_ids": [edge[0] for edge in database["edges"]],
        "database_api_match": True,
        "source_runtime_match": True,
    }


def build_checkpoint(
    *,
    destination: Path,
    source_root: Path,
    database: Path,
    request: Request,
) -> Path:
    if destination.exists():
        raise RuntimeError(f"checkpoint already exists: {destination}")
    source = _load_source(source_root)
    database_observation = _database_workflow(database)
    api_observation = request(_WORKFLOW_PATH)
    if not isinstance(api_observation, dict):
        raise RuntimeError("workflow API response is invalid")
    correlation = _correlate_workflow(
        database=database_observation,
        api=api_observation,
        source=source.runtime,
    )

    store = EvidenceStore(destination)
    store.write_manifest(
        {
            "schema_version": 1,
            "checkpoint": "studio-live-authoring-controls",
            "created_at": datetime.now(UTC).isoformat(),
            "source_root": source_root.resolve().name,
            "source_results_sha256": sha256(
                (source_root / "results.json").read_bytes()
            ).hexdigest(),
            "workflow_id": WORKFLOW_ID,
            "accepted_criteria": list(ACCEPTED_CRITERIA),
        }
    )
    correlation_path = Path("runtime/workflow-correlation.json")
    store._write_exclusive(correlation_path, correlation)
    source_results_path = Path("source/results.json")
    store._write_exclusive(source_results_path, source.results)
    for artifact in source.artifacts:
        store.ingest_artifact(artifact.source, artifact.destination)

    common_evidence = tuple(
        [correlation_path.as_posix(), source_results_path.as_posix(), *source.evidence]
    )
    acceptance = tuple(
        AcceptanceCriterion(criterion, "pass", common_evidence)
        for criterion in ACCEPTED_CRITERIA
    )
    store.finalize_bundle(
        acceptance=acceptance,
        report_markdown=(
            "# Studio live authoring checkpoint\n\n"
            "The source Playwright run passed exactly `ui.node-menu` and "
            "`ui.keyboard-shortcuts`. Its three screenshots, one video, safe JSON "
            "attachments, and report were ingested only after a recursive secret scan. "
            "The persistent graph row and authenticated Studio API response independently "
            "agree that workflow `fd2523b3-adf8-4abb-88d9-0e44d677047d` is draft version 1 "
            "with six nodes and six edges. No other criterion is accepted.\n"
        ),
    )
    return destination


def _request(path: str) -> dict[str, Any]:
    credential = SERVICE_KEY_PATH.read_text(encoding="utf-8").strip()
    request = urllib.request.Request(
        f"http://127.0.0.1:8122{path}",
        headers={"X-API-Key": credential},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise RuntimeError(f"expected object response from {path}")
    return value


def main() -> int:
    root = build_checkpoint(
        destination=ROOT,
        source_root=SOURCE_ROOT,
        database=DATABASE,
        request=_request,
    )
    print(json.dumps({"root": str(root), "sealed": EvidenceStore(root).is_sealed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
