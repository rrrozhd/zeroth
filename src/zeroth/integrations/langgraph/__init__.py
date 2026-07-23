"""Governed LangGraph integration (ZER-2).

Public API::

    from zeroth.integrations.langgraph import govern_graph

    graph = govern_graph(graph)                      # one-line install
    graph = govern_graph(graph, on_run_start=hook)   # optional stability seam

The wrapper is transparent and observed-mode only: it preserves results,
streamed chunks and exceptions, reuses the econ instrumentation delegation for
cost capture, and merges a Zeroth governance callback handler into each run's
config without replacing or duplicating user callbacks. It mints no attestation
and never promotes a run above ``admission`` (FA5); promotion to ``observed`` is
deferred.

Importing this package never imports ``langgraph`` (an optional, test-only
dependency): all langgraph use lives in the compiled graph the caller passes in.
"""

from __future__ import annotations

from zeroth.integrations.langgraph._handler import ZerothGovernanceCallbackHandler
from zeroth.integrations.langgraph._wrapper import (
    GovernedGraph,
    OnRunStart,
    RunStartContext,
    govern_graph,
)

__all__ = [
    "govern_graph",
    "GovernedGraph",
    "RunStartContext",
    "OnRunStart",
    "ZerothGovernanceCallbackHandler",
]
