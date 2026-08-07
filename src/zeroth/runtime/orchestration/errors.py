"""Public exceptions raised by the orchestration runtime.

These are protected capabilities, re-exported by
:mod:`zeroth.runtime.orchestration`. They are defined in their own module, and
that module imports nothing from ``zeroth`` at all, so any collaborator can
raise one without taking on a dependency — importable from any layer, in any
order, with no risk of an import cycle.

(An earlier revision of this docstring claimed the package facade imports its
collaborators, and that raising through the facade would therefore close a
cycle. That is not true: the facade resolves each export lazily, and importing
``OrchestratorError`` through it loads only this module.)
"""

from __future__ import annotations


class OrchestratorError(RuntimeError):
    """Something went wrong during graph orchestration.

    This is the base error for all orchestrator-related problems.
    Catch this if you want to handle any orchestration failure.
    """


class NodeDispatcherError(OrchestratorError):
    """A specific node could not be executed.

    Raised when the orchestrator doesn't know how to run a particular
    node type, or when no runner is registered for an agent node.
    """


class MemoryBindingResolutionError(OrchestratorError):
    """A template memory binding could not be resolved.

    Raised when a connector referenced in ``template_memory_bindings`` is
    not registered, or when fetching the value from the connector fails.
    """
