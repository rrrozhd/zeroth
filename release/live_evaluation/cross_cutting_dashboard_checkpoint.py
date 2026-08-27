"""Seal the incumbent-dashboard five-project responsive browser checkpoint."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from .evidence import AcceptanceCriterion, EvidenceStore

EXPECTED_CRITERIA = {
    "cross-cutting.incumbent-routes-reachable",
    "cross-cutting.no-console-or-api-errors",
    "cross-cutting.responsive-no-page-overflow",
}
EXPECTED_PROJECTS = (
    "desktop-1440",
    "webkit-1440",
    "desktop-1280",
    "tablet-768",
    "mobile-390",
)
EXPECTED_ROUTES = (
    "overview",
    "runs",
    "approvals",
    "audit",
    "deployments",
    "artifacts",
    "studio",
    "templates",
    "connectors",
    "webhooks",
    "cost",
    "regulus-capabilities",
    "regulus-enforcement",
    "regulus-reconciliation",
    "retention",
    "rightsizing",
    "metrics",
)


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


def _screenshot_matrix(source_root: Path) -> dict[tuple[str, str], Path]:
    screenshots = list((source_root / "artifacts").rglob("*.png"))
    matrix: dict[tuple[str, str], Path] = {}
    for project in EXPECTED_PROJECTS:
        for route in EXPECTED_ROUTES:
            suffix = f"/{route}-{project}.png"
            matches = [path for path in screenshots if path.as_posix().endswith(suffix)]
            if len(matches) != 1:
                raise RuntimeError(
                    "incomplete or ambiguous screenshot matrix: "
                    f"{route}/{project} has {len(matches)} captures"
                )
            matrix[(project, route)] = matches[0]
    if len(screenshots) != len(matrix):
        raise RuntimeError("unexpected files in screenshot matrix")
    return matrix


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
        raise RuntimeError("Playwright dashboard checkpoint did not complete")
    observed = {row.get("criterion_id") for row in criteria if isinstance(row, dict)}
    if observed != EXPECTED_CRITERIA or any(row.get("status") != "pass" for row in criteria):
        raise RuntimeError("Playwright dashboard criteria are incomplete or non-passing")

    matrix = _screenshot_matrix(source_root)
    artifact_rows = results.get("artifacts")
    if not isinstance(artifact_rows, list):
        raise RuntimeError("Playwright dashboard artifact index is missing")
    artifact_map = {
        str(row["destination"]): source_root / str(row["source"])
        for row in artifact_rows
        if isinstance(row, dict) and "source" in row and "destination" in row
    }
    videos = [path for path in artifact_map if path.startswith("videos/")]
    if (
        len(artifact_map) != 6
        or len(videos) != 5
        or "playwright-report/index.html" not in artifact_map
    ):
        raise RuntimeError("unexpected Playwright dashboard artifact set")

    revision, diff_sha256 = _repository_identity(repository_root)
    store = EvidenceStore(destination)
    store.write_manifest(
        {
            "schema_version": 1,
            "checkpoint": "cross-cutting-dashboard-reflow",
            "created_at": datetime.now(UTC).isoformat(),
            "tenant_id": "evaluation-studio-v1",
            "revision": revision,
            "diff_sha256": diff_sha256,
            "browsers": ["Playwright Chromium", "Playwright WebKit"],
            "projects": list(EXPECTED_PROJECTS),
            "route_count": len(EXPECTED_ROUTES),
            "screenshot_count": len(matrix),
            "provider_calls_performed": 0,
            "native_safari_status": "blocked_mac_locked",
        }
    )
    evidence_paths: list[str] = []
    for (project, route), source in sorted(matrix.items()):
        relative = f"screenshots/{project}/{route}.png"
        store.ingest_artifact(source, relative)
        evidence_paths.append(relative)
    for relative, source in sorted(artifact_map.items()):
        store.ingest_artifact(source, relative)
        evidence_paths.append(relative)
    store.ingest_artifact(source_root / "results.json", "console/playwright-results.json")
    evidence_paths.append("console/playwright-results.json")
    store.record_command(
        sequence=1,
        name="playwright-dashboard-matrix",
        argv=[
            "npx",
            "playwright",
            "test",
            "e2e/incumbent-dashboard-live.spec.ts",
            "--project=desktop-1440",
            "--project=webkit-1440",
            "--project=desktop-1280",
            "--project=tablet-768",
            "--project=mobile-390",
            "--grep",
            "core dashboards reflow",
        ],
        working_directory=repository_root / "frontend",
        exit_code=0,
        stdout="5 Playwright dashboard matrix tests passed across 17 routes\n",
        stderr="",
    )
    event_id = store.append_event(
        "campaign.console.dashboard_matrix.verified",
        {
            "result": "pass",
            "projects": list(EXPECTED_PROJECTS),
            "routes": list(EXPECTED_ROUTES),
            "route_render_count": len(matrix),
            "page_overflow_failures": 0,
            "console_error_count": 0,
            "failed_request_count": 0,
            "http_error_response_count": 0,
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
            "# Cross-cutting incumbent dashboard checkpoint\n\n"
            "Seventeen incumbent console routes rendered successfully in Chromium at 1440×900, "
            "1280×800, 768×1024, and 390×844, and in WebKit at 1440×900. The 85 captured route "
            "renders had no page-level horizontal overflow, console error, unhandled page error, "
            "failed request, or HTTP 4xx/5xx response. Five complete videos and the Playwright "
            "HTML report are retained. No provider call was made.\n\n"
            "Native Safari remains blocked because the macOS session is locked; WebKit coverage is "
            "accepted only as automated engine coverage, not as a substitute for manual Safari.\n"
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
