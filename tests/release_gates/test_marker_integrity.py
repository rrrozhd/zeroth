"""A deselected test must still run somewhere, and a job may not widen the default.

Two wheel-venv jobs -- ``release-gates.yml:package`` and
``release-zeroth-core.yml:test-wheel`` -- install the built wheel into a clean
venv and run the suite there. They answer one question: *does the built wheel
work?* Nightly, they answered a different one.

Measured on run 31469899049, job 93710803074: **25 failed, 96 errors**, and only
16 deselected against the source gate's 465.

* 84 errors were LangGraph Agent Server startup failures. ``-m "not live"`` on the
  command line **replaces** ``addopts``'s marker expression rather than extending
  it, so the job re-selected the ``langgraph_conformance`` and
  ``deployed_acceptance`` suites that the project deselects by default -- and a
  wheel venv has no Agent Server to start.
* 12 errors were ``FileNotFoundError: uv``: the only gate job that did not install
  it, running the one test file that inspects what the wheel actually ships.
* 23 failures were ``ModuleNotFoundError: mkdocs`` -- a ``[docs]`` extra, absent
  from a wheel venv by design.

The first two are fixed in the workflows. The third needs a marker, and a marker
that removes a test from a job is a place to hide a test. So both properties are
asserted here by *executing* the selections rather than by reading them:

1. Every ``dev_toolchain`` test is selected by the project's own default, so the
   marker moves a test between jobs and never out of the suite. A string scan of
   the workflows cannot see this -- adding ``and not dev_toolchain`` to
   ``addopts`` would deselect the marker everywhere while every workflow file
   still matched.
2. No whole-suite invocation admits anything the project default excludes. That
   is the defect above, stated as a property of the expression rather than of its
   spelling.
"""

from __future__ import annotations

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
import yaml

from .conftest import ROOT

WORKFLOWS = ROOT / ".github/workflows"

#: Every test deselected in a wheel-only venv, and why.
#:
#: Named one by one rather than by file. Both files below also contain tests that
#: run perfectly well in a wheel venv -- ``test_gate_integrity.py`` has one marked
#: test out of dozens -- so a file-granular record cannot see a test being added
#: to a file it already lists. That is the same shape as the defeated guards this
#: repository keeps rediscovering: a record that is technically satisfied while
#: the thing it records has grown.
#:
#: **This record may only shrink.** Removing a line means the test runs in the
#: wheel jobs again; adding one is a visible diff that has to be defended. It is
#: compared for equality with what is actually marked, so it can be neither
#: padded with fiction nor silently outgrown.
DEV_TOOLCHAIN_EXCLUSIONS = {
    # mkdocs is a [docs] extra, deliberately absent from a venv built from the
    # wheel. Exactly the tests that reach `on_post_page`'s function-local
    # `from mkdocs.utils import get_relative_url`, plus the guard that refuses to
    # let its absence go unnoticed where the docs actually build.
    "tests/scripts/test_openapi_generation.py::test_the_docs_gate_environment_really_has_mkdocs": (
        "asserts mkdocs is importable where the docs build and the source gate run"
    ),
    "tests/scripts/test_openapi_generation.py::test_docs_hook_substitutes_the_spec_url_for_both_url_modes": "drives on_post_page",  # noqa: E501
    "tests/scripts/test_openapi_generation.py::test_docs_hook_fails_the_build_when_the_spec_page_loses_its_token": "drives on_post_page",  # noqa: E501
    "tests/scripts/test_openapi_generation.py::test_docs_hook_accepts_the_spellings_a_contributor_may_write": "drives on_post_page",  # noqa: E501
    "tests/scripts/test_openapi_generation.py::test_docs_hook_is_not_confused_by_apostrophes_in_prose": "drives on_post_page",  # noqa: E501
    "tests/scripts/test_openapi_generation.py::test_docs_hook_rejects_a_token_that_is_present_but_not_bound": "drives on_post_page",  # noqa: E501
    "tests/scripts/test_openapi_generation.py::test_docs_hook_requires_the_viewer_call_itself": "drives on_post_page",  # noqa: E501
    "tests/scripts/test_openapi_generation.py::test_docs_hook_tolerates_reformatting_around_the_viewer_call": "drives on_post_page",  # noqa: E501
    "tests/scripts/test_openapi_generation.py::test_docs_hook_leaves_other_pages_alone": "drives on_post_page",  # noqa: E501
    # Ruff is a dev-group dependency, equally absent by design.
    "tests/release_gates/test_gate_integrity.py::test_the_lint_gate_environment_really_has_ruff": (
        "asserts the dev group's Ruff is importable -- true wherever the lint gate "
        "runs, false in a wheel venv by design, and its own failure message said so"
    ),
    # langgraph_api is a gateway-conformance dev-group pin, in the same position.
    "tests/acceptance/test_ephemeral_candidate.py::test_the_product_contract_runs_against_the_ephemeral_candidate": (  # noqa: E501
        "boots the real langgraph_api Agent Server; the other five tests in that "
        "module do not, and stay in the wheel jobs"
    ),
}

#: Jobs permitted to deselect ``dev_toolchain``, and why. Also shrink-only, and
#: also compared for equality: a third job appearing here is a visible diff.
WHEEL_VENV_JOBS = {
    "release-gates.yml:package": "runs the suite inside a venv built from the wheel",
    "release-zeroth-core.yml:test-wheel": "runs the suite inside a venv built from the wheel",
}


# ---------------------------------------------------------------------------
# Executed collection
# ---------------------------------------------------------------------------


def collect(*arguments: str) -> set[str]:
    """Node ids pytest really collects for ``arguments``, from a fresh interpreter.

    A subprocess, not an in-process ``pytest.main``: the point is to observe the
    selection a *job* gets, including the ``addopts`` this session was itself
    started with, and an in-process run inherits state from the current one.
    """
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "--no-header",
         "-p", "no:cacheprovider", *arguments],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 5):  # 5 == no tests collected, a valid answer
        raise AssertionError(
            f"collection failed for {arguments}:\n{result.stdout}\n{result.stderr}"
        )
    return {line.strip() for line in result.stdout.splitlines() if "::" in line}


def _wheel_expression() -> str:
    """The marker expression the wheel-venv jobs pass, read from the workflows.

    Not written out a second time here: a copy would let this module agree with
    itself while disagreeing with what CI actually runs, which is the shape of
    every gate defect this suite exists to catch.
    """
    expressions = {
        expression
        for where, _, expression in _whole_suite_invocations()
        if where in WHEEL_VENV_JOBS and expression
    }
    assert len(expressions) == 1, (
        f"the wheel-venv jobs must pass one agreed marker expression; got {expressions}"
    )
    return expressions.pop()


class _Selections:
    """The three tree-wide collections this module needs, gathered once, in parallel.

    Each costs ~14 s because collection imports the tree, so they are run
    concurrently as subprocesses and cached for the module.
    """

    def __init__(self) -> None:
        with ThreadPoolExecutor(max_workers=3) as pool:
            default, wheel, marked = pool.map(
                lambda arguments: collect(*arguments),
                (
                    ("tests/",),
                    ("-o", "addopts=", "-m", _wheel_expression(), "tests/"),
                    ("-o", "addopts=", "-m", "dev_toolchain", "tests/"),
                ),
            )
        #: What a developer and the source gate run: the project's own default.
        self.default = default
        #: What the wheel-venv jobs run.
        self.wheel = wheel
        #: Everything carrying the marker, found without any default filtering.
        self.marked = marked


@pytest.fixture(scope="module")
def selections() -> _Selections:
    return _Selections()


def _without_parametrization(node_ids: set[str]) -> set[str]:
    """``file::function`` for each node id, with the parametrized case dropped.

    Recording every parametrized id would make the record churn on an added case
    that changes nothing about which tooling is needed; recording only the file
    would let a whole test be added to a listed file unseen.
    """
    return {node_id.split("[", 1)[0] for node_id in node_ids}


# ---------------------------------------------------------------------------
# The marker is not a hiding place
# ---------------------------------------------------------------------------


def test_the_dev_toolchain_marker_is_not_vacuous(selections: _Selections) -> None:
    """A marker nothing carries would make every check below pass for free."""
    assert selections.marked, (
        "no test carries dev_toolchain, so the guards in this module verify nothing"
    )


def test_every_dev_toolchain_test_is_selected_by_the_project_default(
    selections: _Selections,
) -> None:
    """The property: deselecting in one job moves a test, it does not retire it.

    Executed against the real ``addopts``. The representation-level version of
    this check -- scanning the workflow files for a job that does not deselect
    the marker -- is defeated in one move by adding ``and not dev_toolchain`` to
    ``addopts``, which deselects it in *every* job while every workflow file
    still reads correctly.
    """
    orphaned = sorted(selections.marked - selections.default)

    assert orphaned == [], (
        "these dev_toolchain tests are not selected by the project default, so no "
        f"job runs them at all: {orphaned}"
    )


def test_the_wheel_jobs_really_do_deselect_the_marker(selections: _Selections) -> None:
    """And the other direction: the deselection is real, not merely declared.

    Without this the marker could be applied, recorded, and have no effect, which
    would leave the wheel jobs failing on absent tooling exactly as before while
    every guard here reported success.
    """
    assert selections.marked & selections.wheel == set(), sorted(
        selections.marked & selections.wheel
    )


def test_the_recorded_exclusion_matches_what_is_actually_marked(
    selections: _Selections,
) -> None:
    """Equality in both directions -- the record can shrink but not drift.

    A superset assertion would accept an invented entry; a subset assertion would
    accept a test marked and never written down. The exclusion has to be exactly
    what is in force, per test -- see the note on ``DEV_TOOLCHAIN_EXCLUSIONS`` for
    why per *file* is not enough.
    """
    marked = _without_parametrization(selections.marked)
    recorded = set(DEV_TOOLCHAIN_EXCLUSIONS)

    assert marked == recorded, (
        f"marked but unrecorded: {sorted(marked - recorded)}; "
        f"recorded but unmarked: {sorted(recorded - marked)}"
    )


# ---------------------------------------------------------------------------
# No job widens the project default
# ---------------------------------------------------------------------------


def _workflow(name: str) -> dict[str, Any]:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _commands(script: str) -> list[str]:
    """A run block's logical commands, with backslash continuations joined."""
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


def pytest_segment(command: str) -> str:
    """The part of a logical command that is the pytest invocation itself.

    Every gate job trails its invocation with a status capture --
    ``pytest ... ; TESTS=$?`` -- and that suffix is not an argument to pytest.
    Reading it as one made ``$`` appear in the argument list and cost this
    classifier two of the four invocations it exists to find.
    """
    for segment in command.split(";"):
        if "pytest" in segment:
            return segment.strip()
    return command


def marker_expression(command: str) -> str | None:
    """The ``-m`` expression a pytest command passes, if it passes one."""
    import shlex

    try:
        tokens = shlex.split(pytest_segment(command))
    except ValueError:  # pragma: no cover - unbalanced quotes in a script
        return None
    for index, token in enumerate(tokens):
        if token == "-m" and index + 1 < len(tokens):
            return tokens[index + 1]
        if token.startswith("-m") and len(token) > 2 and not token.startswith("--"):
            return token[2:]
    return None


def collects_the_whole_tree(command: str) -> bool:
    """Whether a pytest command collects the entire suite rather than a subset.

    A command naming a narrower path -- the conformance directory, say -- is
    *supposed* to select a marker the default excludes, and passing ``-o addopts=``
    with ``-m langgraph_conformance`` is how that is done. Only a whole-tree run
    is making a claim about the suite.
    """
    import shlex

    try:
        tokens = shlex.split(pytest_segment(command))
    except ValueError:  # pragma: no cover - unbalanced quotes in a script
        return False
    if "pytest" not in " ".join(tokens):
        return False
    skip_next = False
    paths = []
    for token in tokens[1:]:
        if skip_next:
            skip_next = False
            continue
        if token in ("-m", "-o", "-p", "-k", "--junitxml", "--group"):
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        if "$" in token:
            # A path passed through a shell variable -- `run_suite() { pytest "$1"; }`
            # in the untrusted-code job. Its callers name one file each, so the
            # invocation is not a claim about the whole suite and reading it as one
            # would be wrong in the permissive direction.
            return False
        if token.startswith("tests/") or token == "tests":
            paths.append(token)
    return paths in ([], ["tests/"], ["tests"])


def _whole_suite_invocations() -> list[tuple[str, str, str | None]]:
    """Every whole-tree pytest invocation in every workflow, as (where, command, ``-m``)."""
    found: list[tuple[str, str, str | None]] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for job_name, job in (_workflow(path.name).get("jobs") or {}).items():
            for step in job.get("steps") or []:
                for command in _commands(str(step.get("run", ""))):
                    if (
                        "pytest" in command
                        and "--pytest-arg" not in command
                        and collects_the_whole_tree(command)
                    ):
                        found.append(
                            (f"{path.name}:{job_name}", command, marker_expression(command))
                        )
    return found


def test_whole_suite_invocations_are_found_at_all() -> None:
    """A parser that found nothing would make the checks below vacuous."""
    invocations = _whole_suite_invocations()

    assert len(invocations) >= 4, invocations
    assert {where for where, _, expression in invocations if expression} == set(WHEEL_VENV_JOBS)
    # The two that a naive parser loses: both trail the invocation with a status
    # capture, and both are gate jobs whose selection this module has to see.
    assert {"release-gates.yml:source", "release-gates.yml:package"} <= {
        where for where, _, _ in invocations
    }, invocations


def test_no_whole_suite_job_admits_what_the_project_default_excludes(
    selections: _Selections,
) -> None:
    """The 84-error defect, as a property of the selection rather than its spelling.

    ``-m "not live"`` is a perfectly reasonable-looking string. What made it wrong
    is only visible by running it: it collected the conformance suite, which the
    project deselects by default and which a wheel venv cannot run.
    """
    widened = sorted(selections.wheel - selections.default)

    assert widened == [], (
        "the wheel-venv marker expression admits tests the project default "
        f"excludes (first 5 of {len(widened)}): {widened[:5]}"
    )


def test_the_widening_detector_reports_the_expression_that_was_actually_shipped() -> None:
    """The negative fixture: the exact string the two jobs carried until now.

    Scoped to the conformance directory, so the check costs one small collection
    rather than a second sweep of the tree. If ``-m "not live"`` stops admitting
    conformance tests there, this module's premise is wrong and it should fail.
    """
    conformance = "tests/langgraph_gateway/conformance"

    admitted = collect("-o", "addopts=", "-m", "not live", conformance)
    default = collect(conformance)

    assert admitted, "the conformance directory collects nothing -- the fixture is vacuous"
    assert default == set(), (
        "the project default no longer excludes the conformance suite, so this "
        "negative fixture no longer demonstrates anything"
    )
    assert admitted - default != set()


def test_the_shipped_expression_is_accepted_where_it_is_correct() -> None:
    """A detector that reported every expression would be noise, not a check.

    The conformance jobs pass ``-o addopts= -m langgraph_conformance`` against a
    narrower path on purpose. Those are not whole-tree runs and must not be
    reported.
    """
    misread = [
        where
        for where, _, expression in _whole_suite_invocations()
        if expression and not expression.startswith("not ")
    ]

    assert misread == [], (
        "a narrowing invocation was classified as a whole-tree run: " f"{misread}"
    )
    assert not collects_the_whole_tree(
        "uv run --frozen --group gateway-conformance pytest -o addopts= -q "
        "-m langgraph_conformance tests/langgraph_gateway/conformance"
    )
    assert collects_the_whole_tree('pytest tests/ -q -m "not live"')
    assert collects_the_whole_tree("uv run pytest -q --junitxml=out.xml")


def test_the_jobs_that_deselect_the_marker_are_exactly_the_recorded_ones() -> None:
    """Equality again: a third job deselecting dev_toolchain is a visible diff."""
    deselecting = {
        where
        for where, _, expression in _whole_suite_invocations()
        if expression and "dev_toolchain" in expression
    }

    assert deselecting == set(WHEEL_VENV_JOBS), (
        f"observed {sorted(deselecting)}, recorded {sorted(WHEEL_VENV_JOBS)}"
    )


def test_the_default_selection_does_not_itself_deselect_the_marker() -> None:
    """The one-move defeat, closed at its source as well as by execution.

    ``test_every_dev_toolchain_test_is_selected_by_the_project_default`` already
    catches this by running the real default. Naming it here too means the failure
    report points at the line that caused it.
    """
    import tomllib

    addopts = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "tool"
    ]["pytest"]["ini_options"]["addopts"]

    assert "dev_toolchain" not in addopts, addopts


def test_the_wheel_jobs_can_run_the_wheel_content_tests() -> None:
    """``uv`` is on the runner for both, which is what those 12 errors were.

    Not marked away: ``tests/architecture/test_wheel_packaging.py`` inspects what
    the distribution ships, which is the most on-topic check a gate named "does
    the built wheel work?" has. Excluding it would have made the gate green by
    removing the part that was about the wheel.
    """
    for reference in WHEEL_VENV_JOBS:
        name, _, job_name = reference.partition(":")
        steps = _workflow(name)["jobs"][job_name]["steps"]
        assert any("setup-uv" in str(step.get("uses", "")) for step in steps), reference
