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
import re
import shutil
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


# ---------------------------------------------------------------------------
# R6 -- a captured exit status must actually be captured
# ---------------------------------------------------------------------------

#: A status capture, in both shapes the workflows use: trailing on the command
#: (``cmd; RC=$?``) and standing alone on the next line (``RC=$?``).
_CAPTURE = re.compile(r"(?:^|;)\s*([A-Za-z_][A-Za-z0-9_]*)=\$\?\s*$")

#: ``set`` invocations that toggle errexit. ``set -uo pipefail`` matches neither,
#: which is the whole point: it leaves the inherited ``-e`` in place.
_CLEARS_ERREXIT = re.compile(r"^set\b[^#]*\+[a-z]*e")
_SETS_ERREXIT = re.compile(r"^set\b[^#]*-[a-z]*e")


def _errexit_state(stripped: str, cleared: bool) -> bool:
    """Whether errexit is cleared after ``stripped`` runs, given it was ``cleared``."""
    if _CLEARS_ERREXIT.match(stripped):
        return True
    if _SETS_ERREXIT.match(stripped):
        return False
    return cleared


def _uncapturable(script: str) -> tuple[list[str], dict[str, int], bool]:
    """Captures made while errexit is enabled, every capture, and the closing state."""
    problems: list[str] = []
    captures: dict[str, int] = {}
    cleared = False
    for number, line in enumerate(script.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        cleared = _errexit_state(stripped, cleared)
        match = _CAPTURE.search(stripped)
        if match is None:
            continue
        captures.setdefault(match.group(1), number)
        if not cleared:
            problems.append(
                f"line {number}: captures ${{{match.group(1)}}} with errexit still "
                "enabled, so the capture never runs when the command fails"
            )
    return problems, captures, cleared


def errexit_capture_problems(script: str) -> list[str]:
    """Report ways a ``run:`` block's exit-status captures cannot mean what they say.

    Three rules, named exactly -- this is a ratchet, not a proof that the script
    is correct:

    1. **The capture must run.** GitHub's ``shell: bash`` is invoked with ``-e``
       and ``set -uo pipefail`` does not clear it, so a failing command aborts the
       step *before* the following ``VAR=$?``. Every captured status could then
       only ever be 0, and the recorded gate result was decided by whichever
       command failed first rather than by the results being recorded.
    2. **The capture must be used.** Clearing errexit and then discarding the
       status is the same silence by a different route.
    3. **The block must decide its own status.** Once errexit is cleared it has to
       be restored, or the block has to ``exit`` explicitly; otherwise the step
       succeeds on the exit code of whatever ran last.
    """
    problems, captures, cleared = _uncapturable(script)
    if not captures:
        return problems

    problems.extend(
        f"line {number}: captures ${{{name}}} and never reads it"
        for name, number in captures.items()
        if not re.search(rf"\$\{{{name}\}}|\${name}\b", script)
    )
    if cleared and not re.search(r"^\s*exit\b", script, re.MULTILINE):
        problems.append(
            "clears errexit and neither restores it nor exits explicitly, so the step "
            "succeeds on whatever ran last"
        )
    return problems


def _run_blocks() -> list[tuple[str, str]]:
    """Every ``run:`` script in every workflow, as (where, script)."""
    blocks: list[tuple[str, str]] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_name, job in (workflow.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                script = step.get("run")
                if isinstance(script, str):
                    blocks.append((f"{path.name}:{job_name}:{step.get('name')}", script))
    return blocks


def errexit_is_cleared_by(prologue: str) -> bool:
    """Whether ``prologue`` leaves errexit clear -- asked of bash, not read off the script.

    ``errexit_capture_problems`` reasons about ``set`` lines textually, which is a
    *representation* of the shell state. Every other guard in this task that
    reasoned about a representation was eventually defeated by another
    representation of the same thing, so this one is checked against the shell
    itself: run the prologue under the flags GitHub uses, fail a command, and see
    whether the capture on the next line executes.
    """
    script = f'{prologue}\nfalse\nCAPTURED=$?\necho "ran:$CAPTURED"\n'
    result = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    return "ran:" in result.stdout


def _has_capture(script: str) -> bool:
    """Whether any line of ``script`` captures an exit status.

    Line-wise: ``_CAPTURE`` is anchored to end-of-line, so searching a whole
    multi-line script matches nothing at all -- which would have made both checks
    below pass over zero blocks.
    """
    return any(_CAPTURE.search(line.strip()) for line in script.splitlines())


def _prologue_of(script: str) -> str:
    """The leading ``set`` lines of a run block, up to its first capture."""
    lines = []
    for line in script.splitlines():
        stripped = line.strip()
        if _CAPTURE.search(stripped):
            break
        if stripped.startswith("set "):
            lines.append(stripped)
    return "\n".join(lines)


def test_bash_agrees_that_every_capture_block_has_errexit_cleared() -> None:
    """The property, executed: each real block's prologue is run and observed.

    ``set -uo pipefail`` alone leaves errexit on and the capture never runs --
    confirmed against bash below, not asserted from documentation.
    """
    failures = [
        where
        for where, script in _run_blocks()
        if _has_capture(script) and not errexit_is_cleared_by(_prologue_of(script))
    ]

    assert failures == [], (
        "bash reports that these blocks abort before their capture: " + ", ".join(failures)
    )


def test_at_least_one_real_block_is_exercised_against_bash() -> None:
    """A prologue extractor that returned nothing would make the check above pass."""
    exercised = [
        where for where, script in _run_blocks() if _has_capture(script)
    ]

    assert len(exercised) >= 8, exercised


@pytest.mark.parametrize(
    ("prologue", "cleared"),
    [
        ("set -uo pipefail", False),
        ("set -euo pipefail", False),
        ("", False),
        ("set -uo pipefail\nset +e", True),
        ("set +e -u -o pipefail", True),
        ("set +e", True),
    ],
)
def test_bash_is_a_working_oracle_for_the_prologues_that_matter(
    prologue: str, cleared: bool
) -> None:
    """Both directions against the shell, so the oracle itself is not assumed."""
    assert errexit_is_cleared_by(prologue) is cleared


def test_every_workflow_capture_site_can_actually_capture() -> None:
    """The eight blocks A11-1, A11-2 and A11-11 name, plus any added later.

    Measured before the fix: ``set -uo pipefail; false; echo REACHED`` under an
    inherited ``-e`` printed nothing and exited 1, and so did the repo's exact
    ``(subshell); VAR=$?`` shape. Every recorded gate result on those paths was
    therefore decided by the first failure, not by the results.
    """
    problems = [
        f"{where}: {problem}"
        for where, script in _run_blocks()
        for problem in errexit_capture_problems(script)
    ]

    assert not problems, "workflow status captures that cannot mean what they say:\n  " + (
        "\n  ".join(problems)
    )


def test_at_least_one_workflow_block_is_actually_examined() -> None:
    """A guard that inspects nothing passes for the wrong reason."""
    examined = [where for where, script in _run_blocks() if "=$?" in script]

    assert len(examined) >= 8, examined


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        pytest.param(
            "set -uo pipefail\nmake thing; RC=$?\nexit $RC\n",
            "errexit still enabled",
            id="errexit_left_enabled_before_the_capture",
        ),
        pytest.param(
            "set -uo pipefail\nmake thing\nRC=$?\nexit $RC\n",
            "errexit still enabled",
            id="errexit_left_enabled_before_a_standalone_capture",
        ),
        pytest.param(
            "set -uo pipefail\nset +e\nmake thing; RC=$?\nset -e\nrecord --result ok\n",
            "never reads it",
            id="errexit_cleared_but_the_status_is_discarded",
        ),
        pytest.param(
            "set -uo pipefail\nset +e\nmake thing; RC=$?\nrecord --result $RC\n",
            "neither restores it nor exits",
            id="errexit_cleared_and_never_restored",
        ),
    ],
)
def test_the_errexit_guard_reports_a_deliberately_broken_block(script: str, expected: str) -> None:
    """The guard fed the exact shapes it exists to catch."""
    problems = errexit_capture_problems(script)

    assert problems, script
    assert any(expected in problem for problem in problems), problems


@pytest.mark.parametrize(
    "script",
    [
        "set -uo pipefail\nset +e\nmake thing; RC=$?\nset -e\nrecord --result $RC\n",
        "set -uo pipefail\nset +e\nmake thing; RC=$?\ncat report\nexit \"${RC}\"\n",
        'set +e\nmake a; A=$?\nmake b\nB=$?\n'
        'if [ "${A}" -ne 0 ] || [ "${B}" -ne 0 ]; then\nexit 1\nfi\n',
        "set -euo pipefail\nmake thing\nrecord --result ok\n",
    ],
)
def test_the_errexit_guard_accepts_the_correct_shapes(script: str) -> None:
    """A guard that rejected the working shapes would be unusable, not strict."""
    assert errexit_capture_problems(script) == []


# ---------------------------------------------------------------------------
# R5 -- the image digest comes from the daemon, not from the SBOM
# ---------------------------------------------------------------------------


def _release_langgraph_module(name: str):
    """Import a release module through its package path, with no ``sys.path`` edit."""
    import importlib

    return importlib.import_module(f"release.langgraph.{name}")


def _sbom_claiming(tmp_path: Path, reference: str, digest: str) -> Path:
    path = tmp_path / "image.spdx.json"
    path.write_text(
        json.dumps(
            {
                "packages": [
                    {
                        "name": reference,
                        "primaryPackagePurpose": "CONTAINER",
                        "versionInfo": digest,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def test_image_digest_producer_refuses_an_sbom_that_names_another_image(tmp_path: Path) -> None:
    """The producer stops copying the SBOM's claim and starts checking it.

    Before this, the application image's recorded digest *was* ``_spdx_digest``'s
    return value, so a forged SBOM decided what the release said it had built.
    """
    smoke = _release_langgraph_module("runtime_smoke")
    reference = "zeroth-core:v0.0.0"
    inspected = {"Id": "sha256:" + "a" * 64, "RepoDigests": []}

    honest = _sbom_claiming(tmp_path, reference, inspected["Id"])
    assert smoke._bound_application_digest(inspected, honest, reference) == inspected["Id"]

    forged = _sbom_claiming(tmp_path, reference, "sha256:" + "9" * 64)

    with pytest.raises(RuntimeError, match="does not describe the built image"):
        smoke._bound_application_digest(inspected, forged, reference)


def test_image_digest_producer_prefers_a_registry_digest_when_one_exists(tmp_path: Path) -> None:
    """A pushed image has a registry digest, and that is the supply-chain reference."""
    smoke = _release_langgraph_module("runtime_smoke")
    registry = "sha256:" + "b" * 64
    inspected = {"Id": "sha256:" + "a" * 64, "RepoDigests": [f"zeroth-core@{registry}"]}

    assert smoke._resolved_digest(inspected) == registry


def test_the_daemon_reports_the_fields_the_producer_reads() -> None:
    """`docker image inspect` really has `Id` and `RepoDigests`, on this machine.

    The producer's contract is with the daemon, not with a fixture, so one live
    call keeps the stubs above honest. Skipped where Docker is unavailable, which
    is the only reason a stubbed contract would ever drift unnoticed.
    """
    if shutil.which("docker") is None:  # pragma: no cover - environment dependent
        pytest.skip("docker is not installed")
    listing = subprocess.run(
        ["docker", "image", "ls", "--quiet"], check=False, capture_output=True, text=True
    )
    if listing.returncode != 0 or not listing.stdout.strip():  # pragma: no cover
        pytest.skip("no local image to inspect")

    smoke = _release_langgraph_module("runtime_smoke")
    inspected = smoke._inspect_image(listing.stdout.split()[0])

    assert re.fullmatch(r"sha256:[0-9a-f]{64}", str(inspected["Id"]))
    assert isinstance(inspected.get("RepoDigests", []), list)
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", smoke._resolved_digest(inspected))


# ---------------------------------------------------------------------------
# R7, R8 -- the suite's own vacuity guards
# ---------------------------------------------------------------------------

def _ruff_is_installed() -> bool:
    """Whether Ruff can run in this interpreter.

    Two CI jobs -- ``release-gates:package`` and ``release-zeroth-core:test-wheel`` --
    run the suite inside a pip venv built from the wheel, to test the *packaged
    product*. Ruff is a dev-group dependency and is deliberately not in that venv,
    so shelling out to it there fails with ModuleNotFoundError.

    Those jobs are not the lint gate. The lint gate is ``uv run ruff check src
    tests`` in ci.yml and release-gates.yml's ``source`` job, both of which run
    ``uv sync --all-groups`` and therefore have Ruff. Skipping where the tool is
    absent by design is honest; skipping where the gate runs would not be, which
    is what ``test_the_lint_gate_environment_really_has_ruff`` below refuses.
    """
    return (
        subprocess.run(
            [sys.executable, "-m", "ruff", "--version"],
            cwd=ROOT,
            check=False,
            capture_output=True,
        ).returncode
        == 0
    )


@pytest.mark.dev_toolchain
def test_the_lint_gate_environment_really_has_ruff() -> None:
    """Where the developer and the lint gate run, Ruff must be present.

    Without this the skip above could hide a broken lint gate: every Ruff-backed
    check would quietly vanish and the suite would still be green. ``uv sync
    --all-groups`` installs the dev group, so a failure here means the gate's own
    environment is wrong.

    Marked ``dev_toolchain``, because the two wheel-venv jobs are the one place
    where its own message -- "if this is the wheel-venv job that is expected" --
    is true, and an assertion that excuses itself in prose still fails. Landing
    unmarked in ZER-41, it would have failed the next nightly for exactly the
    reason this ticket exists to remove: absent dev tooling reported as a defect.
    No nightly has recorded it, because run 31469899049 tested 7249ca04, one
    commit before ZER-41 merged.

    The marker does not weaken it. ``tests/release_gates/test_marker_integrity.py``
    proves it still runs wherever the project default applies, which is both
    places the lint gate actually runs: ``ci.yml:lint`` and
    ``release-gates.yml:source``.
    """
    assert _ruff_is_installed(), (
        "Ruff is not importable, so every lint-enforcement check would skip. If this "
        "is the wheel-venv job that is expected; if it is the lint gate, the dev "
        "dependency group was not installed."
    )


#: A minimal file that violates each rule this task stopped exempting.
#:
#: The guard runs Ruff over these and asserts the violation is *reported*. It
#: reads no configuration at all, and that is the point.
#:
#: Three earlier attempts each checked a representation of the exemption instead
#: of its effect, and each was defeated by a different representation of the same
#: thing: a tuple of "rules we enforce" (delete the rule, delete its check); the
#: difference from the base exemption list (the base list still contained the
#: restored rule); a frozen historical reference (still reading one config key,
#: while ``lint.extend-per-file-ignores`` restores the rule invisibly -- measured:
#: an F841 probe went from exit 1 to exit 0 with every recorded set unchanged).
#:
#: The root cause is the same in all three: a config key is a *claim* about what
#: Ruff will do. Observing what Ruff actually does closes every path that can
#: change the answer, including the ones nobody enumerated.
RULE_PROBES = {
    "F841": "def probe():\n    unused = 1\n",
    "B017": (
        "import pytest\n\n\ndef test_probe():\n"
        "    with pytest.raises(Exception):\n        pass\n"
    ),
}


def rule_is_reported(rule: str, source: str, *, filename: str = "tests/_probe.py") -> bool:
    """Whether Ruff reports ``rule`` for ``source`` at ``filename``, under this project.

    ``--stdin-filename`` makes the per-file patterns apply without writing to the
    tree, so the probe is subject to exactly the configuration a real test file
    would be.

    **No ``--select``.** Passing one overrides the configured ``ignore``, which
    means the probe would answer "would Ruff report this if asked specifically",
    not "does the project's lint report this" -- and a global
    ``lint.ignore = ["F841"]`` slipped straight through while the probe stayed
    green. Measured. The gate command is ``ruff check src tests`` with no
    selection, so the probe runs the same way and looks for the code in the
    output.
    """
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--stdin-filename", filename,
         "--output-format", "concise", "--no-cache", "-"],
        cwd=ROOT,
        input=source,
        check=False,
        capture_output=True,
        text=True,
    )
    # Match the code by its position -- `path:line:col: CODE message` -- not by
    # splitting on ":". B017's own message contains a colon, so a naive split
    # reported it as absent while Ruff was flagging it perfectly well.
    reported = re.compile(rf":\d+:\d+:\s+{re.escape(rule)}\b")
    return any(reported.search(line) for line in result.stdout.splitlines())


#: NOTE on how ``RULE_PROBES`` above is protected.
#:
#: An earlier revision derived that set by reading the tests/** exemption list at the two
#: commits bracketing this work, so that deleting an entry could not silently drop a rule's
#: enforcement. The anchor did not survive: the branch was squashed before merge, one of
#: those commits now exists on no ref, and the check skipped in CI permanently -- a guard
#: reporting nothing, the very defect this module exists to remove. Any git-SHA anchor
#: meets the same end in a repository that squash-merges.
#:
#: So the arrangement is stated plainly instead. The *guard* is
#: ``test_every_enforced_rule_is_in_force_at_every_test_path``: it reads no history, never
#: skips, and checks both rules at all 515 test paths. ``RULE_PROBES`` only names which
#: rules that sweep covers, and shortening it is a visible diff -- the same protection
#: every ratchet file in every repository actually relies on.

#: One source violating every enforced rule at once, so the exhaustive sweep
#: below costs one Ruff invocation per path rather than one per (path, rule).
COMBINED_PROBE = (
    "import pytest\n\n\ndef test_probe():\n    unused = 1\n"
    "    with pytest.raises(Exception):\n        pass\n"
)


def _reported_rules(source: str, filename: str) -> set[str]:
    """Every enforced rule Ruff reports for ``source`` at ``filename``."""
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--stdin-filename", filename,
         "--output-format", "concise", "--no-cache", "-"],
        cwd=ROOT,
        input=source,
        check=False,
        capture_output=True,
        text=True,
    )
    return {
        rule
        for rule in RULE_PROBES
        if re.search(rf":\d+:\d+:\s+{re.escape(rule)}\b", result.stdout)
    }


@pytest.mark.parametrize("rule", sorted(RULE_PROBES))
def test_the_rules_this_task_enforced_are_reported_on_a_real_violation(rule: str) -> None:
    """The property itself: a violating test file is flagged.

    Not "the exemption list does not contain the rule" -- that is a claim about
    configuration, and there is more than one way to write the configuration.
    """
    if not _ruff_is_installed():  # pragma: no cover - wheel-venv job
        pytest.skip("Ruff is not in this environment; see _ruff_is_installed")

    assert rule_is_reported(rule, RULE_PROBES[rule]), (
        f"{rule} is not reported for a file under tests/; something in the Ruff "
        "configuration is exempting it again"
    )


def test_every_enforced_rule_is_in_force_at_every_test_path() -> None:
    """Exhaustive, because the set of paths is finite and enumerable.

    A single representative filename is not the property. A ``per-file-ignores``
    entry naming one exact path suppresses the rules there while every probe at
    another path stays green and the file still appears in ``--show-files``;
    measured, and it is why this sweep exists rather than a scoped-out residual.

    The set of test files is enumerable, so the check is too. One Ruff call per
    path with a source violating every enforced rule at once -- about 12 s
    serially over 515 files, a few seconds across a pool.
    """
    if not _ruff_is_installed():  # pragma: no cover - wheel-venv job
        pytest.skip("Ruff is not in this environment; see _ruff_is_installed")

    from concurrent.futures import ThreadPoolExecutor

    paths = sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "tests").rglob("*.py")
        if "__pycache__" not in path.parts
    )

    assert paths, "no test files found -- the sweep would be vacuous"

    expected = set(RULE_PROBES)
    with ThreadPoolExecutor(max_workers=8) as pool:
        observed = list(pool.map(lambda path: _reported_rules(COMBINED_PROBE, path), paths))

    suppressed = {
        path: sorted(expected - reported)
        for path, reported in zip(paths, observed, strict=True)
        if reported != expected
    }

    assert suppressed == {}, (
        "these test paths do not have every enforced rule in force: "
        f"{dict(sorted(suppressed.items())[:10])}"
    )


@pytest.mark.parametrize("rule", sorted(RULE_PROBES))
def test_the_probe_really_violates_the_rule_it_names(rule: str) -> None:
    """A probe that violated nothing would make the test above pass for free.

    Checked against a filename outside ``tests/``, where no per-file exemption has
    ever applied, so a reported violation there proves the source is genuinely
    offending rather than the rule being globally off.
    """
    if not _ruff_is_installed():  # pragma: no cover - wheel-venv job
        pytest.skip("Ruff is not in this environment; see _ruff_is_installed")

    assert rule_is_reported(rule, RULE_PROBES[rule], filename="scratch/_probe.py")


@pytest.mark.parametrize("rule", sorted(RULE_PROBES))
def test_a_clean_file_is_not_reported(rule: str) -> None:
    """And the detector is not simply always positive."""
    if not _ruff_is_installed():  # pragma: no cover - wheel-venv job
        pytest.skip("Ruff is not in this environment; see _ruff_is_installed")

    assert not rule_is_reported(rule, "def probe():\n    return 1\n")


def test_ruff_actually_checks_every_test_file_on_disk() -> None:
    """A file Ruff never opens is exempt from every rule at once.

    The probe above proves the rules are in effect *at a path*. This proves the
    set of paths is the whole tree: an ``extend-exclude`` or a narrowed source
    selection silences a file without touching any rule, and no per-rule probe
    can see that.

    Path-scoped *rule* suppression is a different hole and is closed separately,
    by ``test_every_enforced_rule_is_in_force_at_every_test_path``. An earlier
    revision of this docstring called that residual unprovable; it is not, and
    saying so here would be the kind of stale claim this module exists to catch.
    """
    if not _ruff_is_installed():  # pragma: no cover - wheel-venv job
        pytest.skip("Ruff is not in this environment; see _ruff_is_installed")

    listed = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "tests", "--show-files"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert listed.returncode == 0, listed.stderr
    checked = {Path(line).resolve() for line in listed.stdout.split() if line.strip()}
    on_disk = {
        path.resolve()
        for path in (ROOT / "tests").rglob("*.py")
        if "__pycache__" not in path.parts
    }
    unchecked = sorted(str(path.relative_to(ROOT)) for path in on_disk - checked)

    assert unchecked == [], f"Ruff does not check these test files at all: {unchecked}"


def test_the_tests_tree_itself_is_clean_under_every_enforced_rule() -> None:
    """Green on landing, which the ticket requires of any enforcement it adds."""
    if not _ruff_is_installed():  # pragma: no cover - wheel-venv job
        pytest.skip("Ruff is not in this environment; see _ruff_is_installed")

    for rule in sorted(RULE_PROBES):
        result = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "tests", "--select", rule, "--quiet"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"{rule}: {result.stdout}"


def test_vacuity_guard_rejects_a_constant_assertion() -> None:
    """``assert True`` cannot fail, and the guard that exists to say so did not.

    Measured: the guard reported ``1 passed`` while ``assert True`` was live in
    ``tests/test_docs_phase30.py``. It implemented three rules -- empty
    parametrization, self-comparison, repeated sibling assertion -- and none
    inspects ``ast.Constant``.
    """
    import ast

    from tests.architecture.test_legacy_surface_removed import _constant_assertion

    def parsed(source: str) -> ast.stmt:
        return ast.parse(source).body[0]

    assert _constant_assertion(parsed("assert True"))
    assert _constant_assertion(parsed("assert 1"))
    assert _constant_assertion(parsed('assert "a reason"'))
    # `assert False` always fails, which is a deliberate unreachable marker
    # rather than vacuity, so it is deliberately not reported.
    assert not _constant_assertion(parsed("assert False"))
    assert not _constant_assertion(parsed("assert value"))
    assert not _constant_assertion(parsed("assert compute() == 3"))


def test_no_constant_assertion_survives_in_the_tests_tree() -> None:
    """The guard runs green because the tree is clean, not because it is blind."""
    import ast

    from tests.architecture.test_legacy_surface_removed import _constant_assertion

    offenders = [
        f"{path.relative_to(ROOT).as_posix()}:{node.lineno}"
        for path in sorted((ROOT / "tests").rglob("*.py"))
        if "__pycache__" not in path.parts
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if _constant_assertion(node)
    ]

    assert offenders == []


# ---------------------------------------------------------------------------
# R10 -- the guard suite covers TypeScript
# ---------------------------------------------------------------------------

FRONTEND = ROOT / "frontend"


def _vitest_include_globs() -> list[str]:
    """The ``test.include`` globs declared in ``frontend/vitest.config.ts``.

    Read with a regex rather than by running node: the guard has to work in a
    Python-only CI job, and the failure it exists to catch is a *declaration*
    changing, which is visible in the source.
    """
    source = (FRONTEND / "vitest.config.ts").read_text(encoding="utf-8")
    block = re.search(r"include:\s*\[(.*?)\]", source, re.DOTALL)
    assert block, "vitest.config.ts declares no test.include"
    return re.findall(r"[\"']([^\"']+)[\"']", block.group(1))


def _discovered_typescript_tests(root: Path = FRONTEND) -> list[Path]:
    return sorted(
        path
        for pattern in ("*.test.ts", "*.test.tsx")
        for path in (root / "app").rglob(pattern)
        if "node_modules" not in path.parts
    )


def uncollected_typescript_tests(root: Path, globs: list[str]) -> list[str]:
    """TypeScript test files present in ``root`` that ``globs`` would not collect.

    Expanding each glob against the tree, rather than matching each path against
    each glob: ``PurePath.match`` does not let ``**`` span directories before
    Python 3.13, so a nested test would look unmatched when vitest collects it.
    """
    collected = {path for glob in globs for path in root.glob(glob)}
    return sorted(
        path.relative_to(root).as_posix()
        for path in _discovered_typescript_tests(root)
        if path not in collected
    )


def test_every_typescript_test_file_is_matched_by_the_vitest_include() -> None:
    """A console test the runner never collects is a test that cannot fail.

    This is the cross-owner coupling the ticket names: ZER-53's frontend test
    work lands into this config, and an ``include`` that stops matching -- a
    ``.test.tsx`` component test against a ``.test.ts``-only glob, say -- would
    silently reduce coverage while every gate stayed green.
    """
    globs = _vitest_include_globs()

    assert _discovered_typescript_tests(), "no TypeScript tests -- the guard would be vacuous"
    assert uncollected_typescript_tests(FRONTEND, globs) == [], globs


@pytest.mark.parametrize(
    ("globs", "expected"),
    [
        pytest.param(
            ["app/**/*.test.ts"],
            ["app/components/Panel.test.tsx"],
            id="a_component_test_against_a_ts_only_glob",
        ),
        pytest.param(
            ["app/lib/*.test.ts"],
            ["app/components/Panel.test.tsx", "app/lib/deep/nested.test.ts"],
            id="a_glob_narrowed_to_one_directory",
        ),
        pytest.param([], ["app/components/Panel.test.tsx", "app/lib/deep/nested.test.ts",
                          "app/lib/plain.test.ts"], id="include_emptied_entirely"),
    ],
)
def test_the_typescript_guard_reports_a_config_that_stops_collecting(
    tmp_path: Path, globs: list[str], expected: list[str]
) -> None:
    """The guard fed the shapes a frontend config change would take."""
    for relative in ("app/lib/plain.test.ts", "app/lib/deep/nested.test.ts",
                     "app/components/Panel.test.tsx"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    assert uncollected_typescript_tests(tmp_path, globs) == expected


def test_the_typescript_guard_accepts_a_config_that_collects_everything(tmp_path: Path) -> None:
    """A guard that reported every config would be noise, not a check."""
    for relative in ("app/lib/plain.test.ts", "app/components/Panel.test.tsx"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    assert uncollected_typescript_tests(tmp_path, ["app/**/*.test.ts", "app/**/*.test.tsx"]) == []


def test_the_typescript_suite_is_recorded_as_a_release_gate_result() -> None:
    """Running the console tests is not enough; the outcome has to be recorded."""
    script = "\n".join(
        str(step.get("run", ""))
        for job in _workflow("release-gates.yml")["jobs"].values()
        for step in job.get("steps") or []
    )

    assert "vitest run" in script
    assert "frontend-unit=$(status ${VITEST})" in script


def test_a_failed_console_install_is_recorded_as_a_failed_console_suite() -> None:
    """``npm ci`` failing means the suite never ran, which is not a pass.

    Without this the console result would be whatever the uninitialised or
    stale variable held, which is the same silence in a different place.
    """
    script = "\n".join(
        str(step.get("run", ""))
        for job in _workflow("release-gates.yml")["jobs"].values()
        for step in job.get("steps") or []
    )

    assert 'if [ "${NPM_CI}" -ne 0 ]; then VITEST=1; fi' in script


# ---------------------------------------------------------------------------
# R14, R17 -- exemptions applied in code, recorded nowhere
# ---------------------------------------------------------------------------


def test_unsupported_operations_are_declared_rather_than_inferred() -> None:
    """A superset assertion is blind in one direction, so the difference is named.

    ``projected >= claimed`` catches a claim the upstream cannot honour. It
    cannot catch an operation the gateway silently stops implementing: dropping
    a claim only makes the claimed set smaller, which still satisfies the
    relation. Before ZER-41 there was no ``KNOWN_UNSUPPORTED`` sibling anywhere.
    """
    pytest.importorskip("langgraph", reason="requires the gateway-conformance group")
    from tests.langgraph_gateway.conformance.cases import (
        CLAIMED_OPERATIONS,
        KNOWN_UNSUPPORTED_OPERATIONS,
    )

    projection = json.loads(
        (ROOT / "tests/langgraph_gateway/fixtures/openapi-0.11.1.operations.json").read_text(
            encoding="utf-8"
        )
    )
    projected = {(method, path) for method, path, _ in projection["operations"]}
    projected.add(("GET", "/openapi.json"))

    assert KNOWN_UNSUPPORTED_OPERATIONS
    assert projected - CLAIMED_OPERATIONS == KNOWN_UNSUPPORTED_OPERATIONS
    # The two sets partition the projection: nothing is both claimed and declared
    # unsupported, and nothing falls outside both.
    assert not (CLAIMED_OPERATIONS & KNOWN_UNSUPPORTED_OPERATIONS)
    assert projected == CLAIMED_OPERATIONS | KNOWN_UNSUPPORTED_OPERATIONS


def test_dropping_a_claimed_operation_is_visible_to_the_exact_assertion() -> None:
    """The shape the superset relation could not see, exercised directly."""
    pytest.importorskip("langgraph", reason="requires the gateway-conformance group")
    from tests.langgraph_gateway.conformance.cases import (
        CLAIMED_OPERATIONS,
        KNOWN_UNSUPPORTED_OPERATIONS,
    )

    projected = CLAIMED_OPERATIONS | KNOWN_UNSUPPORTED_OPERATIONS
    dropped = sorted(CLAIMED_OPERATIONS)[0]
    reduced = CLAIMED_OPERATIONS - {dropped}

    # A superset assertion still holds after the drop -- which is the defect.
    assert projected >= reduced
    # The exact assertion does not.
    assert projected - reduced != KNOWN_UNSUPPORTED_OPERATIONS


def test_every_hidden_constructor_field_is_recorded() -> None:
    """Ten classes hide fields from the protected-surface gate; all are written down.

    ``PolicyDefinition`` reports eight constructor parameters and carries fifteen
    fields. The idiom is deliberate -- the canonical surface fixture pins
    signatures -- but nothing recorded the total, so the gate reported the surface
    as pinned while the constructor had grown.
    """
    import importlib

    from tests.contracts.test_signature_exclusions import (
        HIDDEN_CONSTRUCTOR_FIELDS,
        hidden_fields,
    )

    assert HIDDEN_CONSTRUCTOR_FIELDS
    for reference, recorded in HIDDEN_CONSTRUCTOR_FIELDS.items():
        module, _, name = reference.partition(":")
        target = getattr(importlib.import_module(module), name)
        assert hidden_fields(target) == set(recorded), reference


# ---------------------------------------------------------------------------
# R16 -- what the pull-request path does not cover, stated rather than assumed
# ---------------------------------------------------------------------------

#: Evidence classes that no pull-request workflow produces, and why.
#:
#: Every `docker` invocation in the repository lives in `release-zeroth-core.yml`,
#: gated on `release: [published]`; `release-gates.yml` is nightly cron plus
#: `workflow_dispatch` and deliberately has no `pull_request` trigger. So a
#: change to the Dockerfile, the compose file, or the hardening flags reaches a
#: real container for the first time at release. That is a deliberate trade --
#: a container build on every PR is slow and needs a daemon -- but it was
#: nowhere written down, so it read as coverage rather than as a gap.
#:
#: **This record may only shrink.** Moving one of these onto the PR path means
#: deleting its line (ZER-41 / A14-13).
NOT_COVERED_ON_PULL_REQUESTS = {
    "docker": "no PR workflow builds or runs a container; every docker step is release-gated",
}


def _pull_request_workflows() -> list[str]:
    """Workflow files with a ``pull_request`` trigger.

    ``on`` is parsed as the boolean ``True`` by PyYAML's 1.1 rules, so both keys
    are read; the trigger block is a mapping in every workflow here but a list is
    also valid YAML for it.
    """
    names = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        document = _workflow(path.name)
        triggers = document.get(True) or document.get("on") or {}
        if isinstance(triggers, dict | list) and "pull_request" in triggers:
            names.append(path.name)
    return names


def observed_pull_request_container_use() -> list[str]:
    """Where a pull-request workflow reaches a container, if anywhere.

    Both shapes count. A ``run:`` line invoking the CLI is the obvious one; a
    ``uses:`` step such as ``docker/build-push-action`` builds an image without
    the word ever appearing in a script, and reading only ``run:`` would have
    missed it entirely.

    Comment lines are stripped: ``verify-extras.yml`` explains in prose why its
    gate stops short of the Docker CLI, and a substring search over the raw
    script reads that explanation as an invocation.
    """
    found: list[str] = []
    for name in _pull_request_workflows():
        for job_name, job in _workflow(name)["jobs"].items():
            if job.get("container") or (job.get("services") or {}):
                found.append(f"{name}:{job_name}: job-level container/services")
            for step in job.get("steps") or []:
                action = str(step.get("uses", ""))
                if re.search(r"docker|buildx|build-push", action, re.IGNORECASE):
                    found.append(f"{name}:{job_name}: uses {action}")
                for line in str(step.get("run", "")).splitlines():
                    if not line.strip().startswith("#") and re.search(r"\bdocker\b", line):
                        found.append(f"{name}:{job_name}: {line.strip()}")
    return found


def test_the_pull_request_container_gap_is_declared_and_still_real() -> None:
    """The record must equal what is observed -- in both directions.

    Exact comparison, not "the record is non-empty and nothing was found". The
    weaker form accepts an invented exclusion, and it also refuses the one
    outcome the record exists to reach: an empty record, meaning the gap was
    closed. Deriving the observed set and comparing exactly permits the shrink to
    empty and rejects padding.
    """
    observed = observed_pull_request_container_use()
    declared = set(NOT_COVERED_ON_PULL_REQUESTS)

    if observed:
        assert declared == set(), (
            "a pull-request workflow now reaches a container, so the gap is closed; "
            f"empty NOT_COVERED_ON_PULL_REQUESTS. Found: {observed}"
        )
    else:
        assert declared == {"docker"}, (
            "no pull-request workflow reaches a container, so the record must say "
            f"exactly that and nothing more; got {sorted(declared)}"
        )


def test_the_container_detector_sees_an_action_not_only_a_shell_line() -> None:
    """The shape a run-line scan cannot see, exercised directly."""
    workflow = {
        "on": {"pull_request": {}},
        "jobs": {"build": {"steps": [{"uses": "docker/build-push-action@v6"}]}},
    }
    steps = workflow["jobs"]["build"]["steps"]

    assert any(re.search(r"docker|buildx|build-push", str(s.get("uses", "")), re.IGNORECASE)
               for s in steps)
    assert not any(str(s.get("run", "")) for s in steps)


def test_the_container_evidence_really_is_release_gated() -> None:
    """The other half of the same claim: the coverage exists, just not on PRs."""
    release = _workflow("release-zeroth-core.yml")
    triggers = release.get(True) or release.get("on") or {}
    scripts = "\n".join(
        str(step.get("run", ""))
        for job in release["jobs"].values()
        for step in job.get("steps") or []
    )

    assert "release" in triggers
    assert "docker build" in scripts


# ---------------------------------------------------------------------------
# R18 -- two findings whose stated consequence rests on a flag nobody passes
# ---------------------------------------------------------------------------


def test_no_configured_pytest_invocation_passes_dash_x() -> None:
    """A10-14 and A10-15 both rest on `-x`, and nothing sets it.

    Each finding's primary consequence was "under ``-x`` this masks the tests
    that never ran". That is false as configured: ``pyproject.toml``'s addopts
    carries no ``-x`` and no workflow passes one, so a failure stops one test and
    the rest still run. The measured halves -- a 1.1s wall-clock sleep against a
    1s window, and six independent expensive setups -- are real; the stated
    consequence is not, and this pins the fact the rebuttal rests on.
    """
    import tomllib

    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    addopts = config["tool"]["pytest"]["ini_options"]["addopts"]

    assert " -x" not in f" {addopts}"
    assert "--exitfirst" not in addopts

    invocations = [
        f"{path.name}: {command}"
        for path in sorted(WORKFLOWS.glob("*.yml"))
        for job in (_workflow(path.name).get("jobs") or {}).values()
        for step in job.get("steps") or []
        for command in joined_commands(str(step.get("run", "")))
        if "pytest" in command
    ]

    assert invocations, "no pytest invocation found -- the assertion would be vacuous"
    for command in invocations:
        assert not exits_first(command), command


def joined_commands(script: str) -> list[str]:
    """A shell script's logical commands, with backslash continuations joined.

    Reading raw lines misses the ordinary shape

        uv run pytest -q \\
          -x --junitxml=...

    where the flag lands on the next physical line. The workflows use exactly
    this continuation style for every long invocation, so a line-wise scan is
    blind precisely where it matters.
    """
    commands: list[str] = []
    pending = ""
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            pending += stripped[:-1].strip() + " "
            continue
        commands.append((pending + stripped).strip())
        pending = ""
    if pending:
        commands.append(pending.strip())
    return [command for command in commands if command]


def exits_first(command: str) -> bool:
    """Whether a command passes pytest's exit-on-first-failure flag.

    Tokenised, so `-x` is not confused with `--exitfirst=`-lookalikes or with an
    `-x` embedded in a path, and grouped short flags like `-qx` are still caught.
    """
    import shlex

    try:
        tokens = shlex.split(command)
    except ValueError:  # pragma: no cover - unbalanced quotes in a script
        tokens = command.split()
    for token in tokens:
        if token == "--exitfirst" or token.startswith("--exitfirst="):
            return True
        if token.startswith("-") and not token.startswith("--") and "x" in token[1:]:
            return True
    return False


@pytest.mark.parametrize(
    "script",
    [
        "uv run pytest -q -x\n",
        "uv run pytest -q \\\n  -x --junitxml=out.xml\n",
        "uv run pytest \\\n  --exitfirst \\\n  -q\n",
        "uv run pytest -qx\n",
    ],
)
def test_the_dash_x_detector_sees_a_multiline_invocation(script: str) -> None:
    """The shape a line-wise scan misses, in the four spellings it can take."""
    assert any(exits_first(command) for command in joined_commands(script)), script


@pytest.mark.parametrize(
    "script",
    [
        "uv run pytest -q --no-header -ra\n",
        "uv run pytest -q \\\n  --junitxml=release/evidence/source-junit.xml\n",
        "uv run pytest -q -m 'not live' --maxfail=3\n",
    ],
)
def test_the_dash_x_detector_accepts_the_invocations_actually_used(script: str) -> None:
    """A detector that flagged every command would be noise, not a check."""
    assert not any(exits_first(command) for command in joined_commands(script)), script


def test_the_rate_limit_window_test_no_longer_sleeps_through_its_window() -> None:
    """A10-14's measured half: a 1.1s wall-clock sleep against a 1s window.

    The 100ms margin had to cover the whole remaining call, and buying it with a
    real sleep proved nothing extra -- what is under test is that a window
    boundary is *observed*. The clock is advanced instead, and the test got
    stronger for it: it now pins both directions of the boundary rather than only
    "eventually allowed".
    """
    source = (ROOT / "tests/guardrails/test_rate_limit.py").read_text(encoding="utf-8")
    body = source.split("async def test_quota_enforcer_resets_after_window")[1].split(
        "\nasync def "
    )[0]

    assert "time.sleep" not in body
    assert "monkeypatch.setattr" in body


def test_the_six_observable_surface_tests_keep_independent_setups() -> None:
    """A10-15's measured half is true, and its implied remedy is worse.

    The helper really is called six times, and each call spawns two subprocesses,
    deploys a service and drives a TestClient: measured at ~2.7s of capture across
    the module, of which ~2.2s is repetition.

    Sharing one capture would save that and *increase* the exposure the finding
    itself names -- "six chances for the shared setup to fail for an unrelated
    reason". One shared setup failing takes out all six surfaces at once and
    reports one error instead of naming which surface leaked. Six independent
    setups are what keep each surface independently diagnosable, which is worth
    2.2 seconds. So the count is recorded rather than collapsed.
    """
    import ast

    path = ROOT / "tests/security/test_observable_surfaces.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    surface_tests = [
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name.startswith("test_credential_canary_absent_from_")
    ]

    assert len(surface_tests) == 6
    for test in surface_tests:
        calls = [
            node
            for node in ast.walk(test)
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", "") == "_capture_observable_surfaces"
        ]
        assert len(calls) == 1, test.name
