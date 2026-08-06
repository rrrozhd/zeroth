"""Public exceptions raised by the orchestration runtime.

These are protected legacy capabilities: ``zeroth.runtime.orchestration`` and
``zeroth.runtime.orchestration`` re-export the same class objects, so both
legacy import locations keep resolving. The definitions live here because the
collaborators that raise them may not import the legacy facade — doing so would
close an import cycle, since the facade imports the collaborators.

This module imports nothing from ``zeroth``, so it is importable from any layer
and in any order.
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
