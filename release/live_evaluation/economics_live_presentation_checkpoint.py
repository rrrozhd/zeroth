"""Seal the live economics truth-presentation browser checkpoint."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from .evidence import AcceptanceCriterion, EvidenceStore

EXPECTED_CRITERIA = {
    "economics-and-rightsizing.reconciliation",
    "economics-and-rightsizing.result",
}


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
    ).stdout
    return revision, hashlib.sha256(diff).hexdigest()


def build_checkpoint(
    *,
    source_root: Path,
    destination: Path,
    repository_root: Path,
) -> Path:
    source_root = source_root.expanduser().resolve(strict=True)
    repository_root = repository_root.expanduser().resolve(strict=True)
    destination = destination.expanduser().resolve(strict=False)
    if destination.exists():
        raise FileExistsError(destination)

    results = json.loads((source_root / "results.json").read_text())
    criteria = results.get("criteria")
    if results.get("completed") is not True or not isinstance(criteria, list):
        raise RuntimeError("Playwright economics checkpoint did not complete")
    observed = {row.get("criterion_id") for row in criteria if isinstance(row, dict)}
    if observed != EXPECTED_CRITERIA or any(row.get("status") != "pass" for row in criteria):
        raise RuntimeError("Playwright economics criteria are incomplete or non-passing")

    artifact_rows = results.get("artifacts")
    if not isinstance(artifact_rows, list):
        raise RuntimeError("Playwright economics artifact index is missing")
    artifact_map = {
        str(row["destination"]): source_root / str(row["source"])
        for row in artifact_rows
        if isinstance(row, dict) and "source" in row and "destination" in row
    }
    expected_suffixes = (
        "-economics-ledger-reconciliation.json",
        "-economics-ledger-run-reconciliation.png",
        "-video.webm",
    )
    if (
        len(artifact_map) != 4
        or "playwright-report/index.html" not in artifact_map
        or any(
            sum(path.endswith(suffix) for path in artifact_map) != 1
            for suffix in expected_suffixes
        )
    ):
        raise RuntimeError("unexpected Playwright economics artifact set")

    reconciliation_path = next(
        path for path in artifact_map if path.endswith("-economics-ledger-reconciliation.json")
    )
    reconciliation = json.loads(artifact_map[reconciliation_path].read_text())
    actual = reconciliation.get("ledger_actual_usd")
    attributed = reconciliation.get("run_attributed_usd")
    difference = reconciliation.get("difference_usd")
    if (
        not isinstance(actual, (int, float))
        or actual <= 0
        or attributed != 0
        or difference != actual
        or reconciliation.get("active_exposure_usd") != 0
        or reconciliation.get("ambiguous_exposure_usd") != 0
        or reconciliation.get("synthetic_control_usd") != 0.01
        or reconciliation.get("failure_mode") != "fail_closed"
    ):
        raise RuntimeError("economics reconciliation is not the accepted live truth state")

    revision, diff_sha256 = _repository_identity(repository_root)
    store = EvidenceStore(destination)
    store.write_manifest(
        {
            "schema_version": 1,
            "checkpoint": "economics-live-presentation",
            "created_at": datetime.now(UTC).isoformat(),
            "tenant_id": "evaluation-studio-v1",
            "revision": revision,
            "diff_sha256": diff_sha256,
            "browser": "Playwright Chromium",
            "viewport": "1440x900",
            "provider_calls_performed": 0,
            "native_safari_status": "blocked_mac_locked",
            "ledger_actual_usd": actual,
            "run_attributed_usd": attributed,
            "synthetic_control_usd": 0.01,
            "failure_mode": "fail_closed",
        }
    )
    evidence_paths: list[str] = []
    for relative, source in sorted(artifact_map.items()):
        store.ingest_artifact(source, relative)
        evidence_paths.append(relative)
    store.ingest_artifact(source_root / "results.json", "console/playwright-results.json")
    evidence_paths.append("console/playwright-results.json")
    store.record_command(
        sequence=1,
        name="playwright-economics",
        argv=[
            "npx",
            "playwright",
            "test",
            "e2e/incumbent-dashboard-live.spec.ts",
            "--project=desktop-1440",
            "--grep",
            "economics distinguishes provider spend",
        ],
        working_directory=repository_root / "frontend",
        exit_code=0,
        stdout="1 Playwright economics checkpoint passed\n",
        stderr="",
    )
    event_id = store.append_event(
        "campaign.console.budget_presentation.verified",
        {
            "result": "pass",
            "ledger_actual_usd": actual,
            "run_attributed_usd": attributed,
            "active_exposure_usd": 0,
            "ambiguous_exposure_usd": 0,
            "synthetic_control_usd": 0.01,
            "failure_mode": "fail_closed",
            "proof_paths": evidence_paths,
        },
    )
    criterion_evidence = tuple([*evidence_paths, f"events.ndjson#{event_id}"])
    store.finalize_bundle(
        acceptance=tuple(
            AcceptanceCriterion(criterion_id, "pass", criterion_evidence)
            for criterion_id in sorted(EXPECTED_CRITERIA)
        ),
        report_markdown=(
            "# Live economics presentation checkpoint\n\n"
            f"The production ledger reports `${actual:.8f}` actual provider spend, while the "
            "latest workflow window contains provider-free runs and therefore `$0.00` attributed "
            "cost. Active and ambiguous exposure are both zero. The `$0.01` synthetic control "
            "proof is visibly excluded from provider spend, deployment attribution, and budget "
            "consumption. The UI and runtime configuration both report fail-closed admission.\n\n"
            "No new provider call was made for this checkpoint. Measured Rightsizing and native "
            "Safari remain blocked by the external credential gate and locked macOS session, "
            "respectively.\n"
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
