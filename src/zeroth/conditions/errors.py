"""Custom error types for the conditions subsystem.

These exceptions are raised when something goes wrong while evaluating
conditions or resolving which branch to take next in a workflow graph.
"""

from __future__ import annotations


class ConditionEvaluationError(ValueError):
    """Raised when a condition expression cannot be evaluated safely.

    For example, this is raised if the expression has a syntax error or uses
    an operator that the safe evaluator does not support.
    """


class BranchResolutionError(ValueError):
    """Raised when branch resolution cannot proceed.

    This typically means something unexpected happened while figuring out
    which edges are active for a given node.
    """
