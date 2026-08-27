"""Seal native Safari template-governance evidence without provider calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

from release.live_evaluation.evidence import (
    AcceptanceCriterion,
    CorrelationIds,
    EvidenceStore,
)

TEMPLATE_NAME = "campaign-native-template-20260825"
REQUIRED_FILES = (
    "screenshots/00-invalid-name.jpg",
    "screenshots/01-template-created.jpg",
    "screenshots/02-refresh-persisted.jpg",
    "screenshots/03-audit-chain-valid.jpg",
    "accessibility/00-invalid-name.ax.txt",
    "accessibility/01-template-created.ax.txt",
    "accessibility/02-refresh-persisted.ax.txt",
    "accessibility/03-audit-chain-valid.ax.txt",
)


def _run_command(
    store: EvidenceStore,
    *,
    sequence: int,
    name: str,
    argv: list[str],
    cwd: Path,
) -> bool:
    completed = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
    store.record_command(
        sequence=sequence,
        name=name,
        argv=argv,
        working_directory=cwd,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
    return completed.returncode == 0


def _latest_template_audit(database: Path) -> dict[str, object]:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT audit_id, run_id, node_id, tenant_id, workspace_id, record_json "
            "FROM node_audits WHERE node_id = 'template.create' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    if row is None:
        raise RuntimeError("template.create audit record is missing")
    record = json.loads(str(row["record_json"]))
    metadata = record.get("execution_metadata", {})
    projected = {
        "audit_id": row["audit_id"],
        "run_id": row["run_id"],
        "node_id": row["node_id"],
        "tenant_id": row["tenant_id"],
        "workspace_id": row["workspace_id"],
        "record_signature": record.get("record_signature"),
        "template_name_sha256": metadata.get("template_name_sha256"),
        "template_version": metadata.get("template_version"),
        "template_transition": metadata.get("template_transition"),
    }
    serialized = json.dumps(record, sort_keys=True)
    if TEMPLATE_NAME in serialized or "Hello {{ input.name }}" in serialized:
        raise RuntimeError("template audit retained raw name or prompt content")
    if projected["record_signature"] is None:
        raise RuntimeError("template audit record is unsigned")
    expected_hash = hashlib.sha256(TEMPLATE_NAME.encode()).hexdigest()
    if projected["template_name_sha256"] != expected_hash:
        raise RuntimeError("template audit name digest does not match the UI fixture")
    if projected["template_transition"] != "created" or projected["template_version"] != 1:
        raise RuntimeError("template audit transition identity is incomplete")
    return projected


def build_checkpoint(*, root: Path, database: Path, repository: Path) -> Path:
    store = EvidenceStore(root)
    for relative in REQUIRED_FILES:
        if not (root / relative).is_file():
            raise FileNotFoundError(root / relative)

    commands = (
        (
            "frontend-template-tests",
            ["npm", "test", "--", "--run", "app/templates/page.test.tsx"],
            repository / "frontend",
        ),
        (
            "backend-template-tests",
            [
                "uv",
                "run",
                "pytest",
                "tests/service/test_template_api.py",
                "tests/service/test_template_dependencies.py",
                "tests/service/test_service_audit.py",
                "tests/service/test_template_repository.py",
                "tests/templates",
                "tests/orchestrator/test_template_memory_bindings.py",
                "tests/contracts/test_templates_surface.py",
                "-q",
            ],
            repository,
        ),
        ("frontend-typecheck", ["npx", "tsc", "--noEmit"], repository / "frontend"),
        ("diff-check", ["git", "diff", "--check"], repository),
    )
    command_results = [
        _run_command(store, sequence=index, name=name, argv=argv, cwd=cwd)
        for index, (name, argv, cwd) in enumerate(commands, start=1)
    ]

    audit = _latest_template_audit(database)
    store.record_command(
        sequence=5,
        name="template-audit-projection",
        argv=["sqlite3", "<external-state>", "template.create metadata projection"],
        working_directory=database.parent,
        exit_code=0,
        stdout=json.dumps(audit, indent=2, sort_keys=True) + "\n",
        stderr="",
    )
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    diff = subprocess.run(
        ["git", "diff", "--binary", "HEAD"],
        cwd=repository,
        capture_output=True,
        check=True,
    ).stdout
    store.write_manifest(
        {
            "campaign_id": "evaluation-studio-v1",
            "checkpoint": "native-safari-template-governance-20260825-1",
            "revision": revision,
            "diff_sha256": hashlib.sha256(diff).hexdigest(),
            "browser": "Safari",
            "service_url": "http://127.0.0.1:8122",
            "tenant_id": "evaluation-studio-v1",
            "workspace_id": None,
            "role": "admin",
            "provider_calls": 0,
            "audit_id": audit["audit_id"],
        }
    )
    event_id = store.append_event(
        "campaign.native_safari.template_governance_verified",
        {
            "template_name_sha256": audit["template_name_sha256"],
            "template_version": 1,
            "invalid_name_rejected": True,
            "refresh_persisted": True,
            "signed_audit": True,
            "audit_chain_ui_result": "chain intact; signatures valid",
            "provider_calls": 0,
        },
        correlation=CorrelationIds(audit_event_id=str(audit["audit_id"])),
    )
    shared_evidence = tuple(
        [
            *REQUIRED_FILES,
            "commands/0001-frontend-template-tests.json",
            "commands/0002-backend-template-tests.json",
            "commands/0003-frontend-typecheck.json",
            "commands/0004-diff-check.json",
            "commands/0005-template-audit-projection.json",
            f"events.ndjson#{event_id}",
        ]
    )
    deterministic_status = "pass" if all(command_results) else "fail"
    store.finalize_bundle(
        acceptance=(
            AcceptanceCriterion(
                "TEMPLATES-NATIVE-SAFARI-001", deterministic_status, shared_evidence
            ),
            AcceptanceCriterion(
                "TEMPLATES-FIELD-VALIDATION-002", deterministic_status, shared_evidence
            ),
            AcceptanceCriterion(
                "TEMPLATES-REFRESH-PERSISTENCE-003", deterministic_status, shared_evidence
            ),
            AcceptanceCriterion(
                "TEMPLATES-SIGNED-AUDIT-004", deterministic_status, shared_evidence
            ),
            AcceptanceCriterion(
                "TEMPLATES-ATOMIC-DEPENDENCY-005",
                "blocked",
                shared_evidence,
                "Dependency lookup/delete and mutation/audit are not one transaction; "
                "a concurrent publish or post-mutation audit failure remains possible.",
            ),
            AcceptanceCriterion(
                "TEMPLATES-LIVE-RENDER-006",
                "blocked",
                shared_evidence,
                "A newly rotated provider credential is still required for the priced "
                "template-rendered workflow checkpoint.",
            ),
        ),
        report_markdown=(
            "# Native Safari template governance checkpoint\n\n"
            "Safari rejected an invalid template name with a field-associated error, "
            "created one disposable tenant-scoped version through the real interface, "
            "and restored it after a browser reload. The corresponding metadata-only "
            "template.create audit record is signed, contains the tenant/version/actor "
            "correlation, stores only the SHA-256 template identity, and the Audit UI "
            "reported `chain intact · signatures valid`. No provider call was made.\n\n"
            "## Adversarial review\n\n"
            "The strongest remaining objection is atomicity: dependency inspection is "
            "a pre-delete scan rather than a locked reference index, and audit persistence "
            "is not in the template mutation transaction. The safer smaller claim is the "
            "one recorded UI creation path plus deterministic authorization/dependency "
            "tests; full production acceptance remains blocked until an outbox/reference "
            "index closes those races and a rotated credential proves live rendering.\n"
        ),
    )
    return root


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--repository", type=Path, required=True)
    args = parser.parse_args()
    root = build_checkpoint(root=args.root, database=args.database, repository=args.repository)
    print(json.dumps({"root": str(root), "sealed": EvidenceStore(root).is_sealed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
