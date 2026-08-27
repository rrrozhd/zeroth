"""Seal provider-independent D-013 webhook transaction evidence.

This checkpoint deliberately executes exact test nodes at the real shared
SQLite transaction boundary.  It does not reuse an ordinary browser success
bundle as atomicity proof and it keeps PostgreSQL explicitly unproved.
"""

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
class AtomicityCase:
    slug: str
    transition: str
    fault_boundary: str
    node_id: str


REQUIRED_CASES = (
    AtomicityCase(
        "subscription-create-rollback",
        "subscription_created",
        "audit_failure_after_insert",
        "tests/test_webhook_service.py::TestSubscriptionManagement::"
        "test_create_and_signed_audit_roll_back_together_after_audit_insert",
    ),
    AtomicityCase(
        "subscription-deactivate-rollback",
        "subscription_deactivated",
        "audit_failure_after_insert",
        "tests/test_webhook_service.py::TestSubscriptionManagement::"
        "test_deactivate_and_signed_audit_roll_back_together_after_audit_insert",
    ),
    AtomicityCase(
        "dead-letter-replay-rollback",
        "delivery_enqueued",
        "audit_failure_after_insert",
        "tests/test_webhook_service.py::TestReplayDeadLetter::"
        "test_replay_and_signed_enqueue_audit_roll_back_together_after_audit_insert",
    ),
    AtomicityCase(
        "delivery-enqueue-rollback",
        "delivery_enqueued",
        "audit_failure_after_insert",
        "tests/test_webhook_service.py::TestEmitEvent::"
        "test_enqueue_and_signed_audit_roll_back_together_on_audit_failure",
    ),
    AtomicityCase(
        "delivery-fanout-rollback",
        "delivery_enqueued",
        "multi_subscription_audit_failure",
        "tests/test_webhook_service.py::TestEmitEvent::"
        "test_fanout_is_all_or_none_when_second_audit_fails",
    ),
    AtomicityCase(
        "delivery-unsigned-fail-closed",
        "delivery_enqueued",
        "signer_absent",
        "tests/test_webhook_service.py::TestEmitEvent::"
        "test_unsigned_audit_fails_closed_before_delivery_is_visible",
    ),
    AtomicityCase(
        "delivery-chain-valid",
        "delivery_enqueued",
        "signed_chain_validity",
        "tests/test_webhook_service.py::TestEmitEvent::"
        "test_real_signed_audit_and_delivery_share_one_commit",
    ),
    AtomicityCase(
        "delivery-chain-head-rollback",
        "delivery_enqueued",
        "audit_failure_after_insert",
        "tests/test_webhook_service.py::TestEmitEvent::"
        "test_real_audit_failure_rolls_back_delivery_and_chain_head",
    ),
    AtomicityCase(
        "delivery-delivered-rollback",
        "delivery_delivered",
        "audit_failure_after_insert",
        "tests/test_webhook_delivery.py::TestDeliver::"
        "test_delivered_state_and_signed_audit_roll_back_together_on_audit_failure",
    ),
    AtomicityCase(
        "delivery-failed-rollback",
        "delivery_failed",
        "audit_failure_after_insert",
        "tests/test_webhook_delivery.py::TestDeliver::"
        "test_failed_retry_state_rolls_back_when_signed_audit_fails",
    ),
    AtomicityCase(
        "delivery-dead-letter-rollback",
        "delivery_dead_lettered",
        "audit_failure_after_insert",
        "tests/test_webhook_delivery.py::TestDeliver::"
        "test_dead_letter_and_status_roll_back_when_signed_audit_fails",
    ),
    AtomicityCase(
        "delivery-lost-fence",
        "delivery_delivered",
        "lost_generation_fence",
        "tests/test_webhook_delivery.py::TestDeliver::"
        "test_lost_delivery_fence_does_not_record_transition_audit",
    ),
    AtomicityCase(
        "subscription-tenant-collision",
        "subscription_created",
        "tenant_scope_collision",
        "tests/test_webhook_repository.py::"
        "test_webhook_subscription_collision_preserves_each_tenant_owner",
    ),
    AtomicityCase(
        "subscription-audit-sanitization",
        "subscription_created",
        "metadata_sanitization",
        "tests/test_webhook_service.py::TestSubscriptionManagement::test_create_subscription",
    ),
    AtomicityCase(
        "delivery-failure-audit-sanitization",
        "delivery_failed",
        "metadata_sanitization",
        "tests/test_webhook_delivery.py::TestDeliver::"
        "test_failed_attempt_records_typed_audit_without_error_text",
    ),
    AtomicityCase(
        "delivery-dead-letter-linkage",
        "delivery_dead_lettered",
        "metadata_sanitization",
        "tests/test_webhook_delivery.py::TestDeliver::"
        "test_dead_letter_records_only_after_successful_transition",
    ),
)

CaseRunner = Callable[[AtomicityCase, Path], subprocess.CompletedProcess[str]]
_ONE_PASS = re.compile(r"(?:^|\n)1 passed(?:[ ,]|\n)")


def _run_case(case: AtomicityCase, repository: Path) -> subprocess.CompletedProcess[str]:
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
    """Run and seal the exact SQLite D-013 matrix without live service mutation."""
    repository = repository.resolve(strict=True)
    store = EvidenceStore(root)
    failed: list[str] = []
    evidence: list[str] = []
    case_results: list[dict[str, object]] = []

    for sequence, case in enumerate(REQUIRED_CASES, start=1):
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
    store.write_manifest(
        {
            "schema_version": 1,
            "campaign_id": "evaluation-studio-v1",
            "checkpoint": root.name,
            "revision": revision,
            "dirty_tree_hash": dirty_tree_hash(repository),
            "backend": "sqlite",
            "postgres_proven": False,
            "provider_calls_performed": 0,
            "external_network_calls": 0,
            "live_service_mutated": False,
            "required_case_count": len(REQUIRED_CASES),
        }
    )
    event_id = store.append_event(
        "campaign.webhook.atomicity_matrix",
        {
            "backend": "sqlite",
            "case_count": len(case_results),
            "cases": case_results,
            "result": "pass" if not failed else "fail",
            "postgres_proven": False,
            "provider_calls": 0,
            "external_network_calls": 0,
        },
    )
    evidence.append(f"events.ndjson#{event_id}")
    status = "pass" if not failed else "fail"
    failure_note = None if not failed else f"Non-passing exact cases: {', '.join(failed)}"
    store.finalize_bundle(
        acceptance=(
            AcceptanceCriterion(
                "WEBHOOKS-TRANSACTIONAL-STATE-AND-AUDIT-D013",
                status,
                tuple(evidence),
                failure_note,
            ),
            AcceptanceCriterion(
                "WEBHOOKS-POSTGRES-ATOMICITY-D013",
                "blocked",
                (f"events.ndjson#{event_id}",),
                "This provider-independent checkpoint proves the shared SQLite boundary only; "
                "no PostgreSQL webhook transition/audit fault matrix was available.",
            ),
        ),
        report_markdown=(
            "# Webhook transactional state and audit checkpoint\n\n"
            f"The exact provider-independent SQLite fault matrix recorded {len(case_results)} "
            f"cases with overall result `{status}`. It covers create, deactivate, replay enqueue, "
            "ordinary and multi-subscription enqueue, delivered, failed, and dead-letter state; "
            "audit failure after insert; signer absence; a lost generation fence; tenant scope; "
            "signed-chain validity; and metadata-only audit linkage. No live service, provider, "
            "external destination, signing material, payload, or request header was used.\n\n"
            "## Adversarial review\n\n"
            "The strongest remaining objection is backend parity: these exact rollback proofs run "
            "against SQLite. PostgreSQL is therefore recorded as blocked, not inferred from the "
            "shared repository abstraction. The simpler safe claim is D-013 closure for the "
            "tested SQLite service topology only.\n"
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
