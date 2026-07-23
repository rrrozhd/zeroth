"""Governed LangGraph integration (ZER-2 wrapper, ZER-3 ancestry capture).

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

The governance handler reconstructs the run's causal ``run_id`` / ``parent_run_id``
tree into neutral :class:`CausalSpan` records and carries the gateway correlation
id onto each span (ZER-3). This is capture only: spans are held in an in-memory
sink, not delivered / persisted (ZER-5), and are OpenTelemetry-agnostic (ZER-4).

Importing this package never imports ``langgraph`` (an optional, test-only
dependency): all langgraph use lives in the compiled graph the caller passes in.
"""

from __future__ import annotations

from zeroth.integrations.langgraph._handler import ZerothGovernanceCallbackHandler
from zeroth.integrations.langgraph._spans import CausalSpan, SpanKind, SpanStatus
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
    "CausalSpan",
    "SpanKind",
    "SpanStatus",
]
