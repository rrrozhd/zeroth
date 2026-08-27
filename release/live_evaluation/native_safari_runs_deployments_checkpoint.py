"""Seal privileged native-Safari Runs and Deployments recovery evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path
from urllib.request import urlopen

from release.live_evaluation.evidence import (
    AcceptanceCriterion,
    CorrelationIds,
    EvidenceStore,
)

RUN_ID = "8792760552e5487b855fb3350a26ee60"
DEPLOYMENT_REF = "evaluation-studio-v1-governed-remediation-v2"
GRAPH_VERSION_REF = "evaluation-studio-v1-governed-remediation@6"
DEPLOYMENT_VERSION = 5

CAPTURES = (
    "01-failed-run",
    "02-succeeded-run",
    "03-succeeded-run-chain-verified",
    "04-serving-v4-unsigned-attestation",
    "05-registered-v5-signed-attestation",
    "06-restart-restored-v5-serving",
    "07-serving-v5-signed-attestation",
)
REQUIRED_FILES = tuple(
    [*(f"screenshots/{name}.jpg" for name in CAPTURES),
     *(f"accessibility/{name}.txt" for name in CAPTURES)]
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


def _runtime_health() -> dict[str, object]:
    with urlopen("http://127.0.0.1:8122/health", timeout=5) as response:  # noqa: S310
        payload = json.loads(response.read())
    expected = {
        "status": "ok",
        "deployment_ref": DEPLOYMENT_REF,
        "deployment_version": DEPLOYMENT_VERSION,
        "graph_version_ref": GRAPH_VERSION_REF,
        "campaign_id": "evaluation-studio-v1",
    }
    if payload != expected:
        raise RuntimeError(f"unexpected serving health identity: {payload!r}")
    return payload


def _database_projection(database: Path) -> dict[str, object]:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        deployments = connection.execute(
            "SELECT deployment_ref, version, graph_version_ref, status, "
            "attestation_signature IS NOT NULL AS signed, "
            "attestation_signing_key_id, attestation_algorithm "
            "FROM deployment_versions WHERE deployment_ref = ? "
            "AND version IN (4, 5) ORDER BY version",
            (DEPLOYMENT_REF,),
        ).fetchall()
        run = connection.execute(
            "SELECT run_id, status, graph_version_ref, deployment_ref, "
            "tenant_id, workspace_id FROM runs WHERE run_id = ?",
            (RUN_ID,),
        ).fetchone()
        audit = connection.execute(
            "SELECT COUNT(*) AS records, SUM(CASE WHEN "
            "json_extract(record_json, '$.record_signature') IS NOT NULL "
            "THEN 1 ELSE 0 END) AS signed_records "
            "FROM node_audits WHERE run_id = ?",
            (RUN_ID,),
        ).fetchone()

    if len(deployments) != 2 or run is None or audit is None:
        raise RuntimeError("deployment/run projection is incomplete")
    v4, v5 = (dict(row) for row in deployments)
    run_projection = dict(run)
    audit_projection = dict(audit)
    if not (
        v4["version"] == 4
        and v4["signed"] == 0
        and v4["status"] == "superseded"
        and v5["version"] == DEPLOYMENT_VERSION
        and v5["signed"] == 1
        and v5["status"] == "active"
        and v5["graph_version_ref"] == GRAPH_VERSION_REF
        and v5["attestation_signing_key_id"] == "dev-local"
        and v5["attestation_algorithm"] == "HS256"
    ):
        raise RuntimeError("immutable signed-deployment replacement is not proven")
    if not (
        run_projection["status"] == "COMPLETED"
        and run_projection["graph_version_ref"] == GRAPH_VERSION_REF
        and run_projection["deployment_ref"] == DEPLOYMENT_REF
        and run_projection["tenant_id"] == "evaluation-studio-v1"
        and run_projection["workspace_id"] is None
        and audit_projection["records"] == 5
        and audit_projection["signed_records"] == 5
    ):
        raise RuntimeError("selected run correlation or signed evidence is incomplete")
    return {
        "deployments": [v4, v5],
        "run": run_projection,
        "audit": audit_projection,
    }


def build_checkpoint(*, root: Path, database: Path, repository: Path) -> Path:
    store = EvidenceStore(root)
    for relative in REQUIRED_FILES:
        if not (root / relative).is_file():
            raise FileNotFoundError(root / relative)

    commands = (
        (
            "deployment-signing-tests",
            [
                "uv",
                "run",
                "pytest",
                "tests/deployments/test_attestation_signing.py",
                "tests/service/test_provenance_signing_api.py",
                "tests/service/test_deployment_api.py",
                "tests/service/test_deployments_surface.py",
                "tests/service/test_evidence_api.py",
                "-q",
            ],
            repository,
        ),
        ("frontend-typecheck", ["npx", "tsc", "--noEmit"], repository / "frontend"),
        (
            "catalog-tests",
            ["uv", "run", "pytest", "tests/product_validation/test_catalog.py", "-q"],
            repository,
        ),
        ("diff-check", ["git", "diff", "--check"], repository),
    )
    command_results = [
        _run_command(store, sequence=index, name=name, argv=argv, cwd=cwd)
        for index, (name, argv, cwd) in enumerate(commands, start=1)
    ]

    health = _runtime_health()
    projection = _database_projection(database)
    store.record_command(
        sequence=5,
        name="serving-health-projection",
        argv=["GET", "http://127.0.0.1:8122/health"],
        working_directory=repository,
        exit_code=0,
        stdout=json.dumps(health, indent=2, sort_keys=True) + "\n",
        stderr="",
    )
    store.record_command(
        sequence=6,
        name="deployment-run-database-projection",
        argv=["sqlite3", "<external-state>", "signed deployment/run projection"],
        working_directory=database.parent,
        exit_code=0,
        stdout=json.dumps(projection, indent=2, sort_keys=True) + "\n",
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
            "checkpoint": "native-safari-runs-deployments-privileged-20260825-3",
            "revision": revision,
            "diff_sha256": hashlib.sha256(diff).hexdigest(),
            "browser": "Safari",
            "service_url": "http://127.0.0.1:8122",
            "tenant_id": "evaluation-studio-v1",
            "workspace_id": None,
            "role": "admin",
            "provider_calls": 0,
            "run_id": RUN_ID,
            "deployment_ref": DEPLOYMENT_REF,
            "deployment_version": DEPLOYMENT_VERSION,
            "graph_version_ref": GRAPH_VERSION_REF,
        }
    )
    event_id = store.append_event(
        "campaign.native_safari.deployment_recovery_verified",
        {
            "legacy_version": 4,
            "legacy_attestation_state": "digest_valid_unsigned",
            "replacement_version": DEPLOYMENT_VERSION,
            "replacement_attestation_state": "valid_signed",
            "replacement_key_id": "dev-local",
            "restart_serving_identity_restored": True,
            "run_chain_state": "chain_intact_signatures_valid",
            "provider_calls": 0,
        },
        correlation=CorrelationIds(run_id=RUN_ID),
    )
    shared_evidence = tuple(
        [
            *REQUIRED_FILES,
            "commands/0001-deployment-signing-tests.json",
            "commands/0002-frontend-typecheck.json",
            "commands/0003-catalog-tests.json",
            "commands/0004-diff-check.json",
            "commands/0005-serving-health-projection.json",
            "commands/0006-deployment-run-database-projection.json",
            f"events.ndjson#{event_id}",
        ]
    )
    status = "pass" if all(command_results) else "fail"
    store.finalize_bundle(
        acceptance=(
            AcceptanceCriterion("RUNS-PRIVILEGED-NATIVE-SAFARI-001", status, shared_evidence),
            AcceptanceCriterion(
                "DEPLOYMENTS-PRIVILEGED-NATIVE-SAFARI-002", status, shared_evidence
            ),
            AcceptanceCriterion(
                "DEPLOYMENTS-SIGNED-ATTESTATION-RECOVERY-003", status, shared_evidence
            ),
            AcceptanceCriterion(
                "DEPLOYMENTS-RESTART-SERVING-IDENTITY-004", status, shared_evidence
            ),
        ),
        report_markdown=(
            "# Native Safari Runs and Deployments checkpoint\n\n"
            "Native Safari inspected a persisted failed run and a successful governed-action "
            "run. The successful run exposes its exact deployment/graph/thread identity, "
            "durable local receipt, node visits, routing decisions, five signed audit records, "
            "and reconciled zero priced-call economics; its chain reports "
            "`chain intact · signatures valid`.\n\n"
            "The first serving deployment inspection caught a real historical discrepancy: "
            "immutable version 4 had a valid digest but no keyed signature. The UI then "
            "registered version 5 from the same published graph under the configured signer. "
            "Both server verification and the persisted row report `valid · signed` with the "
            "local `dev-local` key identity. Restarting the persistent backend promoted version "
            "5 to serving, and `/health` now reports version 5 with graph version 6.\n\n"
            "## Adversarial review\n\n"
            "This checkpoint does not rewrite or falsely bless the unsigned historical row; "
            "version 4 remains immutable and superseded. HMAC demonstrates local keyed "
            "integrity, not non-repudiation. The safer alternative would have been to keep "
            "version 4 serving and mark the campaign blocked, but that would leave the current "
            "demo on an unsigned attestation despite a configured signer. No provider call was "
            "made and no external action API was contacted.\n"
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
