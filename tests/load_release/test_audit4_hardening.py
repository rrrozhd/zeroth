"""Regressions for ZER-33 AUDIT-4 fail-closed evidence."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
from inspect import signature
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _extract_revision(revision: str, target: Path) -> None:
    archive = subprocess.run(
        ["git", "archive", "--format=tar", revision],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(target, filter="data")


def _archive_source_digest(revision: str) -> str:
    archive = subprocess.run(
        ["git", "archive", "--format=tar", revision],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    digest = hashlib.sha256()
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        for member in sorted(
            (member for member in source.getmembers() if member.isfile()),
            key=lambda member: member.name,
        ):
            content = source.extractfile(member)
            assert content is not None
            digest.update(member.name.encode())
            digest.update(b"\0")
            digest.update(hashlib.sha256(content.read()).digest())
    return "sha256:" + digest.hexdigest()


def test_baseline_source_digest_matches_the_exact_base_archive(tmp_path: Path) -> None:
    from release.load.receipt import load_source_identity, source_digest

    baseline = json.loads((ROOT / "release/load/baseline-v1.json").read_text())
    source = baseline["source"]
    identity, identity_digest = load_source_identity(ROOT / "release/load/baseline-source-v1.json")
    _extract_revision(source["commit"], tmp_path)

    assert _archive_source_digest(source["commit"]) == source["source_digest"]
    assert source_digest(tmp_path) == source["source_digest"]
    assert source["source_identity_digest"] == identity_digest
    assert all(
        source[name] == identity[name]
        for name in ("commit", "tree", "package_version", "source_digest")
    )


def test_receipt_binds_measured_source_to_retained_git_identity(tmp_path: Path) -> None:
    from release.load.receipt import build_receipt, source_identity

    identity_path = ROOT / "release/load/baseline-source-v1.json"
    expected = json.loads(identity_path.read_text())
    _extract_revision(expected["commit"], tmp_path)
    measured = source_identity(tmp_path, identity_path)
    tree = subprocess.run(
        ["git", "rev-parse", f"{expected['commit']}^{{tree}}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert measured["commit"] == expected["commit"]
    assert measured["tree"] == tree
    assert "commit" not in signature(build_receipt).parameters
    assert "tree" not in signature(build_receipt).parameters


def _row(deployment: str, replica: str, worker: str) -> dict:
    run_id = f"{deployment}-{replica}-{worker}"
    return {
        "profile": "sustained",
        "deployment_ref": deployment,
        "replica": replica,
        "worker": worker,
        "fault": None,
        "started_at_ms": 0.0,
        "finished_at_ms": 1.0,
        "latency_ms": 1.0,
        "status_code": 202,
        "queue_depth": 0,
        "cpu_percent": 1.0,
        "memory_bytes": 1,
        "tenant_id": "tenant",
        "lifecycle": [
            {"state": "accepted", "at_ms": 0.0, "run_id": run_id},
            {"state": "completed", "at_ms": 1.0, "run_id": run_id},
        ],
    }


def test_topology_and_fairness_include_missing_identities_per_deployment() -> None:
    from release.load.measurements import _matrix_errors, recompute

    matrix = {"tenants": 1, "deployments_per_tenant": 2, "replicas": 2, "workers": 3}
    replica_rows = [
        _row(deployment, replica, worker)
        for deployment, replica in (("deployment-a", "replica-1"), ("deployment-b", "replica-2"))
        for worker in ("worker-1", "worker-2", "worker-3")
    ]
    worker_rows = [
        _row("deployment-a", "replica-1", "worker-1"),
        _row("deployment-a", "replica-2", "worker-2"),
        _row("deployment-b", "replica-1", "worker-2"),
        _row("deployment-b", "replica-2", "worker-3"),
    ]

    assert any(
        "deployment-a replica" in error for error in _matrix_errors(replica_rows, matrix, "profile")
    )
    assert any(
        "deployment-a worker" in error for error in _matrix_errors(worker_rows, matrix, "profile")
    )
    assert (
        recompute(replica_rows, {"sustained": {}}, matrix)["sustained"]["replica_fairness"] == 0.5
    )
    assert recompute(worker_rows, {"sustained": {}}, matrix)["sustained"][
        "worker_fairness"
    ] == pytest.approx(2 / 3, abs=1e-6)


@pytest.mark.parametrize(
    "events",
    [
        [{"state": "draining", "at_ms": 1.0, "run_id": "run-1"}],
        [
            {"state": "accepted", "at_ms": 0.0, "run_id": "run-1"},
            {"state": "cancel-requested", "at_ms": 1.0, "run_id": "run-1"},
            {"state": "completed", "at_ms": 2.0, "run_id": "run-1"},
        ],
        [
            {"state": "accepted", "at_ms": 0.0, "run_id": "run-1"},
            {"state": "completed", "at_ms": 1.0, "run_id": "run-1"},
            {"state": "draining", "at_ms": 2.0, "run_id": "run-1"},
        ],
    ],
)
def test_drain_and_cancel_require_an_ordered_matching_terminal(events: list[dict]) -> None:
    from release.load.measurements import _lifecycle_errors

    assert _lifecycle_errors("request-1", events)


def test_documented_reproduction_waits_boundedly_for_both_services() -> None:
    page = (ROOT / "docs/how-to/deployment/release-gates.md").read_text()

    assert "--health-cmd 'pg_isready -U zeroth -d zeroth'" in page
    assert "--health-cmd 'redis-cli ping'" in page
    assert "for attempt in $(seq 1 60)" in page
    assert ".State.Health.Status" in page
