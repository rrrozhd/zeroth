"""Agent evaluation harness: datasets, scorers, runner, and CI gate (EVAL).

Measures agent/graph output *quality* against reference cases — distinct from
``agent_runtime.OutputValidator``, which validates output *shape*. Decoupled from
any specific executor: evaluate anything via a ``target`` callable that maps a case
input to an output.
"""

from zeroth.eval.models import (
    CaseResult,
    EvalCase,
    EvalDataset,
    EvalReport,
    Score,
)
from zeroth.eval.runner import EvalTarget, EvalThresholdError, gate, run_eval
from zeroth.eval.scorers import (
    ContainsScorer,
    ExactMatchScorer,
    JudgeVerdict,
    LLMJudgeScorer,
    PredicateScorer,
    RegexScorer,
    SchemaScorer,
    Scorer,
)

__all__ = [
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
]
