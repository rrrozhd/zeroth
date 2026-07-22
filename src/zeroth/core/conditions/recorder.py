"""Legacy import path for :mod:`zeroth.runtime.runs.condition_recorder`.

``ConditionResultRecorder`` mutates ``Run`` objects, so it lives in the
runtime run domain. Resolution stays lazy: an eager import here would put
the runtime on the import path of the legacy conditions package.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zeroth.runtime.runs.condition_recorder import ConditionResultRecorder

__all__ = ["ConditionResultRecorder"]


def __getattr__(name: str) -> object:
    if name == "ConditionResultRecorder":
        import zeroth.runtime.runs.condition_recorder as condition_recorder

        return condition_recorder.ConditionResultRecorder
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
