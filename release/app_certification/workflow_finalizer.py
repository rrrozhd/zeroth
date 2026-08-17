"""Canonical report finalization for always-run workflow cleanup."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .models import (
    MANDATORY_CHECKS,
    CertificationReport,
    CheckResult,
    file_digest,
    validate_report,
    write_report,
)

_STAGE_NAMES = (
    "APP_CHECKOUT",
    "CERTIFIER_CHECKOUT",
    "PREPARE",
    "IMAGE",
    "WHEEL",
    "SBOM",
    "EVIDENCE",
    "CONTAINERS",
    "HEALTH",
    "CERTIFY",
    "CLEANUP",
)
_REQUIRED_STAGES = tuple(name.lower() for name in _STAGE_NAMES)
_CHECK_STAGES = {
    "container-startup": "containers",
    "health": "health",
    "packaged-smoke": "certify",
    "ephemeral-smoke": "certify",
    "sbom": "sbom",
    "provenance": "evidence",
}


def _workflow_stages() -> dict[str, str]:
    return {name.lower(): os.environ.get(name, "skipped") for name in _STAGE_NAMES}


def _existing_report_is_valid(report_path: Path, root: Path, stages: dict[str, str]) -> bool:
    if not report_path.exists() or stages["certify"] == "skipped":
        return False
    try:
        report = validate_report(report_path, root=root / "root")
    except ValueError:
        return False
    if report.status == "failed":
        return True
    return all(stages[name] == "success" for name in _REQUIRED_STAGES)


def _cleanup_succeeded(path: Path) -> bool:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    resources = document.get("resources") if isinstance(document, dict) else None
    return (
        document.get("schema_version") == 1
        and document.get("status") == "passed"
        and isinstance(resources, list)
        and all(isinstance(item, dict) and item.get("absent") is True for item in resources)
    )


def _write_json(path: Path, document: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def write_workflow_evidence(
    path: Path, *, cleanup: Path, report: Path, workflow_stages: Path
) -> None:
    """Bind the finalized report to retained cleanup and workflow-stage evidence."""
    _write_json(
        path,
        {
            "cleanup_sha256": file_digest(cleanup),
            "report_sha256": file_digest(report),
            "schema_version": 1,
            "workflow_stages_sha256": file_digest(workflow_stages),
        },
    )


def validate_workflow_evidence(
    path: Path, *, cleanup: Path, report: Path, workflow_stages: Path
) -> dict[str, object]:
    """Authenticate successful cleanup and exact workflow outcomes for a report."""
    for candidate in (path, cleanup, report, workflow_stages):
        if candidate.is_symlink() or not candidate.is_file():
            raise ValueError(f"retained workflow evidence is missing: {candidate}")
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
        stages = json.loads(workflow_stages.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("retained workflow evidence is not valid JSON") from error
    expected_fields = {
        "cleanup_sha256",
        "report_sha256",
        "schema_version",
        "workflow_stages_sha256",
    }
    if (
        not isinstance(evidence, dict)
        or set(evidence) != expected_fields
        or evidence.get("schema_version") != 1
    ):
        raise ValueError("retained workflow evidence fields are invalid")
    expected_digests = {
        "cleanup_sha256": file_digest(cleanup),
        "report_sha256": file_digest(report),
        "workflow_stages_sha256": file_digest(workflow_stages),
    }
    if any(evidence.get(name) != digest for name, digest in expected_digests.items()):
        raise ValueError("retained workflow evidence digest mismatch")
    if not _cleanup_succeeded(cleanup):
        raise ValueError("retained cleanup evidence is not successful")
    if (
        not isinstance(stages, dict)
        or set(stages) != set(_REQUIRED_STAGES)
        or any(stages[name] != "success" for name in _REQUIRED_STAGES)
    ):
        raise ValueError("retained workflow stages are not all successful")
    return evidence


def _failed_checks(
    stages: dict[str, str], *, forced_stage: str | None = None
) -> list[CheckResult]:
    failed_stage = forced_stage or next(
        (name for name in _REQUIRED_STAGES if stages[name] != "success"), "prepare"
    )
    checks: list[CheckResult] = []
    for name in MANDATORY_CHECKS:
        requested = _CHECK_STAGES.get(name, "certify")
        observed = requested if stages[requested] != "success" else failed_stage
        checks.append(
            CheckResult(
                name=name,
                status="failed",
                detail=f"{name}: workflow stage {observed} outcome={stages[observed]}",
            )
        )
    return checks


def finalize_workflow(root: Path) -> int:
    stages = _workflow_stages()
    if root.is_symlink():
        raise ValueError("handoff root must not be a symlink")
    root.mkdir(parents=True, exist_ok=True)
    stages_path = root / "workflow-stages.json"
    _write_json(stages_path, stages)
    cleanup_path = root / "cleanup.json"
    cleanup_succeeded = _cleanup_succeeded(cleanup_path)
    if not cleanup_path.exists():
        _write_json(
            cleanup_path,
            {
                "errors": ["cleanup stage produced no retained evidence"],
                "resources": [],
                "schema_version": 1,
                "status": "failed",
            },
        )
    report_path = root / "report.json"
    if not (_existing_report_is_valid(report_path, root, stages) and cleanup_succeeded):
        write_report(
            CertificationReport(
                status="failed",
                candidate=None,
                checks=_failed_checks(
                    stages, forced_stage=None if cleanup_succeeded else "cleanup"
                ),
                evidence=None,
            ),
            report_path,
        )
    write_workflow_evidence(
        root / "workflow-evidence.json",
        cleanup=cleanup_path,
        report=report_path,
        workflow_stages=stages_path,
    )
    return 0
