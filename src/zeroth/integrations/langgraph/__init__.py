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
sink, not delivered / persisted (ZER-5).

Those neutral records are mapped onto the OpenTelemetry GenAI semantic
conventions by :func:`map_causal_span` (ZER-4), a pure function importable
without OpenTelemetry. :func:`emit_genai_spans` turns a batch into a real span
tree and is therefore exported **lazily**: it needs the optional ``otel`` extra,
so it is resolved on first attribute access and importing this package never
pulls in OpenTelemetry.

Importing this package never imports ``langgraph`` (an optional, test-only
dependency): all langgraph use lives in the compiled graph the caller passes in.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from zeroth.integrations.langgraph._genai import (
    GENAI_CONVENTION_VERSION,
    MappedGenAiSpan,
    PerfCounterAnchor,
    map_causal_span,
)
from zeroth.integrations.langgraph._handler import ZerothGovernanceCallbackHandler
from zeroth.integrations.langgraph._spans import CausalSpan, SpanKind, SpanStatus
from zeroth.integrations.langgraph._wrapper import (
    GovernedGraph,
    OnRunStart,
    RunStartContext,
    govern_graph,
)

_LAZY_EXPORTS = {"emit_genai_spans": "zeroth.integrations.langgraph._genai_emit"}
"""Names resolved on first access because their module imports OpenTelemetry."""


def __getattr__(name: str) -> Any:
    """Resolve an OpenTelemetry-dependent export on first access (PEP 562)."""
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value  # cache: subsequent lookups skip __getattr__
    return value


def __dir__() -> list[str]:
    """List the public API, including the not-yet-resolved lazy exports."""
    return sorted(__all__)


__all__ = [
    "govern_graph",
    "GovernedGraph",
    "RunStartContext",
    "OnRunStart",
    "ZerothGovernanceCallbackHandler",
    "CausalSpan",
    "SpanKind",
    "SpanStatus",
    "GENAI_CONVENTION_VERSION",
    "MappedGenAiSpan",
    "PerfCounterAnchor",
    "map_causal_span",
    "emit_genai_spans",
]
