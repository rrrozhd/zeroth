from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path


def _module():
    return importlib.import_module("release.live_evaluation.webhook_atomicity_checkpoint")


def _passing_case(case, repository: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["uv", "run", "pytest", "-q", case.node_id],
        returncode=0,
        stdout=".                                                                        [100%]\n"
        "1 passed in 0.10s\n",
        stderr="",
    )


def test_required_matrix_names_every_d013_transition_and_fault_boundary() -> None:
    module = _module()

    assert {case.transition for case in module.REQUIRED_CASES} == {
        "subscription_created",
        "subscription_deactivated",
        "delivery_enqueued",
        "delivery_delivered",
        "delivery_failed",
        "delivery_dead_lettered",
    }
    assert {
        "audit_failure_after_insert",
        "multi_subscription_audit_failure",
        "signer_absent",
        "lost_generation_fence",
        "tenant_scope_collision",
        "metadata_sanitization",
        "signed_chain_validity",
    }.issubset({case.fault_boundary for case in module.REQUIRED_CASES})
    assert {
        "test_create_and_signed_audit_roll_back_together_after_audit_insert",
        "test_deactivate_and_signed_audit_roll_back_together_after_audit_insert",
        "test_replay_and_signed_enqueue_audit_roll_back_together_after_audit_insert",
    }.issubset({case.node_id.rsplit("::", 1)[-1] for case in module.REQUIRED_CASES})


def test_checkpoint_seals_only_exact_passing_cases_and_keeps_postgres_unproved(
    tmp_path: Path,
) -> None:
    module = _module()
    root = tmp_path / "checkpoint"

    module.build_checkpoint(
        root=root,
        repository=Path(__file__).parents[2],
        run_case=_passing_case,
    )

    acceptance = json.loads((root / "acceptance.json").read_text())
    by_id = {criterion["criterion_id"]: criterion for criterion in acceptance["criteria"]}
    assert by_id["WEBHOOKS-TRANSACTIONAL-STATE-AND-AUDIT-D013"]["status"] == "pass"
    assert by_id["WEBHOOKS-POSTGRES-ATOMICITY-D013"]["status"] == "blocked"
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["provider_calls_performed"] == 0
    assert manifest["external_network_calls"] == 0
    assert manifest["backend"] == "sqlite"
    assert manifest["postgres_proven"] is False
    module.EvidenceStore(root).scan_recursive()
    assert (root / "SHA256SUMS").is_file()


def test_skipped_or_non_exact_case_cannot_become_a_d013_pass(tmp_path: Path) -> None:
    module = _module()

    def skip_one(case, repository: Path) -> subprocess.CompletedProcess[str]:
        if case.slug == "subscription-create-rollback":
            return subprocess.CompletedProcess(
                args=["uv", "run", "pytest", "-q", case.node_id],
                returncode=0,
                stdout="1 skipped in 0.01s\n",
                stderr="",
            )
        return _passing_case(case, repository)

    root = tmp_path / "checkpoint"
    module.build_checkpoint(
        root=root,
        repository=Path(__file__).parents[2],
        run_case=skip_one,
    )

    acceptance = json.loads((root / "acceptance.json").read_text())
    criterion = next(
        item
        for item in acceptance["criteria"]
        if item["criterion_id"] == "WEBHOOKS-TRANSACTIONAL-STATE-AND-AUDIT-D013"
    )
    assert criterion["status"] == "fail"
    assert "subscription-create-rollback" in criterion["note"]
