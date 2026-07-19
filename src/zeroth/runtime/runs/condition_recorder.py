"""Records condition evaluation results onto a run.

After the branch resolver decides which edges are active, the results need
to be saved on the Run object so they can be inspected later for debugging
and auditing. This module handles that bookkeeping. It lives in the runtime
run domain rather than the conditions contracts because it mutates ``Run``
objects, which the contracts layer may not depend on.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zeroth.runtime.runs.models import Run, RunConditionResult


class ConditionResultRecorder:
    """Saves condition evaluation results onto a Run for later inspection.

    Use this after branch resolution to persist the results so you can
    see exactly why each branch was taken or suppressed.
    """

    def record(self, run: Run, result: RunConditionResult) -> Run:
        """Append a single condition result to the run and update its timestamp."""
        run.condition_results.append(result)
        run.touch()
        return run

    def record_many(self, run: Run, results: Iterable[RunConditionResult]) -> Run:
        """Append multiple condition results to the run at once and update its timestamp."""
        run.condition_results.extend(results)
        run.touch()
        return run
