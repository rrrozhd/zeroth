"""Seal direct evidence that secret-shaped evidence and artifacts fail closed."""

from __future__ import annotations

import json
import platform
from datetime import UTC, datetime
from pathlib import Path

from .evidence import AcceptanceCriterion, EvidenceStore
from .workflow3_lifecycle_evidence import (
    STATE_ROOT,
    WORKTREE,
    _git,
    _run_recorded,
    _source_hashes,
    _tree_digest,
)

ROOT = STATE_ROOT / "evidence/security-rejection-checkpoint-20260824-2"


def main() -> int:
    if ROOT.exists():
        raise RuntimeError(f"checkpoint already exists: {ROOT}")
    store = EvidenceStore(ROOT)
    sources = [
        WORKTREE / "release/live_evaluation/evidence.py",
        WORKTREE / "release/live_evaluation/security_rejection_probe.py",
        WORKTREE / "tests/live_evaluation/test_evidence.py",
        WORKTREE / "tests/live_evaluation/test_evidence_hardening.py",
    ]
    store.write_manifest(
        {
            "schema_version": 1,
            "checkpoint": "evidence-secret-rejection",
            "created_at": datetime.now(UTC).isoformat(),
            "revision": str(_git(WORKTREE, "rev-parse", "HEAD")).strip(),
            "tree_digest": _tree_digest(),
            "source_hashes": _source_hashes(sources),
            "runtime": {
                "python": platform.python_version(),
                "platform": platform.platform(),
            },
        }
    )
    _run_recorded(
        store,
        sequence=1,
        name="direct-secret-rejection-probe",
        argv=[
            "uv",
            "run",
            "python",
            "-m",
            "release.live_evaluation.security_rejection_probe",
        ],
        cwd=WORKTREE,
    )
    _run_recorded(
        store,
        sequence=2,
        name="evidence-security-tests",
        argv=[
            "uv",
            "run",
            "pytest",
            "-q",
            "tests/live_evaluation/test_evidence.py",
            "tests/live_evaluation/test_evidence_hardening.py",
        ],
        cwd=WORKTREE,
    )
    _run_recorded(
        store,
        sequence=3,
        name="security-probe-lint",
        argv=[
            "uv",
            "run",
            "ruff",
            "check",
            "release/live_evaluation/evidence.py",
            "release/live_evaluation/security_rejection_probe.py",
            "release/live_evaluation/security_evidence_checkpoint.py",
            "tests/live_evaluation/test_evidence.py",
            "tests/live_evaluation/test_evidence_hardening.py",
        ],
        cwd=WORKTREE,
    )
    probe = json.loads((ROOT / "commands/0001-direct-secret-rejection-probe.json").read_text())
    observation = json.loads(str(probe["stdout"]))
    store._write_exclusive(Path("runtime/rejection-observation.json"), observation)
    criteria = (
        AcceptanceCriterion(
            "evidence.secret-rejection",
            "pass",
            (
                "commands/0001-direct-secret-rejection-probe.json",
                "commands/0002-evidence-security-tests.json",
                "runtime/rejection-observation.json",
            ),
        ),
        AcceptanceCriterion(
            "stop.no-secret-artifact",
            "pass",
            (
                "commands/0001-direct-secret-rejection-probe.json",
                "runtime/rejection-observation.json",
            ),
        ),
    )
    report = """# Evidence secret-rejection checkpoint

Direct runtime probes and the evidence-hardening test suite prove that structured
credential fields, secret-bearing artifacts, and out-of-band tampering are
rejected before a bundle can be sealed. Rejected artifacts leave no destination
file, and failed recursive scans leave no checksum manifest.
"""
    store.finalize_bundle(acceptance=criteria, report_markdown=report)
    print(json.dumps({"root": str(ROOT), "sealed": store.is_sealed}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
