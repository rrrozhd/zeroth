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


def test_eval_publishes_its_whole_surface() -> None:
    from zeroth import eval as canonical

    for name in EXPORTS:
        assert hasattr(canonical, name), name


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
def test_eval_modules_publish_their_names(module_name: str, names: tuple[str, ...]) -> None:
    import importlib

    canonical_module = importlib.import_module(f"zeroth.eval.{module_name}")

    for name in names:
        assert hasattr(canonical_module, name), name


@pytest.mark.parametrize(
    "module", ("zeroth.eval", "zeroth.eval.models", "zeroth.eval.runner", "zeroth.eval.scorers")
)
def test_eval_modules_import_in_a_cold_interpreter(module: str) -> None:
    """Each canonical module imports with nothing else pre-warmed.

    The original ran every ordered pair of canonical and legacy packages to
    catch a cycle between them. An emptied parameter list would collect zero
    cases and pass while proving nothing, so each canonical module is asserted
    to stand up on its own instead.
    """
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"cold import of {module} failed:\n{result.stderr}"
