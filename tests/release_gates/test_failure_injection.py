"""R4 — every gate can independently block promotion.

AC5 asks for proof that no gate is decorative and no gate is masked by
another. For each gate and each way evidence can be wrong, exactly that gate
is corrupted and the run must block naming exactly that gate.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from release.security.matrix import load_matrix, verify_coverage, verify_outcomes
from release.security.scan import main as scan_main

from .conftest import CLI, MANIFEST_PATH, ROOT


SECURITY_MATRIX = ROOT / "release/security/security-matrix.json"
SECURITY_NODES = load_matrix(SECURITY_MATRIX).nodes("release-candidate")


def _passing_outcomes() -> list[dict[str, object]]:
    return [
        {
            "nodeid": nodeid,
            "phase": phase,
            "outcome": "passed",
            "skip": False,
            "wasxfail": False,
        }
        for nodeid in SECURITY_NODES
        for phase in ("setup", "call", "teardown")
    ]


def _write_outcomes(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        json.dumps({"schema_version": 1, "records": records}), encoding="utf-8"
    )


def _corrupt(root: Path, gate: dict, mode: str) -> None:
    path = root / gate["record"]
    if mode == "missing":
        path.unlink()
        return
    record = json.loads(path.read_text(encoding="utf-8"))
    if mode == "stale":
        record["identity"]["commit"] = "f" * 40
    elif mode == "partial":
        record["results"].pop(gate["requires"][0])
    elif mode == "mismatched":
        facet = next(item for item in gate["binds"] if item != "commit")
        record["identity"][facet] = {"tampered": True} if facet != "configuration" else "sha256:0"
    elif mode == "failed":
        record["results"][gate["requires"][0]] = "failed"
    else:  # pragma: no cover - guards the parametrization itself
        raise AssertionError(f"unknown corruption mode {mode!r}")
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _cases():
    from gates.manifest import load_manifest

    for gate in load_manifest(MANIFEST_PATH)["gates"]:
        for mode in ("missing", "stale", "partial", "mismatched", "failed"):
            # A gate binding only ``commit`` has no second facet to mismatch;
            # a wrong commit there is staleness, which is covered above.
            if mode == "mismatched" and [item for item in gate["binds"] if item != "commit"] == []:
                continue
            yield pytest.param(gate["id"], mode, id=f"{gate['id']}-{mode}")


@pytest.mark.parametrize(("gate_id", "mode"), list(_cases()))
def test_corrupting_one_gate_blocks_promotion_and_names_it(
    manifest, candidate, evidence, gate_id, mode
):
    from gates.validate import releasable, validate

    gate = next(item for item in manifest["gates"] if item["id"] == gate_id)
    _corrupt(evidence, gate, mode)

    results = validate(manifest, candidate, evidence, phase="final")
    blocking = [result for result in results if result.blocking]

    assert not releasable(results), f"{gate_id}/{mode} did not block promotion"
    assert [result.gate for result in blocking] == [gate_id], (
        f"{gate_id}/{mode} should block exactly its own gate"
    )
    assert blocking[0].status == mode
    assert blocking[0].reason


def test_every_manifest_gate_is_exercised_by_this_suite(manifest):
    """Guards the parametrization: a new gate must not slip in unproven."""
    exercised = {case.values[0] for case in _cases()}

    assert exercised == {gate["id"] for gate in manifest["gates"]}


@pytest.mark.parametrize("gate_id", ["source", "deployment-smoke", "promotion"])
def test_the_cli_exits_nonzero_so_a_ci_job_actually_fails(
    manifest, candidate, evidence, tmp_path, gate_id
):
    """`needs:` keys off the process exit status, so that is what is asserted."""
    gate = next(item for item in manifest["gates"] if item["id"] == gate_id)
    _corrupt(evidence, gate, "missing")
    identity_path = tmp_path / "candidate-identity.json"
    identity_path.write_text(json.dumps(candidate), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "validate",
            "--manifest",
            str(MANIFEST_PATH),
            "--identity",
            str(identity_path),
            "--evidence-root",
            str(evidence),
            "--phase",
            "final",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1, completed.stderr
    assert gate_id in completed.stderr


def test_the_cli_exits_zero_on_complete_evidence(candidate, evidence, tmp_path):
    identity_path = tmp_path / "candidate-identity.json"
    identity_path.write_text(json.dumps(candidate), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "validate",
            "--manifest",
            str(MANIFEST_PATH),
            "--identity",
            str(identity_path),
            "--evidence-root",
            str(evidence),
            "--phase",
            "final",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout


def test_each_security_subresult_independently_blocks_promotion(
    manifest, candidate, evidence
):
    from gates.validate import releasable, validate

    gate = next(item for item in manifest["gates"] if item["id"] == "security-regression")
    record_path = evidence / gate["record"]
    pristine = json.loads(record_path.read_text(encoding="utf-8"))

    for required in gate["requires"]:
        record = {**pristine, "results": {**pristine["results"], required: "failed"}}
        record_path.write_text(json.dumps(record), encoding="utf-8")
        results = validate(manifest, candidate, evidence, phase="final")
        blocking = [result for result in results if result.blocking]
        assert not releasable(results)
        assert [(result.gate, result.status) for result in blocking] == [
            ("security-regression", "failed")
        ]


def test_incomplete_security_matrix_independently_fails_coverage(tmp_path: Path) -> None:
    outcomes = tmp_path / "outcomes.json"
    missing = SECURITY_NODES[0]
    _write_outcomes(
        outcomes,
        [record for record in _passing_outcomes() if record["nodeid"] != missing],
    )
    output = tmp_path / "coverage.json"

    assert verify_coverage(
        SECURITY_MATRIX, "release-candidate", outcomes, output
    ) == 1
    assert json.loads(output.read_text(encoding="utf-8"))["missing_nodes"] == [missing]


@pytest.mark.parametrize("skipped_node", SECURITY_NODES)
def test_each_required_security_node_skip_independently_fails_outcomes(
    tmp_path: Path, skipped_node: str
) -> None:
    records = _passing_outcomes()
    skipped = next(
        record
        for record in records
        if record["nodeid"] == skipped_node and record["phase"] == "setup"
    )
    skipped.update(outcome="skipped", skip=True)
    outcomes = tmp_path / "outcomes.json"
    _write_outcomes(outcomes, records)

    assert verify_outcomes(
        SECURITY_MATRIX,
        "release-candidate",
        outcomes,
        tmp_path / "verdict.json",
    ) == 1


@pytest.mark.parametrize(
    "service_node",
    [
        next(node for node in SECURITY_NODES if "distributed/test_redis" in node),
        next(node for node in SECURITY_NODES if "distributed/test_postgres" in node),
        next(node for node in SECURITY_NODES if "test_run_in_docker" in node),
    ],
    ids=["redis-unavailable", "postgres-unavailable", "docker-unavailable"],
)
def test_unavailable_required_service_skip_blocks_independently(
    tmp_path: Path, service_node: str
) -> None:
    records = _passing_outcomes()
    skipped = next(
        record
        for record in records
        if record["nodeid"] == service_node and record["phase"] == "setup"
    )
    skipped.update(outcome="skipped", skip=True)
    outcomes = tmp_path / "outcomes.json"
    _write_outcomes(outcomes, records)

    assert verify_outcomes(
        SECURITY_MATRIX,
        "release-candidate",
        outcomes,
        tmp_path / "verdict.json",
    ) == 1


def test_leaked_canary_independently_fails_the_scanner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canary = "zeroth-security-regression-canary-7f3a"
    leaked = tmp_path / "junit.xml"
    leaked.write_text(f"<testsuite><system-out>{canary}</system-out></testsuite>", encoding="utf-8")
    output = tmp_path / "scan.json"
    monkeypatch.setenv("ZEROTH_SECURITY_CANARIES", json.dumps([canary]))

    assert scan_main(
        ["--root", str(tmp_path), "--output", str(output), str(leaked)]
    ) == 1
    raw = output.read_text(encoding="utf-8")
    assert canary not in raw
    assert json.loads(raw)["status"] == "failed"
