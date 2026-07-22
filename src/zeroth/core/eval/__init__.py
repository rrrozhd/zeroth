"""Legacy import path for the evaluation library.

The evaluation harness lives in :mod:`zeroth.eval`; this package
republishes the same objects for compatibility. Import from the canonical
location instead (see docs/backend-import-migration.md).
"""

from zeroth.eval import (
    CaseResult,
    ContainsScorer,
    EvalCase,
    EvalDataset,
    EvalReport,
    EvalTarget,
    EvalThresholdError,
    ExactMatchScorer,
    JudgeVerdict,
    LLMJudgeScorer,
    PredicateScorer,
    RegexScorer,
    SchemaScorer,
    Score,
    Scorer,
    gate,
    run_eval,
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
