"""Seal the exact-eight Workflow-2 child-pause browser checkpoint."""

from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .control_plane import dirty_tree_hash
from .evidence import AcceptanceCriterion, CorrelationIds, EvidenceStore
from .workflow2_child_pause_live import validate_workflow2_child_pause_summary

EXPECTED_CRITERIA = {
    "audit.child-parent-signed-linkage",
    "economics.provider-free-zero-activity",
    "runs.refresh-restoration",
    "subgraphs.child-approval-no-sibling-replay",
    "subgraphs.child-pause-and-partial-collection",
    "workflow2.negative-child-pause-partial-collection",
}
EXPECTED_D012 = {
    "deployment_ref": "provider-free-child-approval-d012-20260826-2-parent",
    "graph_version_ref": "0179d403-2863-45f3-9556-58052a992da8@1",
}
EXPECTED_SCREENSHOT_COUNT = 8


def _object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid {label}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"invalid {label}")
    return value


def _relative(value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"invalid {label}")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or len(path.parts) < 2:
        raise RuntimeError(f"unsafe {label}")
    return path


def _source_file(root: Path, relative: Path) -> Path:
    unresolved = root / relative
    if unresolved.is_symlink():
        raise RuntimeError(f"source artifact cannot be a symlink: {relative.as_posix()}")
    candidate = unresolved.resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("source artifact escapes browser root") from exc
    if not candidate.is_file():
        raise RuntimeError(f"missing source artifact: {relative.as_posix()}")
    return candidate


def _load_browser_root(
    browser_root: Path,
) -> tuple[dict[str, Any], dict[str, Path], tuple[Path, ...]]:
    browser_root = browser_root.resolve(strict=True)
    EvidenceStore(browser_root).scan_recursive()
    results = _object(browser_root / "results.json", label="browser results")
    criteria = results.get("criteria")
    if results.get("schema_version") != 1 or results.get("completed") is not True:
        raise RuntimeError("browser run did not complete")
    if not isinstance(criteria, list):
        raise RuntimeError("browser results have no criteria")
    dispositions = {
        row.get("criterion_id"): row.get("status")
        for row in criteria
        if isinstance(row, Mapping)
    }
    if (
        len(criteria) != len(EXPECTED_CRITERIA)
        or dispositions != {criterion: "pass" for criterion in EXPECTED_CRITERIA}
    ):
        raise RuntimeError("browser criteria do not match the exact allowlist")

    rows = results.get("artifacts")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("browser results have no indexed artifacts")
    artifacts: dict[str, Path] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise RuntimeError("invalid indexed artifact")
        source = _relative(row.get("source"), label="artifact source")
        destination = _relative(row.get("destination"), label="artifact destination")
        destination_text = destination.as_posix()
        if destination_text in artifacts:
            raise RuntimeError("duplicate indexed artifact destination")
        artifacts[destination_text] = _source_file(browser_root, source)

    screenshots = [path for path in artifacts if path.startswith("screenshots/")]
    videos = [path for path in artifacts if path.startswith("videos/")]
    if len(screenshots) != EXPECTED_SCREENSHOT_COUNT or any(
        Path(path).suffix.lower() != ".png" for path in screenshots
    ):
        raise RuntimeError("browser checkpoint requires exactly eight PNG screenshots")
    if len(videos) != 1 or Path(videos[0]).suffix.lower() != ".webm":
        raise RuntimeError("browser checkpoint requires exactly one WebM video")

    summary_paths = [
        source
        for destination, source in artifacts.items()
        if destination.startswith("console/")
        and destination.endswith("workflow2-child-pause-summary.json")
    ]
    if len(summary_paths) != 1:
        raise RuntimeError("browser checkpoint requires one raw child-pause summary")
    summary = _object(summary_paths[0], label="child-pause summary")
    validate_workflow2_child_pause_summary(summary)

    declared = set(artifacts) - {"playwright-report/index.html"}
    for row in criteria:
        references = row.get("evidence") if isinstance(row, Mapping) else None
        if not isinstance(references, Sequence) or isinstance(references, str):
            raise RuntimeError("criterion evidence is invalid")
        if set(references) != declared:
            raise RuntimeError("criterion evidence is incomplete or over-broad")

    report_root = browser_root / "html-report"
    html_files = tuple(path for path in sorted(report_root.rglob("*")) if path.is_file())
    if (
        not html_files
        or not (report_root / "index.html").is_file()
        or not any(path.parent != report_root for path in html_files)
    ):
        raise RuntimeError("full Playwright HTML report is missing")
    return summary, artifacts, html_files


def _identity_values(rows: Sequence[object], field: str) -> set[str]:
    values: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise RuntimeError("response identity row is invalid")
        identity = row.get("identity")
        if not isinstance(identity, Mapping):
            raise RuntimeError("response identity row has no identity object")
        raw = identity.get(field, ())
        if not isinstance(raw, Sequence) or isinstance(raw, str):
            raise RuntimeError(f"response identity field is invalid: {field}")
        if any(not isinstance(value, str) or not value for value in raw):
            raise RuntimeError(f"response identity value is invalid: {field}")
        values.update(raw)
    return values


def _validate_execution_identities(
    *,
    summary: Mapping[str, Any],
    artifacts: Mapping[str, Path],
) -> list[dict[str, object]]:
    identity_paths = [
        source
        for destination, source in artifacts.items()
        if destination.startswith("console/")
        and destination.endswith("response-identities.json")
    ]
    if len(identity_paths) != 1:
        raise RuntimeError("browser checkpoint requires one response identity artifact")
    try:
        rows = json.loads(identity_paths[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("response identities are invalid") from exc
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("response identities are invalid")

    run_ids = _identity_values(rows, "run_id")
    thread_ids = _identity_values(rows, "thread_id")
    audit_ids = _identity_values(rows, "audit_id")
    deployment_refs = _identity_values(rows, "deployment_ref")
    graph_refs = _identity_values(rows, "graph_version_ref")
    cost_event_ids = _identity_values(rows, "cost_event_id")
    provider_request_ids = _identity_values(rows, "provider_request_id")
    if cost_event_ids or provider_request_ids:
        raise RuntimeError("provider-free checkpoint contains cost/provider identities")

    health = summary.get("health")
    outcomes = summary.get("outcomes")
    if not isinstance(health, Mapping) or not isinstance(outcomes, list):
        raise RuntimeError("child-pause health or outcomes are invalid")
    if (
        health.get("status") != "ok"
        or health.get("deployment_ref") not in deployment_refs
        or health.get("graph_version_ref") not in graph_refs
    ):
        raise RuntimeError("served parent identity is absent from captured responses")

    result: list[dict[str, object]] = []
    for outcome in outcomes:
        if not isinstance(outcome, Mapping):
            raise RuntimeError("child-pause outcome identity is invalid")
        parent = str(outcome["parent_run_id"])
        approval = str(outcome["approval_id"])
        children = outcome.get("children_after")
        if not isinstance(children, list) or any(
            not isinstance(child, Mapping) for child in children
        ):
            raise RuntimeError("child-pause child identities are invalid")
        ordered = sorted(children, key=lambda child: int(child["branch_index"]))
        child_run_ids = [str(child["run_id"]) for child in ordered]
        child_thread_ids = [str(child["thread_id"]) for child in ordered]
        expected_audits = [f"{parent}:branch:{index}:audit:1" for index in range(8)]
        expected_audits.append(f"{parent}:child-approval-continuation:{approval}")
        if (
            parent not in run_ids
            or not set(child_run_ids).issubset(run_ids)
            or not set(child_thread_ids).issubset(thread_ids)
        ):
            raise RuntimeError("parent or child run identities are absent from responses")
        if not set(expected_audits).issubset(audit_ids):
            raise RuntimeError("exact signed audit identities are absent from responses")
        result.append(
            {
                "decision": str(outcome["decision"]),
                "parent_run_id": parent,
                "approval_id": approval,
                "approval_child_run_id": str(outcome["approval_child_run_id"]),
                "child_run_ids": child_run_ids,
                "child_thread_ids": child_thread_ids,
                "audit_ids": expected_audits,
                "cost_event_ids": [],
                "provider_request_ids": [],
                "priced_call_count": 0,
                "total_cost_usd": 0.0,
            }
        )
    return result


def _d012_health(source_root: Path) -> dict[str, str]:
    command = source_root / "commands" / "live-run.txt"
    if command.is_symlink() or not command.is_file():
        raise RuntimeError("D-012 restoration record is missing")
    records: list[dict[str, Any]] = []
    lines = command.read_text(encoding="utf-8").splitlines()
    for line in lines:
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    expected = {"status": "ok", **EXPECTED_D012}
    if not lines or lines[-1] != "D012_RESTORED" or records[-1:] != [expected]:
        raise RuntimeError("D-012 was not restored to the exact health identity")
    return expected


def _repository_identity(repository_root: Path) -> tuple[str, str]:
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return revision, dirty_tree_hash(repository_root).removeprefix("sha256:")


def build_checkpoint(
    *,
    source_root: Path,
    destination: Path,
    repository_root: Path,
) -> Path:
    source_root = source_root.expanduser().resolve(strict=True)
    browser_root = (source_root / "browser").resolve(strict=True)
    destination = destination.expanduser().resolve(strict=False)
    repository_root = repository_root.expanduser().resolve(strict=True)
    if destination.exists():
        raise FileExistsError(destination)
    summary, artifacts, html_files = _load_browser_root(browser_root)
    restored_health = _d012_health(source_root)
    validation = validate_workflow2_child_pause_summary(summary)
    execution_identities = _validate_execution_identities(
        summary=summary,
        artifacts=artifacts,
    )
    revision, diff_sha256 = _repository_identity(repository_root)

    outcomes = summary["outcomes"]
    assert isinstance(outcomes, list)
    store = EvidenceStore(destination)
    store.write_manifest(
        {
            "schema_version": 1,
            "checkpoint": "workflow2-child-pause-accepted-20260826-1",
            "created_at": datetime.now(UTC).isoformat(),
            "tenant_id": "evaluation-studio-v1",
            "revision": revision,
            "diff_sha256": diff_sha256,
            "source_root": str(source_root),
            "provider_calls_performed": 0,
            "provider_economics_status": "blocked",
            "served_identity": summary["health"],
            "execution_identities": execution_identities,
            "d012_restored_health": restored_health,
            "database_snapshot_status": "not_run",
            "database_snapshots_in_bundle": False,
        }
    )

    evidence_paths: list[str] = []
    for relative, source in sorted(artifacts.items()):
        if relative == "playwright-report/index.html":
            continue
        store.ingest_artifact(source, relative)
        evidence_paths.append(relative)
    for source in html_files:
        relative = Path("playwright-report") / source.relative_to(browser_root / "html-report")
        store.ingest_artifact(source, relative)
        evidence_paths.append(relative.as_posix())
    store.ingest_artifact(browser_root / "results.json", "playwright-report/results.json")
    evidence_paths.append("playwright-report/results.json")
    store._write_exclusive(
        Path("reconciliation/workflow2-child-pause-validation.json"),
        {
            "schema_version": 1,
            **validation,
            "served_identity": summary["health"],
            "execution_identities": execution_identities,
            "d012_restored_health": restored_health,
        },
    )
    evidence_paths.append("reconciliation/workflow2-child-pause-validation.json")

    event_refs: list[str] = []
    for outcome in outcomes:
        assert isinstance(outcome, Mapping)
        event_id = store.append_event(
            "campaign.run.workflow2_child_pause_verified",
            {
                "result": "pass",
                "decision": str(outcome["decision"]),
                "approval_id": str(outcome["approval_id"]),
                "approval_child_run_id": str(outcome["approval_child_run_id"]),
                "parent_status": str(outcome["parent_status"]),
                "signed_parent_chain": True,
                "signed_child_chain_count": 8,
                "sibling_replay_count": 0,
                "priced_call_count": 0,
                "total_cost_usd": 0.0,
            },
            correlation=CorrelationIds(run_id=str(outcome["parent_run_id"])),
        )
        event_refs.append(f"events.ndjson#{event_id}")
    criterion_evidence = tuple([*evidence_paths, *event_refs])
    acceptance = [
        *(
            AcceptanceCriterion(criterion, "pass", criterion_evidence)
            for criterion in sorted(EXPECTED_CRITERIA)
        ),
        AcceptanceCriterion(
            "evidence.database-snapshots",
            "not_run",
            (),
            "No coherent pre-run database snapshot was captured for this slice.",
        ),
    ]
    store.finalize_bundle(
        acceptance=acceptance,
        report_markdown=(
            "# Workflow 2 exact-eight child-pause checkpoint\n\n"
            "The real Studio UI submitted approve and reject cases with concurrency four. "
            "Branch seven alone paused while seven siblings completed; refresh preserved all "
            "parent/child identities, reviewer resolution replayed no sibling, all signed chains "
            "verified, and provider activity and attributed cost were exactly zero. The reject "
            "case terminated the parent as `parallel_execution_failed`.\n\n"
            "Database snapshots are **not run** for this slice: no coherent pre-run snapshot was "
            "captured, and this bundle does not imply otherwise. D-012 serving health was restored "
            "to the exact deployment and graph recorded in the manifest.\n"
        ),
    )
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    args = parser.parse_args()
    root = build_checkpoint(
        source_root=args.source_root,
        destination=args.destination,
        repository_root=args.repository_root,
    )
    print(json.dumps({"root": str(root), "sealed": EvidenceStore(root).is_sealed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
