"""Seal PostgreSQL D-013 webhook state/audit transaction evidence."""

from __future__ import annotations

import argparse
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from release.live_evaluation.control_plane import dirty_tree_hash
from release.live_evaluation.evidence import AcceptanceCriterion, EvidenceStore


@dataclass(frozen=True, slots=True)
class PostgresAtomicityCase:
    slug: str
    transition: str
    fault_boundary: str
    node_id: str
    database_assertions: tuple[str, ...]


POSTGRES_CASES = (
    PostgresAtomicityCase(
        slug="subscription-create-rollback",
        transition="subscription_created",
        fault_boundary="audit_failure_after_insert",
        node_id=(
            "tests/test_webhook_postgres_atomicity.py::"
            "test_postgres_create_and_signed_audit_roll_back_after_audit_insert"
        ),
        database_assertions=(
            "subscription row absent",
            "node audit row absent",
            "audit chain-head row absent",
        ),
    ),
    PostgresAtomicityCase(
        slug="subscription-deactivate-rollback",
        transition="subscription_deactivated",
        fault_boundary="audit_failure_after_insert",
        node_id=(
            "tests/test_webhook_postgres_atomicity.py::"
            "test_postgres_deactivate_and_signed_audit_roll_back_after_audit_insert"
        ),
        database_assertions=(
            "subscription remains active",
            "node audit row absent",
            "audit chain-head row absent",
        ),
    ),
    PostgresAtomicityCase(
        slug="dead-letter-replay-rollback",
        transition="delivery_replayed",
        fault_boundary="audit_failure_after_insert",
        node_id=(
            "tests/test_webhook_postgres_atomicity.py::"
            "test_postgres_replay_and_signed_audit_roll_back_after_audit_insert"
        ),
        database_assertions=(
            "only original dead-lettered delivery remains",
            "replay delivery row absent",
            "node audit row absent",
            "audit chain-head row absent",
        ),
    ),
)

CaseRunner = Callable[[PostgresAtomicityCase, Path], subprocess.CompletedProcess[str]]
_ONE_PASS = re.compile(r"(?:^|\n)1 passed(?:[ ,]|\n)")


def _run_case(
    case: PostgresAtomicityCase,
    repository: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "pytest", "-q", case.node_id],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )


def _case_passed(result: subprocess.CompletedProcess[str]) -> bool:
    output = f"{result.stdout}\n{result.stderr}".lower()
    return (
        result.returncode == 0
        and _ONE_PASS.search(output) is not None
        and " skipped" not in output
        and " failed" not in output
        and " error" not in output
    )


def build_checkpoint(
    *,
    root: Path,
    repository: Path,
    run_case: CaseRunner = _run_case,
) -> Path:
    """Run exact PostgreSQL rollback tests and seal their sanitized evidence."""
    repository = repository.resolve(strict=True)
    store = EvidenceStore(root)
    evidence: list[str] = []
    failed: list[str] = []
    case_results: list[dict[str, object]] = []

    for sequence, case in enumerate(POSTGRES_CASES, start=1):
        result = run_case(case, repository)
        passed = _case_passed(result)
        if not passed:
            failed.append(case.slug)
        store.record_command(
            sequence=sequence,
            name=case.slug,
            argv=["uv", "run", "pytest", "-q", case.node_id],
            working_directory=repository,
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
        evidence.append(f"commands/{sequence:04d}-{case.slug}.json")
        case_results.append(
            {
                "case": case.slug,
                "transition": case.transition,
                "fault_boundary": case.fault_boundary,
                "exact_test_node": case.node_id,
                "database_assertions": list(case.database_assertions),
                "result": "pass" if passed else "fail",
            }
        )

    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    status = "pass" if not failed else "fail"
    store.write_manifest(
        {
            "schema_version": 1,
            "campaign_id": "evaluation-studio-v1",
            "checkpoint": root.name,
            "revision": revision,
            "dirty_tree_hash": dirty_tree_hash(repository),
            "backend": "postgres",
            "postgres_image": "postgres:17",
            "postgres_proven": status == "pass",
            "provider_calls_performed": 0,
            "external_network_calls": 0,
            "live_service_mutated": False,
            "required_case_count": len(POSTGRES_CASES),
        }
    )
    event_id = store.append_event(
        "campaign.webhook.postgres_atomicity_matrix",
        {
            "backend": "postgres",
            "postgres_image": "postgres:17",
            "case_count": len(case_results),
            "cases": case_results,
            "result": status,
            "provider_calls": 0,
            "external_network_calls": 0,
        },
    )
    evidence.append(f"events.ndjson#{event_id}")
    note = None if not failed else f"Non-passing exact cases: {', '.join(failed)}"
    store.finalize_bundle(
        acceptance=(
            AcceptanceCriterion(
                "WEBHOOKS-POSTGRES-ATOMICITY-D013",
                status,
                tuple(evidence),
                note,
            ),
        ),
        report_markdown=(
            "# PostgreSQL webhook transactional state and audit checkpoint\n\n"
            f"The exact PostgreSQL 17 fault matrix recorded {len(case_results)} cases with "
            f"overall result `{status}`. Each test injects failure after the signed audit "
            "insert but before commit, then opens fresh transactions to assert create, "
            "deactivate, or dead-letter replay state and its audit-chain head rolled back "
            "together. The tests use a disposable local PostgreSQL container and make no "
            "provider or external-network request.\n\n"
            "## Adversarial review\n\n"
            "This proves transactional atomicity for the three operator mutations named by "
            "D-013 on PostgreSQL 17. It does not prove crash recovery after PostgreSQL itself "
            "loses durable storage or multi-region failover. The safer narrower claim is one-"
            "database ACID rollback at the repository transaction boundary.\n"
        ),
    )
    return root


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--repository", type=Path, required=True)
    args = parser.parse_args()
    build_checkpoint(root=args.root, repository=args.repository)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
