"""Negative fixtures: every release gate must fail for the reason it names.

Each test feeds one gate a deliberately broken input and asserts the gate rejects
it. That is the point of the module -- a gate that only ever sees valid input is
indistinguishable from a gate that checks nothing, and every input here was
measured passing green before the paired fix landed (ZER-41 / G01).

Positive assertions against the real artifacts sit next to them, so a fix that
makes the negative case fail by also breaking the honest case is caught here.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from release.security.matrix import load_matrix, verify_outcomes

from .conftest import ROOT

WORKFLOWS = ROOT / ".github/workflows"
SECURITY_MATRIX = ROOT / "release/security/security-matrix.json"
MATRIX_FIXTURE = ROOT / "tests/security/fixtures/valid-matrix.json"

STUB_MODULE = "test_bound_stub.py"


# ---------------------------------------------------------------------------
# R2 -- a matrix-bound security test may not be skipped
# ---------------------------------------------------------------------------


def _matrix_binding_stubs(tmp_path: Path) -> tuple[Path, list[str]]:
    """A schema-valid matrix whose bound nodes are all stubs in ``tmp_path``.

    Built by substitution from the repository's own valid fixture rather than
    hand-written, so it stays valid as the schema evolves. Each distinct original
    node maps to a distinct stub -- the schema rejects a duplicated binding --
    and rewriting *every* node reference keeps the ``claims`` section pointing at
    bound nodes, which ``load_matrix`` also requires.

    Returns the matrix path and the stub function names, in binding order.
    """
    raw = MATRIX_FIXTURE.read_text(encoding="utf-8")
    mapping: dict[str, str] = {}

    def rewrite(value: Any) -> Any:
        if isinstance(value, str):
            if "::" not in value:
                return value
            name = mapping.setdefault(value, f"test_bound_stub_case_{len(mapping)}")
            return f"{STUB_MODULE}::{name}"
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        if isinstance(value, dict):
            return {key: rewrite(item) for key, item in value.items()}
        return value

    matrix = rewrite(json.loads(raw))
    path = tmp_path / "matrix.json"
    path.write_text(json.dumps(matrix), encoding="utf-8")
    return path, list(mapping.values())


def _run_bound_stub(tmp_path: Path, decorator: str) -> subprocess.CompletedProcess[str]:
    """Collect matrix-bound stubs, the first carrying ``decorator``, through the plugin."""
    matrix, names = _matrix_binding_stubs(tmp_path)
    body = "raise AssertionError('this test must never be reported as evidence')"
    source = "import pytest\n"
    for index, name in enumerate(names):
        marker = f"\n{decorator}" if index == 0 else ""
        source += f"\n{marker}\ndef {name}() -> None:\n    {body}\n"
    (tmp_path / STUB_MODULE).write_text(source, encoding="utf-8")

    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "release.security.pytest_plugin",
            "--security-matrix",
            str(matrix),
            "-p",
            "no:cacheprovider",
            "-q",
            STUB_MODULE,
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        env={
            "PYTHONPATH": str(ROOT),
            "ZEROTH_SECURITY_MATRIX_FIXTURE_MODE": "1",
            "PATH": "/usr/bin:/bin",
        },
    )


@pytest.mark.parametrize(
    "decorator",
    [
        "@pytest.mark.skip",
        "@pytest.mark.skip(reason='docker unavailable')",
        "@pytest.mark.skipif(True, reason='docker unavailable')",
        "@pytest.mark.skipif(False, reason='never true here')",
        "@pytest.mark.xfail",
    ],
)
def test_matrix_bound_skip_is_refused_at_collection(tmp_path: Path, decorator: str) -> None:
    """skip, skipif and xfail all suppress execution, so all three are refused.

    Before ZER-41 only ``xfail`` was refused. A file of one ``@pytest.mark.skip``
    and one ``@pytest.mark.skipif(True)`` exited 0 with both nodes still
    *collected*, so the exact-collection guard stayed satisfied and
    ``EXIT_NOTESTSCOLLECTED`` never fired: the gate reported success over a test
    that never ran.

    ``skipif(False)`` is refused too. Its condition is evaluated against the
    environment, so a marker that is harmless on the author's machine is exactly
    the one that skips in CI; a bound node may not carry the mechanism at all.
    """
    result = _run_bound_stub(tmp_path, decorator)

    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "may not use xfail, skip or skipif" in combined, combined
    assert "test_bound_stub_case_0" in combined, combined


def test_an_unmarked_bound_node_still_collects(tmp_path: Path) -> None:
    """The refusal must reject the marker, not every bound node.

    Without this, a rule that refused everything would satisfy the tests above
    while making the gate unusable. The stubs all fail on purpose, so reaching
    the failure report is proof collection completed rather than being refused.
    """
    result = _run_bound_stub(tmp_path, "")

    combined = result.stdout + result.stderr
    assert "may not use xfail, skip or skipif" not in combined, combined
    assert "failed" in combined and "matrix-bound" not in combined, combined


# ---------------------------------------------------------------------------
# R3 -- the pull-request path verifies outcomes, not just the exit code
# ---------------------------------------------------------------------------


def _workflow(name: str) -> dict[str, Any]:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _security_regression_steps() -> list[dict[str, Any]]:
    return _workflow("ci.yml")["jobs"]["security-regression"]["steps"]


def test_pull_request_path_verifies_security_outcomes_and_coverage() -> None:
    """``pytest_gate``'s exit code is not the whole verdict.

    A bound node skipped at run time leaves the process at 0, so the job has to
    read the outcomes file it already writes. Before ZER-41 this job had exactly
    two steps -- run, then upload -- and nothing ever verified the uploaded file.
    """
    runs = " ".join(str(step.get("run", "")) for step in _security_regression_steps())

    assert "verify-outcomes" in runs
    assert "verify-coverage" in runs
    assert "--tier pr-critical" in runs


def test_pull_request_outcome_verification_survives_a_failing_subset() -> None:
    """The verdict must be computed when the subset failed -- that is when it matters."""
    verifying = [
        step
        for step in _security_regression_steps()
        if "verify-outcomes" in str(step.get("run", ""))
        or "verify-coverage" in str(step.get("run", ""))
    ]

    assert verifying
    for step in verifying:
        assert step.get("if") == "always()", step.get("name")


def test_pull_request_critical_tier_binds_a_non_empty_node_set() -> None:
    """A verifier over an empty expected set passes unconditionally.

    Wiring ``verify-outcomes`` in would itself be a new vacuous gate if the tier
    bound nothing, so the binding is asserted rather than assumed.
    """
    assert len(load_matrix(SECURITY_MATRIX).nodes("pr-critical")) > 0


def test_verify_outcomes_rejects_a_skipped_bound_node(tmp_path: Path) -> None:
    """The gate itself, fed a document in which a bound node was skipped at run time.

    This is the half the collection guard cannot see: ``pytest.skip()`` called
    inside a test body leaves the marker absent and the process at 0.
    """
    nodes = load_matrix(SECURITY_MATRIX).nodes("pr-critical")
    skipped = sorted(nodes)[0]
    records = [
        {
            "nodeid": nodeid,
            "phase": phase,
            "outcome": "skipped" if (nodeid == skipped and phase == "call") else "passed",
            "skip": nodeid == skipped and phase == "call",
            "wasxfail": False,
        }
        for nodeid in nodes
        for phase in ("setup", "call", "teardown")
    ]
    outcomes = tmp_path / "outcomes.json"
    outcomes.write_text(
        json.dumps({"schema_version": 1, "records": records}), encoding="utf-8"
    )
    output = tmp_path / "verdict.json"

    assert verify_outcomes(SECURITY_MATRIX, "pr-critical", outcomes, output) == 1
    verdict = json.loads(output.read_text(encoding="utf-8"))
    assert verdict["status"] == "failed"
    assert verdict["failures"] == [{"nodeid": skipped, "phase": "call", "reason": "skipped"}]

    # The same document with that one skip removed passes, so the rejection is
    # the skip and not something incidental about the fixture.
    for record in records:
        record["outcome"], record["skip"] = "passed", False
    outcomes.write_text(
        json.dumps({"schema_version": 1, "records": records}), encoding="utf-8"
    )
    assert verify_outcomes(SECURITY_MATRIX, "pr-critical", outcomes, output) == 0
