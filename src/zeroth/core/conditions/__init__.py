"""Legacy import path for the condition contracts package.

The conditions subsystem lives in :mod:`zeroth.contracts.conditions`; this
package republishes the same objects for compatibility. Import from the
canonical location instead (see docs/backend-import-migration.md).

``ConditionResultRecorder`` is runtime-owned and resolves lazily through
:mod:`zeroth.core.conditions.recorder` so that importing this package never
puts the runtime run domain on the contracts import path.
"""

from typing import TYPE_CHECKING

from zeroth.contracts.conditions import (
    BranchResolution,
    BranchResolver,
    ConditionBinder,
    ConditionBinding,
    ConditionContext,
    ConditionEvaluator,
    ConditionOutcome,
    NextStepPlan,
    NextStepPlanner,
    TraversalState,
)

if TYPE_CHECKING:
    from zeroth.core.conditions.recorder import ConditionResultRecorder

__all__ = [
    "BranchResolution",
    "BranchResolver",
    "ConditionBinder",
    "ConditionBinding",
    "ConditionContext",
    "ConditionEvaluator",
    "ConditionOutcome",
    "ConditionResultRecorder",
    "NextStepPlan",
    "NextStepPlanner",
    "TraversalState",
]


def __getattr__(name: str) -> object:
    if name == "ConditionResultRecorder":
        import zeroth.core.conditions.recorder as recorder

        return recorder.ConditionResultRecorder
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
