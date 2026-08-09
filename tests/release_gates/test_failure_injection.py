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

from .conftest import CLI, MANIFEST_PATH


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
