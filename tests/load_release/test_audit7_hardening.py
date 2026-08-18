"""Regressions for ZER-33 AUDIT-7 evidence causality."""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/release-gates.yml"
DOCS = ROOT / "docs/how-to/deployment/release-gates.md"


def _committed_source(path: Path, version: str = "1.2.3") -> Path:
    source = path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text(
        f'[project]\nname = "candidate"\nversion = "{version}"\n', encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(["git", "add", "pyproject.toml"], cwd=source, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Receipt Test",
            "-c",
            "user.email=receipt@example.invalid",
            "commit",
            "-qm",
            "candidate",
        ],
        cwd=source,
        check=True,
    )
    return source


def test_fault_recovery_requires_one_ordered_same_row_chain() -> None:
    from release.load.report import evidence_errors, load_profiles
    from tests.load_release.test_report import FAULT_STATES, PROFILES, _fault_row

    injected = _fault_row(1, 0, 503, 1)
    observations = copy.deepcopy(injected)
    injected["lifecycle"] = [
        {"state": "fault-injected", "at_ms": 0.0},
        {"state": "recovered", "at_ms": 1.0, "repair": "automatic"},
    ]
    observations["request_id"] = "request-2"
    observations["lifecycle"] = [
        {"state": state, "at_ms": float(index + 2)}
        for index, state in enumerate(FAULT_STATES["database-contention"])
    ]

    errors = evidence_errors([injected, observations], load_profiles(PROFILES))

    assert any("database-contention" in error and "ordered same-row" in error for error in errors)


def test_fault_recovery_rejects_restoration_after_automatic_recovery() -> None:
    from release.load.report import evidence_errors, load_profiles
    from tests.load_release.test_report import PROFILES, _fault_row

    row = _fault_row(1, 0, 503, 1)
    row["lifecycle"] = [
        {"state": "fault-injected", "at_ms": 0.0},
        {"state": "coordination-timeout", "at_ms": 1.0},
        {"state": "recovered", "at_ms": 2.0, "repair": "automatic"},
        {"state": "query-restored", "at_ms": 3.0},
    ]

    errors = evidence_errors([row], load_profiles(PROFILES))

    assert any("database-contention" in error and "ordered same-row" in error for error in errors)


def test_recovery_measurement_rejects_an_inverted_interval() -> None:
    from release.load.measurements import recompute
    from tests.load_release.test_report import _fault_row, _workload_row

    row = _fault_row(1, 0, 503, 1)
    row["lifecycle"] = [
        {"state": "fault-injected", "at_ms": 10.0},
        {"state": "recovered", "at_ms": 5.0, "repair": "automatic"},
    ]

    with pytest.raises(ValueError, match="recovery precedes fault injection"):
        recompute([_workload_row("overload", 0, 2, 1), row], {"overload": {}})


def test_candidate_receipt_atomically_binds_exact_head_source_to_observations(
    tmp_path: Path,
) -> None:
    from release.gates.identity import candidate_identity, identity_digest
    from release.load.environment import observation_digest
    from release.load.receipt import build_candidate_receipt

    source = _committed_source(tmp_path)
    identity = candidate_identity(source)
    identity_path = tmp_path / "candidate-identity.json"
    identity_path.write_text(json.dumps(identity), encoding="utf-8")
    observations = [{"request_id": "measured"}]
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps(observations), encoding="utf-8")

    receipt = build_candidate_receipt(source, raw, identity_path)

    assert (
        receipt["commit"]
        == subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=source, check=True, capture_output=True, text=True
        ).stdout.strip()
    )
    assert (
        receipt["tree"]
        == subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"],
            cwd=source,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    assert receipt["candidate_identity_digest"] == identity_digest(identity)
    assert receipt["observation_digest"] == observation_digest(observations)
    assert receipt["source_digest"].startswith("sha256:")


def test_candidate_receipt_cli_writes_the_bound_record_atomically(tmp_path: Path) -> None:
    from release.gates.identity import candidate_identity

    source = _committed_source(tmp_path)
    identity = tmp_path / "candidate-identity.json"
    identity.write_text(json.dumps(candidate_identity(source)), encoding="utf-8")
    raw = tmp_path / "raw.json"
    raw.write_text('[{"request_id":"measured"}]', encoding="utf-8")
    output = tmp_path / "candidate-source-receipt.json"

    result = subprocess.run(
        [
            str(ROOT / ".venv/bin/python"),
            str(ROOT / "release/load/receipt.py"),
            "candidate",
            "--source",
            str(source),
            "--identity",
            str(identity),
            "--raw",
            str(raw),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text())["commit"] == candidate_identity(source)["commit"]
    assert not output.with_suffix(".json.tmp").exists()


def test_candidate_receipt_rejects_a_dirty_version_not_present_in_exact_head(
    tmp_path: Path,
) -> None:
    from release.gates.identity import candidate_identity
    from release.load.receipt import build_candidate_receipt

    source = _committed_source(tmp_path)
    (source / "pyproject.toml").write_text(
        '[project]\nname = "candidate"\nversion = "9.9.9"\n', encoding="utf-8"
    )
    identity = tmp_path / "candidate-identity.json"
    identity.write_text(json.dumps(candidate_identity(source)), encoding="utf-8")
    raw = tmp_path / "raw.json"
    raw.write_text('[{"request_id":"measured"}]', encoding="utf-8")

    with pytest.raises(ValueError, match="exact HEAD source"):
        build_candidate_receipt(source, raw, identity)


def test_release_runner_retains_the_exact_head_source_receipt() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    docs = DOCS.read_text(encoding="utf-8")
    manifest = json.loads((ROOT / "release/gates/release-gates.json").read_text())
    gate = next(gate for gate in manifest["gates"] if gate["id"] == "load-recovery")

    for text in (workflow, docs):
        assert "release/load/receipt.py candidate" in text
        assert "load-recovery-source-receipt.json" in text
    assert "source-receipt=$(status ${RECEIPT})" in workflow
    assert "--kind source-receipt=release/evidence/load-recovery-source-receipt.json" in workflow
    assert '"${RECEIPT}" -ne 0' in workflow
    assert "source-receipt" in gate["requires"]
    assert "source-receipt" in gate["kinds"]
