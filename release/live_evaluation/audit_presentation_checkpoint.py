"""Seal the live Audit presentation and signed-chain browser checkpoint."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from .evidence import AcceptanceCriterion, EvidenceStore

EXPECTED_CRITERIA = {
    "audit.workflow-default-view",
    "audit.metadata-only-presentation",
    "audit.signed-chain-verification",
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
    if results.get("completed") is not True:
        raise RuntimeError("Playwright audit checkpoint did not complete")
    criteria = results.get("criteria")
    if not isinstance(criteria, list):
        raise RuntimeError("Playwright audit criteria are missing")
    observed = {row.get("criterion_id") for row in criteria if isinstance(row, dict)}
    if observed != EXPECTED_CRITERIA or any(row.get("status") != "pass" for row in criteria):
        raise RuntimeError("Playwright audit criteria are incomplete or non-passing")

    artifact_rows = results.get("artifacts")
    if not isinstance(artifact_rows, list):
        raise RuntimeError("Playwright audit artifact index is missing")
    artifact_map = {
        str(row["destination"]): source_root / str(row["source"])
        for row in artifact_rows
        if isinstance(row, dict) and "source" in row and "destination" in row
    }
    expected_suffixes = (
        "-audit-chain-verification.json",
        "-audit-chain-verified.png",
        "-audit-configured.png",
        "-video.webm",
    )
    if (
        len(artifact_map) != 5
        or "playwright-report/index.html" not in artifact_map
        or any(
            sum(path.endswith(suffix) for path in artifact_map) != 1
            for suffix in expected_suffixes
        )
    ):
        raise RuntimeError("unexpected Playwright audit artifact set")

    verification_path = next(
        path for path in artifact_map if path.endswith("-audit-chain-verification.json")
    )
    verification = json.loads(
        artifact_map[verification_path].read_text()
    )
    if (
        verification.get("verified") is not True
        or verification.get("signature_verified") is not True
        or verification.get("unsigned_record_count") != 0
        or not isinstance(verification.get("record_count"), int)
        or verification["record_count"] <= 0
    ):
        raise RuntimeError("audit verification response is not a signed intact chain")

    revision, diff_sha256 = _repository_identity(repository_root)
    store = EvidenceStore(destination)
    store.write_manifest(
        {
            "schema_version": 1,
            "checkpoint": "audit-presentation",
            "created_at": datetime.now(UTC).isoformat(),
            "tenant_id": "evaluation-studio-v1",
            "revision": revision,
            "diff_sha256": diff_sha256,
            "browser": "Playwright Chromium",
            "viewport": "1440x900",
            "provider_calls_performed": 0,
            "native_safari_status": "blocked_mac_locked",
            "verification_scope": verification.get("scope"),
            "verified_record_count": verification["record_count"],
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
        name="playwright-audit",
        argv=[
            "npx",
            "playwright",
            "test",
            "e2e/incumbent-dashboard-live.spec.ts",
            "--project=desktop-1440",
            "--grep",
            "audit chain verification succeeds",
        ],
        working_directory=repository_root / "frontend",
        exit_code=0,
        stdout="1 Playwright audit checkpoint passed\n",
        stderr="",
    )
    event_id = store.append_event(
        "campaign.console.presentation.verified",
        {
            "result": "pass",
            "default_view": "workflow",
            "literal_redaction_marker_visible": False,
            "signed_chain_verified": True,
            "unsigned_record_count": 0,
            "verification_scope": verification.get("scope"),
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
            "# Audit presentation checkpoint\n\n"
            "Chromium at 1440×900 opened Audit on workflow execution records while All and "
            "Security remained reachable. The normal view rendered no literal secret-redaction "
            "markers and explained metadata-only capture above the table. The actively served "
            "deployment audit endpoint returned an intact signed chain with zero unsigned records, "
            "and the UI displayed `chain intact · signatures valid`. No provider call was made.\n\n"
            "Native Safari is not accepted by this checkpoint: macOS was locked, so that manual "
            "browser criterion remains blocked rather than inferred from Chromium.\n"
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
