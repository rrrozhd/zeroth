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

import pytest


def test_conditions_is_the_same_surface_through_both_paths() -> None:
    from zeroth.contracts import conditions as canonical
    from zeroth.core import conditions as legacy

    assert canonical.BranchResolution is legacy.BranchResolution
    assert canonical.BranchResolver is legacy.BranchResolver
    assert canonical.ConditionBinder is legacy.ConditionBinder
    assert canonical.ConditionBinding is legacy.ConditionBinding
    assert canonical.ConditionContext is legacy.ConditionContext
    assert canonical.ConditionEvaluator is legacy.ConditionEvaluator
    assert canonical.ConditionOutcome is legacy.ConditionOutcome
    assert canonical.NextStepPlan is legacy.NextStepPlan
    assert canonical.NextStepPlanner is legacy.NextStepPlanner
    assert canonical.TraversalState is legacy.TraversalState


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


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("zeroth.contracts.conditions", "zeroth.core.conditions"),
        ("zeroth.core.conditions", "zeroth.contracts.conditions"),
        ("zeroth.contracts.conditions", "zeroth.core.runs.models"),
        ("zeroth.core.runs.models", "zeroth.contracts.conditions"),
        ("zeroth.contracts.conditions", "zeroth.runtime.runs"),
        ("zeroth.runtime.runs", "zeroth.contracts.conditions"),
    ],
)
def test_conditions_cold_imports_from_both_directions(first: str, second: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}\nimport {second}\n"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"cold import {first} then {second} failed:\n{result.stderr}"
