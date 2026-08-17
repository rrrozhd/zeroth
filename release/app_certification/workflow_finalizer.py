"""Canonical report finalization for always-run workflow cleanup."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .models import (
    MANDATORY_CHECKS,
    CertificationReport,
    CheckResult,
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


def _failed_checks(stages: dict[str, str]) -> list[CheckResult]:
    failed_stage = next((name for name in _REQUIRED_STAGES if stages[name] == "failure"), "prepare")
    checks: list[CheckResult] = []
    for name in MANDATORY_CHECKS:
        requested = _CHECK_STAGES.get(name, "certify")
        observed = failed_stage if stages[requested] == "skipped" else requested
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
    (root / "workflow-stages.json").write_text(
        json.dumps(stages, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report_path = root / "report.json"
    if _existing_report_is_valid(report_path, root, stages):
        return 0
    write_report(
        CertificationReport(
            status="failed",
            candidate=None,
            checks=_failed_checks(stages),
            evidence=None,
        ),
        report_path,
    )
    return 0
