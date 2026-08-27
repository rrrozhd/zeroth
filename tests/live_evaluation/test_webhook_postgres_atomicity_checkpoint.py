from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path


def _module():
    return importlib.import_module("release.live_evaluation.webhook_postgres_atomicity_checkpoint")


def _passing_case(case, repository: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["uv", "run", "pytest", "-q", case.node_id],
        returncode=0,
        stdout=".                                                                        [100%]\n"
        "1 passed in 0.10s\n",
        stderr="",
    )


def test_postgres_matrix_covers_create_deactivate_and_replay_rollbacks() -> None:
    module = _module()

    assert {case.transition for case in module.POSTGRES_CASES} == {
        "subscription_created",
        "subscription_deactivated",
        "delivery_replayed",
    }
    assert all(
        case.fault_boundary == "audit_failure_after_insert" for case in module.POSTGRES_CASES
    )
    assert all(case.database_assertions for case in module.POSTGRES_CASES)


def test_checkpoint_seals_only_exact_postgres_passes(tmp_path: Path) -> None:
    module = _module()
    root = tmp_path / "checkpoint"

    module.build_checkpoint(
        root=root,
        repository=Path(__file__).parents[2],
        run_case=_passing_case,
    )

    acceptance = json.loads((root / "acceptance.json").read_text())
    criterion = acceptance["criteria"][0]
    assert criterion["criterion_id"] == "WEBHOOKS-POSTGRES-ATOMICITY-D013"
    assert criterion["status"] == "pass"
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["backend"] == "postgres"
    assert manifest["postgres_proven"] is True
    assert manifest["provider_calls_performed"] == 0
    assert manifest["external_network_calls"] == 0
    module.EvidenceStore(root).scan_recursive()
    assert (root / "SHA256SUMS").is_file()


def test_skip_or_non_exact_result_cannot_pass_postgres_gate(tmp_path: Path) -> None:
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
    assert acceptance["criteria"][0]["status"] == "fail"
    assert "subscription-create-rollback" in acceptance["criteria"][0]["note"]
