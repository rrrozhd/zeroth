"""Fail-closed result tooling for matrix-bound security tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from release.security.matrix import main, verify_coverage, verify_outcomes


MATRIX = Path(__file__).parent / "fixtures" / "valid-matrix.json"


def _outcomes(path: Path, records: list[dict[str, object]]) -> Path:
    path.write_text(json.dumps({"schema_version": 1, "records": records}), encoding="utf-8")
    return path


def _record(nodeid: str, phase: str, outcome: str = "passed", **extra: object) -> dict[str, object]:
    return {
        "nodeid": nodeid,
        "phase": phase,
        "outcome": outcome,
        "skip": None,
        "wasxfail": None,
        **extra,
    }


def _passing_records(nodeid: str) -> list[dict[str, object]]:
    return [_record(nodeid, phase) for phase in ("setup", "call", "teardown")]


def test_nodes_cli_emits_exact_nul_delimited_parameterized_node_ids(tmp_path: Path) -> None:
    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
    expected = [
        "tests/security/test_surface_isolation.py::test_same_key[tenant/a]",
        "tests/security/test_surface_isolation.py::test_same_key[tenant b]",
    ]
    matrix["cases"][0]["test_nodes"] = expected
    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "release.security.matrix",
            "--matrix",
            str(matrix_path),
            "nodes",
            "--tier",
            "pr-critical",
            "--format",
            "nul",
        ],
        cwd=Path(__file__).parents[2],
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == b"\0".join(item.encode() for item in expected) + b"\0"


def test_plugin_writes_canonical_phase_outcomes_without_captured_output(tmp_path: Path) -> None:
    test_file = tmp_path / "test_sample.py"
    test_file.write_text(
        "def test_pass():\n"
        "    print('DO-NOT-CAPTURE')\n"
        "\n"
        "def test_fail():\n"
        "    assert False, 'DO-NOT-CAPTURE'\n",
        encoding="utf-8",
    )
    output = tmp_path / "outcomes.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "release.security.pytest_plugin",
            f"--security-results={output}",
            str(test_file),
        ],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    raw = output.read_text(encoding="utf-8")
    assert "DO-NOT-CAPTURE" not in raw
    payload = json.loads(raw)
    by_node = {
        record["nodeid"]: record for record in payload["records"] if record["phase"] == "call"
    }
    assert by_node["test_sample.py::test_pass"]["outcome"] == "passed"
    assert by_node["test_sample.py::test_fail"]["outcome"] == "failed"
    assert all(
        set(record) == {"nodeid", "phase", "outcome", "skip", "wasxfail"}
        for record in payload["records"]
    )


def test_plugin_records_wasxfail_for_strict_xfail_and_nonstrict_xpass(tmp_path: Path) -> None:
    test_file = tmp_path / "test_xfail.py"
    test_file.write_text(
        "import pytest\n"
        "@pytest.mark.xfail(strict=True, reason='strict-reason')\n"
        "def test_strict_xfail(): assert False\n"
        "@pytest.mark.xfail(strict=False, reason='xpass-reason')\n"
        "def test_nonstrict_xpass(): pass\n",
        encoding="utf-8",
    )
    output = tmp_path / "outcomes.json"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "release.security.pytest_plugin",
            f"--security-results={output}",
            str(test_file),
        ],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        check=False,
    )

    calls = {
        record["nodeid"].rsplit("::", 1)[-1]: record
        for record in json.loads(output.read_text(encoding="utf-8"))["records"]
        if record["phase"] == "call"
    }
    assert calls["test_strict_xfail"]["wasxfail"] == "strict-reason"
    assert calls["test_nonstrict_xpass"]["wasxfail"] == "xpass-reason"


def test_plugin_atomically_leaves_parseable_evidence_after_setup_error(tmp_path: Path) -> None:
    test_file = tmp_path / "test_error.py"
    test_file.write_text(
        "import pytest\n"
        "@pytest.fixture\n"
        "def broken(): raise RuntimeError('setup exploded')\n"
        "def test_error(broken): pass\n",
        encoding="utf-8",
    )
    output = tmp_path / "outcomes.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "release.security.pytest_plugin",
            f"--security-results={output}",
            str(test_file),
        ],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert any(
        record["phase"] == "setup" and record["outcome"] == "failed"
        for record in payload["records"]
    )
    assert not list(tmp_path.glob(".*outcomes.json.*.tmp"))


def test_plugin_leaves_parseable_evidence_after_keyboard_interrupt(tmp_path: Path) -> None:
    test_file = tmp_path / "test_interrupt.py"
    test_file.write_text(
        "import os, signal\ndef test_interrupt(): os.kill(os.getpid(), signal.SIGINT)\n",
        encoding="utf-8",
    )
    output = tmp_path / "outcomes.json"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "release.security.pytest_plugin",
            f"--security-results={output}",
            str(test_file),
        ],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["records"] == [
        {
            "nodeid": "test_interrupt.py::test_interrupt",
            "phase": "setup",
            "outcome": "passed",
            "skip": None,
            "wasxfail": None,
        }
    ]


def test_collection_rejects_xfail_on_a_matrix_bound_node(tmp_path: Path) -> None:
    test_file = tmp_path / "test_bound.py"
    test_file.write_text(
        "import pytest\n@pytest.mark.xfail(reason='not release evidence')\ndef test_bound(): assert False\n",
        encoding="utf-8",
    )
    nodeid = "test_bound.py::test_bound"
    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
    matrix["cases"][0]["test_nodes"] = [nodeid]
    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
    output = tmp_path / "outcomes.json"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "release.security.pytest_plugin",
            f"--security-results={output}",
            f"--security-matrix={matrix_path}",
            str(test_file),
        ],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "matrix-bound security test may not use xfail" in (result.stdout + result.stderr)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("missing", 1),
        ("duplicate", 1),
        ("unbound", 1),
        ("valid", 0),
    ],
)
def test_verify_coverage_has_independent_exit_semantics(
    tmp_path: Path, mutation: str, expected: int
) -> None:
    nodeid = "tests/security/test_matrix_contract.py::test_fixture_behavioral_binding"
    records = _passing_records(nodeid)
    if mutation == "missing":
        records = []
    elif mutation == "duplicate":
        records.append(_record(nodeid, "call"))
    elif mutation == "unbound":
        records.extend(_passing_records("tests/security/test_unbound.py::test_extra"))

    output = tmp_path / "coverage.json"
    rc = verify_coverage(
        MATRIX,
        "pr-critical",
        _outcomes(tmp_path / "outcomes.json", records),
        output,
    )

    assert rc == expected
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == (
        "passed" if expected == 0 else "failed"
    )


@pytest.mark.parametrize(
    "mutation",
    ["skip", "failure", "setup-error", "teardown-error", "xfail", "xpass", "incomplete"],
)
def test_verify_outcomes_rejects_every_nonpassing_terminal_state(
    tmp_path: Path, mutation: str
) -> None:
    nodeid = "tests/security/test_matrix_contract.py::test_fixture_behavioral_binding"
    records = _passing_records(nodeid)
    if mutation == "skip":
        records[0] = _record(nodeid, "setup", "skipped", skip="requires redis")
    elif mutation == "failure":
        records[1] = _record(nodeid, "call", "failed")
    elif mutation == "setup-error":
        records[0] = _record(nodeid, "setup", "failed")
        records.pop(1)
    elif mutation == "teardown-error":
        records[2] = _record(nodeid, "teardown", "failed")
    elif mutation == "xfail":
        records[1] = _record(nodeid, "call", "skipped", wasxfail="known bug")
    elif mutation == "xpass":
        records[1] = _record(nodeid, "call", "passed", wasxfail="unexpected pass")
    elif mutation == "incomplete":
        records.pop()

    output = tmp_path / "verdict.json"
    rc = verify_outcomes(
        MATRIX,
        "pr-critical",
        _outcomes(tmp_path / "outcomes.json", records),
        output,
    )

    assert rc == 1
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "failed"


def test_cli_returns_distinct_usage_code_for_unreadable_evidence(tmp_path: Path) -> None:
    rc = main(
        [
            "--matrix",
            str(MATRIX),
            "verify-outcomes",
            "--tier",
            "pr-critical",
            "--outcomes",
            str(tmp_path / "missing.json"),
            "--output",
            str(tmp_path / "verdict.json"),
        ]
    )

    assert rc == 2
