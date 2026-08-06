"""Canonical import surface for the condition contracts package.

Non-golden boundary tests for the Task 12 conditions move: the canonical
``zeroth.contracts.conditions`` package must publish the same objects the
legacy ``zeroth.core.conditions`` path keeps republishing, and both packages
must stay cold-importable from a fresh interpreter in either order.

Two runtime-owned seams are pinned alongside the move. ``RunConditionResult``
is defined in ``zeroth.contracts.conditions.models`` — it is the conditions
domain's evaluation-outcome vocabulary — while ``zeroth.core.runs.models`` and
``zeroth.runtime.runs`` keep republishing it on the run surface.
``ConditionResultRecorder`` mutates ``Run`` objects, so it lives in
``zeroth.runtime.runs``; the legacy conditions paths resolve it lazily so the
runtime never lands on the import path of the contracts package.
"""

from __future__ import annotations

import subprocess
import sys


def test_models_publishes_its_whole_surface() -> None:
    """Every name the package is documented to export is still exported.

    This replaced a parity assertion comparing each name against the legacy
    republisher. ZER-25 removed that path, so the comparison would compare
    the module with itself; the surface it pinned is asserted directly.
    """
    import zeroth.contracts.conditions as canonical

    expected = {
        "BranchResolution",
        "BranchResolver",
        "ConditionBinder",
        "ConditionBinding",
        "ConditionContext",
        "ConditionEvaluator",
        "ConditionOutcome",
        "NextStepPlan",
        "NextStepPlanner",
        "TraversalState",
    }

    missing = sorted(name for name in expected if not hasattr(canonical, name))
    assert not missing, f"zeroth.contracts.conditions no longer publishes: {missing}"


def test_condition_errors_are_the_same_surface_through_both_paths() -> None:
    from zeroth.contracts.conditions import errors as canonical
    from zeroth.core.conditions import errors as legacy

    assert canonical.BranchResolutionError is legacy.BranchResolutionError
    assert canonical.ConditionEvaluationError is legacy.ConditionEvaluationError


def test_run_condition_result_has_one_contract_owned_definition() -> None:
    from zeroth.contracts.conditions import models as canonical
    from zeroth.core.runs import models as legacy_runs
    from zeroth.runtime import runs as runtime_runs

    assert legacy_runs.RunConditionResult is canonical.RunConditionResult
    assert runtime_runs.RunConditionResult is canonical.RunConditionResult


def test_condition_recorder_is_runtime_owned_and_lazily_republished() -> None:
    from zeroth.core import conditions as legacy
    from zeroth.core.conditions import recorder as legacy_recorder
    from zeroth.runtime import runs as runtime_runs

    assert legacy.ConditionResultRecorder is runtime_runs.ConditionResultRecorder
    assert legacy_recorder.ConditionResultRecorder is runtime_runs.ConditionResultRecorder


def test_models_imports_in_a_cold_interpreter() -> None:
    """The canonical package imports with nothing else pre-warmed.

    This kept the canonical half of a test that used to import the legacy
    and canonical packages in both orders, guarding a cycle between them.
    With the legacy package gone there is one direction left to guard.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import zeroth.contracts.conditions.models"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
