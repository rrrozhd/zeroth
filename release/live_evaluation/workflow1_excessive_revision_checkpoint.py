"""Seal exact provider-independent Workflow-1 excessive-revision UI evidence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .evidence import AcceptanceCriterion, CorrelationIds, EvidenceStore
from .workflow1_excessive_revision_live import validate_excessive_revision_summary

CRITERION = "workflow1.negative-excessive-revision"


def _json(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"missing {label}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} is malformed") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be an object")
    EvidenceStore(path.parent).validate(value)
    return value


def _browser_artifacts(browser_root: Path) -> tuple[dict[str, Any], tuple[tuple[Path, Path], ...]]:
    browser_root = browser_root.resolve(strict=True)
    index = _json(browser_root / "results.json", label="Playwright evidence index")
    criteria = index.get("criteria")
    if index.get("schema_version") != 1 or index.get("completed") is not True:
        raise RuntimeError("Playwright journey did not complete")
    if (
        not isinstance(criteria, list)
        or len(criteria) != 1
        or not isinstance(criteria[0], Mapping)
        or criteria[0].get("criterion_id") != CRITERION
        or criteria[0].get("status") != "pass"
    ):
        raise RuntimeError("Playwright evidence does not pass the exact accepted criterion")
    raw = index.get("artifacts")
    if not isinstance(raw, list):
        raise RuntimeError("Playwright evidence lacks artifacts")
    counts = {
        name: 0
        for name in (
            "screenshots",
            "videos",
            "network",
            "console",
            "playwright-report",
        )
    }
    artifacts: list[tuple[Path, Path]] = []
    destinations: set[str] = set()
    for row in raw:
        if not isinstance(row, Mapping):
            raise RuntimeError("Playwright artifact row is malformed")
        source_text = row.get("source")
        destination_text = row.get("destination")
        if not isinstance(source_text, str) or not isinstance(destination_text, str):
            raise RuntimeError("Playwright artifact lacks paths")
        source_relative = Path(source_text)
        destination = Path(destination_text)
        if (
            source_relative.is_absolute()
            or destination.is_absolute()
            or ".." in source_relative.parts
            or ".." in destination.parts
            or len(destination.parts) < 2
            or destination.parts[0] not in counts
            or destination_text in destinations
        ):
            raise RuntimeError("Playwright artifact path is unsafe or duplicated")
        source = (browser_root / source_relative).resolve(strict=True)
        if source.is_symlink() or not source.is_file() or browser_root not in source.parents:
            raise RuntimeError("Playwright artifact escaped its source root")
        counts[destination.parts[0]] += 1
        destinations.add(destination_text)
        artifacts.append((source, destination))
    if (
        counts["screenshots"] < 3
        or counts["videos"] < 1
        or counts["network"] < 1
        or counts["console"] < 2
        or counts["playwright-report"] < 1
    ):
        raise RuntimeError("Playwright evidence lacks screenshots, video, console, or network")
    return index, tuple(artifacts)


def build_checkpoint(*, source_root: Path, destination: Path) -> Path:
    """Validate an unsealed UI source and create one exact append-only bundle."""
    source_root = source_root.expanduser().resolve(strict=True)
    destination = destination.expanduser().resolve(strict=False)
    if destination.exists():
        raise FileExistsError(destination)
    summary = _json(source_root / "runtime/summary.json", label="runtime summary")
    health = summary.get("health")
    if not isinstance(health, Mapping):
        raise RuntimeError("runtime summary lacks health")
    deployment = health.get("deployment_ref")
    graph = health.get("graph_version_ref")
    if not isinstance(deployment, str) or not isinstance(graph, str):
        raise RuntimeError("runtime summary lacks serving identity")
    validated = validate_excessive_revision_summary(
        summary,
        expected_deployment_ref=deployment,
        expected_graph_version_ref=graph,
    )
    browser_index, browser_artifacts = _browser_artifacts(source_root / "browser")

    store = EvidenceStore(destination)
    store.write_manifest(
        {
            "schema_version": 1,
            "checkpoint": "workflow1-provider-independent-excessive-revision",
            "created_at": datetime.now(UTC).isoformat(),
            "tenant_id": "evaluation-studio-v1",
            "deployment_ref": deployment,
            "graph_version_ref": graph,
            "run_id": validated["run_id"],
            "provider_calls_performed": 0,
            "accepted_criteria": [CRITERION],
            "d012_restored": True,
        }
    )
    paths = ["manifest.json", "runtime/summary.json", "playwright-report/evidence-index.json"]
    store._write_exclusive(Path("runtime/summary.json"), summary)
    store._write_exclusive(Path("playwright-report/evidence-index.json"), browser_index)
    for source, relative in browser_artifacts:
        store.ingest_artifact(source, relative)
        paths.append(relative.as_posix())
    event_id = store.append_event(
        "campaign.workflow1.excessive_revision.verified",
        {
            "result": "pass",
            "research_visit_count": 2,
            "failure_reason": "max_total_steps",
            "signed_audit_record_count": validated["audit_record_count"],
            "provider_call_count": 0,
            "cost_event_count": 0,
            "total_cost_usd": 0.0,
            "d012_restored": True,
        },
        correlation=CorrelationIds(run_id=str(validated["run_id"])),
    )
    evidence = tuple([*paths, f"events.ndjson#{event_id}"])
    store.finalize_bundle(
        acceptance=(AcceptanceCriterion(CRITERION, "pass", evidence),),
        report_markdown=(
            "# Workflow 1 provider-independent excessive revision checkpoint\n\n"
            "The Studio UI submitted a deterministic local research loop. Exactly two "
            "`research` visits were signed before the runtime terminated the run with "
            "`max_total_steps`. The same run and reason survived browser refresh. No "
            "provider request or cost identity exists, total cost reconciles to `$0.00`, "
            "and the pre-existing D-012 serving identity was restored exactly.\n"
        ),
    )
    return destination
