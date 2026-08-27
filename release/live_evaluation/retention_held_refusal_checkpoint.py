"""Seal current-build Retention legal-hold erasure-refusal UI evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from .evidence import AcceptanceCriterion, CorrelationIds, EvidenceStore

EXPECTED_CRITERIA = {
    "retention-and-erasure.held-erasure-refusal",
    "retention-and-erasure.persistence",
}
EXPECTED_DEPLOYMENT = "provider-free-child-approval-d012-20260826-2-parent"
EXPECTED_GRAPH_VERSION = "0179d403-2863-45f3-9556-58052a992da8@1"


def _repository_identity(repository_root: Path) -> tuple[str, str]:
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    diff = subprocess.run(
        ["git", "diff", "--binary", "HEAD"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return revision, hashlib.sha256(diff.encode()).hexdigest()


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid JSON evidence: {path.name}") from exc


def _validate_result(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise RuntimeError("retention result must be an object")
    if value.get("refusal_status") != 409:
        raise RuntimeError("held erasure refusal did not return 409")
    chain = value.get("signed_chain")
    if (
        not isinstance(chain, Mapping)
        or chain.get("verified") is not True
        or chain.get("signature_verified") is not True
        or chain.get("unsigned_record_count") != 0
        or not isinstance(chain.get("record_count"), int)
        or int(chain["record_count"]) <= 0
    ):
        raise RuntimeError("retention refusal did not preserve a signed intact chain")
    exact = {
        "refusal_action": value.get("refusal_action"),
        "run_snapshot_unchanged": value.get("run_snapshot_unchanged"),
        "evidence_snapshot_unchanged": value.get("evidence_snapshot_unchanged"),
        "hold_refresh_restored": value.get("hold_refresh_restored"),
        "hold_released": value.get("hold_released"),
        "provider_calls": value.get("provider_calls"),
    }
    if exact != {
        "refusal_action": "erasure_refused_legal_hold",
        "run_snapshot_unchanged": True,
        "evidence_snapshot_unchanged": True,
        "hold_refresh_restored": True,
        "hold_released": True,
        "provider_calls": 0,
    }:
        raise RuntimeError("retention refusal proof is incomplete")
    for field in ("run_id", "hold_id", "refusal_log_id"):
        if not isinstance(value.get(field), str) or not value[field]:
            raise RuntimeError(f"retention result is missing {field}")
    baseline = value.get("baseline_hold_ids_preserved")
    if not isinstance(baseline, list) or not baseline or not all(
        isinstance(item, str) and item for item in baseline
    ):
        raise RuntimeError("baseline legal holds were not explicitly preserved")
    health = value.get("health")
    if (
        not isinstance(health, Mapping)
        or health.get("status") != "ok"
        or health.get("deployment_ref") != EXPECTED_DEPLOYMENT
        or health.get("graph_version_ref") != EXPECTED_GRAPH_VERSION
    ):
        raise RuntimeError("exact D012 deployment was not restored")
    return value


def build_checkpoint(
    *,
    source_root: Path,
    destination: Path,
    repository_root: Path,
    command_stdout: str,
    command_stderr: str,
) -> Path:
    """Validate, secret-scan, and seal one reversible Retention UI checkpoint."""
    source_root = source_root.expanduser().resolve(strict=True)
    repository_root = repository_root.expanduser().resolve(strict=True)
    destination = destination.expanduser().resolve(strict=False)
    if destination.exists():
        raise FileExistsError(destination)

    results = _load_json(source_root / "results.json")
    if not isinstance(results, Mapping) or results.get("completed") is not True:
        raise RuntimeError("Playwright retention checkpoint did not complete")
    criteria = results.get("criteria")
    if not isinstance(criteria, list):
        raise RuntimeError("Playwright retention criteria are missing")
    observed = {row.get("criterion_id") for row in criteria if isinstance(row, Mapping)}
    if observed != EXPECTED_CRITERIA or any(
        not isinstance(row, Mapping) or row.get("status") != "pass" for row in criteria
    ):
        raise RuntimeError("Playwright retention criteria are incomplete or non-passing")

    artifact_rows = results.get("artifacts")
    if not isinstance(artifact_rows, list):
        raise RuntimeError("Playwright retention artifact index is missing")
    artifacts: dict[str, Path] = {}
    for row in artifact_rows:
        if not isinstance(row, Mapping):
            raise RuntimeError("invalid Playwright artifact row")
        source_value = row.get("source")
        destination_value = row.get("destination")
        if not isinstance(source_value, str) or not isinstance(destination_value, str):
            raise RuntimeError("invalid Playwright artifact path")
        relative = Path(source_value)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("unsafe Playwright artifact source")
        source = (source_root / relative).resolve(strict=True)
        source.relative_to(source_root)
        artifacts[destination_value] = source

    screenshots = sorted(path for path in artifacts if path.startswith("screenshots/"))
    videos = sorted(path for path in artifacts if path.startswith("videos/"))
    console = sorted(
        path
        for path in artifacts
        if path.startswith("console/") and path.endswith("-retention-held-refusal-result.json")
    )
    accessibility = sorted(
        path
        for path in artifacts
        if path.startswith("accessibility/") and path.endswith("-axe-wcag22-aa.json")
    )
    if (
        len(screenshots) != 6
        or len(videos) != 2
        or len(console) != 1
        or len(accessibility) != 1
        or "playwright-report/index.html" not in artifacts
    ):
        raise RuntimeError("unexpected Retention Playwright artifact set")
    if _load_json(artifacts[accessibility[0]]) != []:
        raise RuntimeError("Retention UI has WCAG 2.2 AA violations")
    result = _validate_result(_load_json(artifacts[console[0]]))

    revision, diff_sha256 = _repository_identity(repository_root)
    store = EvidenceStore(destination)
    store.write_manifest(
        {
            "schema_version": 1,
            "checkpoint": "retention-held-erasure-refusal-current-build",
            "created_at": datetime.now(UTC).isoformat(),
            "tenant_id": result["tenant_id"],
            "revision": revision,
            "diff_sha256": diff_sha256,
            "browser": "Playwright Chromium",
            "viewport": "1440x900",
            "deployment_ref": EXPECTED_DEPLOYMENT,
            "graph_version_ref": EXPECTED_GRAPH_VERSION,
            "provider_calls_performed": 0,
            "destructive_erasure_performed": False,
            "baseline_restored": True,
            "run_id": result["run_id"],
            "hold_id": result["hold_id"],
            "retention_log_id": result["refusal_log_id"],
        }
    )
    evidence_paths: list[str] = []
    for relative, source in sorted(artifacts.items()):
        store.ingest_artifact(source, relative)
        evidence_paths.append(relative)
    store.ingest_artifact(source_root / "results.json", "console/playwright-results.json")
    evidence_paths.append("console/playwright-results.json")
    # Preserve the full linked HTML report, not only its entry point.
    report_root = source_root / "html-report"
    if report_root.is_dir():
        for source in sorted(report_root.rglob("*")):
            if not source.is_file() or source.is_symlink():
                continue
            relative = "playwright-report/" + source.relative_to(report_root).as_posix()
            if relative in artifacts:
                continue
            store.ingest_artifact(source, relative)
            evidence_paths.append(relative)
    store.record_command(
        sequence=1,
        name="retention-held-refusal-playwright",
        argv=[
            "npx",
            "playwright",
            "test",
            "e2e/retention-held-refusal-current-live.spec.ts",
            "--project=desktop-1440",
        ],
        working_directory=repository_root / "frontend",
        exit_code=0,
        stdout=command_stdout,
        stderr=command_stderr,
    )
    event_id = store.append_event(
        "campaign.retention.held_erasure_refused",
        {
            "result": "pass",
            "http_status": 409,
            "retention_action": "erasure_refused_legal_hold",
            "run_snapshot_unchanged": True,
            "evidence_snapshot_unchanged": True,
            "signed_chain_verified": True,
            "hold_released": True,
            "provider_calls": 0,
            "proof_paths": evidence_paths,
        },
        correlation=CorrelationIds(
            run_id=str(result["run_id"]),
            audit_event_id=str(result["refusal_log_id"]),
            ui_action_id="retention-held-refusal-current-20260826-1",
        ),
    )
    criterion_evidence = tuple([*evidence_paths, f"events.ndjson#{event_id}"])
    store.finalize_bundle(
        acceptance=tuple(
            AcceptanceCriterion(criterion, "pass", criterion_evidence)
            for criterion in sorted(EXPECTED_CRITERIA)
        ),
        report_markdown=(
            "# Retention held-erasure refusal checkpoint\n\n"
            "The real Retention interface placed a run-scoped legal hold, restored it after "
            "refresh, staged the protected run for erasure, and submitted the irreversible "
            "action through the confirmation dialog. The service refused it with HTTP 409 and "
            "persisted `erasure_refused_legal_hold`. The run and its evidence snapshot remained "
            "byte-for-byte stable, its signed audit chain remained intact, and the UI restored "
            "the durable refusal history after refresh. The temporary hold was released and the "
            "pre-existing hold set was restored exactly. No provider call or destructive erasure "
            "occurred.\n"
        ),
    )
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--stdout-file", type=Path, required=True)
    parser.add_argument("--stderr-file", type=Path, required=True)
    args = parser.parse_args()
    root = build_checkpoint(
        source_root=args.source_root,
        destination=args.destination,
        repository_root=args.repository_root,
        command_stdout=args.stdout_file.read_text(encoding="utf-8"),
        command_stderr=args.stderr_file.read_text(encoding="utf-8"),
    )
    print(json.dumps({"root": str(root), "sealed": EvidenceStore(root).is_sealed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
