"""Canonical import surface for the evaluation library.

Non-golden boundary tests for the Task 15 eval move: the canonical
top-level ``zeroth.eval`` package must publish the same objects the legacy
``zeroth.core.eval`` path keeps republishing, and both packages must stay
cold-importable from a fresh interpreter in either order.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

EXPORTS = (
    "CaseResult",
    "ContainsScorer",
    "EvalCase",
    "EvalDataset",
    "EvalReport",
    "EvalTarget",
    "EvalThresholdError",
    "ExactMatchScorer",
    "JudgeVerdict",
    "LLMJudgeScorer",
    "PredicateScorer",
    "RegexScorer",
    "SchemaScorer",
    "Score",
    "Scorer",
    "gate",
    "run_eval",
)


def test_eval_is_the_same_surface_through_both_paths() -> None:
    from zeroth import eval as canonical
    from zeroth.core import eval as legacy

    for name in EXPORTS:
        assert getattr(canonical, name) is getattr(legacy, name), name


@pytest.mark.parametrize(
    ("module_name", "names"),
    [
        ("models", ("CaseResult", "EvalCase", "EvalDataset", "EvalReport", "Score")),
        ("runner", ("EvalTarget", "EvalThresholdError", "gate", "run_eval")),
        (
            "scorers",
            (
                "ContainsScorer",
                "ExactMatchScorer",
                "JudgeVerdict",
                "LLMJudgeScorer",
                "PredicateScorer",
                "RegexScorer",
                "SchemaScorer",
                "Scorer",
            ),
        ),
    ],
)
def test_eval_modules_are_the_same_surface_through_both_paths(
    module_name: str, names: tuple[str, ...]
) -> None:
    import importlib

    legacy_module = importlib.import_module(f"zeroth.core.eval.{module_name}")
    canonical_module = importlib.import_module(f"zeroth.eval.{module_name}")

    for name in names:
        assert getattr(canonical_module, name) is getattr(legacy_module, name), name


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("zeroth.eval", "zeroth.core.eval"),
        ("zeroth.core.eval", "zeroth.eval"),
    ],
)
def test_eval_cold_imports_from_both_directions(first: str, second: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}\nimport {second}\n"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"cold import {first} then {second} failed:\n{result.stderr}"
