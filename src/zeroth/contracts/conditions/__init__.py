"""Conditional execution contracts.

This package handles deciding which path to take next in an agent workflow
graph. It evaluates conditions on edges, figures out which branches are
active, and describes the outcome. Recording outcomes onto a run is
runtime-owned: see ``zeroth.runtime.runs.condition_recorder``.
"""

from zeroth.contracts.conditions.binding import ConditionBinder, ConditionBinding
from zeroth.contracts.conditions.branch import (
    BranchResolution,
    BranchResolver,
    NextStepPlan,
    NextStepPlanner,
)
from zeroth.contracts.conditions.evaluator import ConditionContext, ConditionEvaluator
from zeroth.contracts.conditions.models import (
    ConditionOutcome,
    RunConditionResult,
    TraversalState,
)

__all__ = [
    "BranchResolution",
    "BranchResolver",
    "ConditionBinder",
    "ConditionBinding",
    "ConditionContext",
    "ConditionEvaluator",
    "ConditionOutcome",
    "NextStepPlan",
    "NextStepPlanner",
    "RunConditionResult",
    "TraversalState",
]
