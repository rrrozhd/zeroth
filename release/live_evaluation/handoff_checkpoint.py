"""Seal operator handoff documents against the live persistent topology."""

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

ROOT = STATE_ROOT / "evidence/handoff-checkpoint-20260825-4"
DOCUMENTS = {
    "handoff.discrepancy-register": WORKTREE
    / "release/live_evaluation/handoff/discrepancy-register.md",
    "handoff.execution-and-rollback-instructions": WORKTREE
    / "release/live_evaluation/handoff/execution-and-rollback.md",
}
REQUIRED_EVIDENCE_ROOTS = (
    "economics-pipeline-checkpoint-20260824-2",
    "chroma-corpus-checkpoint-20260824-2",
    "health-graph-stop-checkpoint-20260824-1",
    "native-safari-loop-refresh-checkpoint-20260825-1",
    "native-safari-retention-validation-checkpoint-20260825-1",
    "retention-compliance-live-checkpoint-20260825-1",
    "product-surface-inventory-checkpoint-20260825-1",
    "acceptance-gap-audit-20260825-27",
)


def _validate_document(criterion_id: str, path: Path) -> str:
    content = path.read_text(encoding="utf-8")
    lowered = content.lower()
    required = {
        "handoff.discrepancy-register": (
            "discrepancy register",
            "reconciliation rule",
            "d-001",
            "native safari",
            "production actual spend",
        ),
        "handoff.execution-and-rollback-instructions": (
            "execution preflight",
            "deployment rollback and roll-forward",
            "stop and recovery rules",
            "docker compose -f compose.dev.yml",
            "shasum -a 256 -c sha256sums",
        ),
    }[criterion_id]
    missing = [value for value in required if value not in lowered]
    if missing:
        raise RuntimeError(f"handoff document lacks required content: {missing}")
    return content


def _validate_evidence_roots(evidence_root: Path) -> dict[str, str]:
    observations: dict[str, str] = {}
    for name in REQUIRED_EVIDENCE_ROOTS:
        root = evidence_root / name
        if not EvidenceStore(root).is_sealed:
            raise RuntimeError(f"required handoff evidence is not sealed: {name}")
        EvidenceStore(root).scan_recursive()
        observations[name] = "sealed_and_secret_clean"
    return observations


def build_checkpoint(*, destination: Path = ROOT) -> Path:
    """Validate live state and copy the reviewed handoff into an append-only root."""
    if destination.exists():
        raise RuntimeError(f"checkpoint already exists: {destination}")
    documents = {
        criterion_id: _validate_document(criterion_id, path)
        for criterion_id, path in DOCUMENTS.items()
    }
    evidence_state = _validate_evidence_roots(STATE_ROOT / "evidence")
    store = EvidenceStore(destination)
    store.write_manifest(
        {
            "schema_version": 1,
            "checkpoint": "operator-handoff",
            "created_at": datetime.now(UTC).isoformat(),
            "revision": str(_git(WORKTREE, "rev-parse", "HEAD")).strip(),
            "tree_digest": _tree_digest(),
            "source_hashes": _source_hashes(tuple(DOCUMENTS.values())),
            "runtime": {
                "python": platform.python_version(),
                "platform": platform.platform(),
            },
            "required_evidence_roots": evidence_state,
        }
    )
    _run_recorded(
        store,
        sequence=1,
        name="persistent-services",
        argv=[
            "docker",
            "compose",
            "-f",
            "compose.dev.yml",
            "ps",
            "--format",
            "table {{.Service}}\\t{{.State}}\\t{{.Health}}\\t{{.Ports}}",
        ],
        cwd=WORKTREE,
    )
    _run_recorded(
        store,
        sequence=2,
        name="primary-exact-health",
        argv=["curl", "-fsS", "http://127.0.0.1:8122/health"],
        cwd=WORKTREE,
    )
    _run_recorded(
        store,
        sequence=3,
        name="twin-exact-health",
        argv=["curl", "-fsS", "http://127.0.0.1:8123/health"],
        cwd=WORKTREE,
    )
    criteria: list[AcceptanceCriterion] = []
    for criterion_id, content in documents.items():
        filename = criterion_id.removeprefix("handoff.") + ".md"
        reference = Path("handoff") / filename
        store._write_exclusive(reference, content)
        criteria.append(
            AcceptanceCriterion(
                criterion_id,
                "pass",
                (
                    reference.as_posix(),
                    "commands/0001-persistent-services.json",
                    "commands/0002-primary-exact-health.json",
                    "commands/0003-twin-exact-health.json",
                ),
            )
        )
    store._write_exclusive(
        Path("runtime/evidence-root-status.json"),
        {"roots": evidence_state},
    )
    store.finalize_bundle(
        acceptance=tuple(criteria),
        report_markdown=(
            "# Operator handoff checkpoint\n\n"
            "The discrepancy register and execution/rollback runbook are present, "
            "content-validated, linked to sealed source roots, and paired with current "
            "persistent-service and exact-health command records.\n"
        ),
    )
    print(json.dumps({"root": str(destination), "sealed": store.is_sealed}, sort_keys=True))
    return destination


def main() -> int:
    build_checkpoint()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
