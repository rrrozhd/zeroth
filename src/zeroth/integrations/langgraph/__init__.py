"""Governed LangGraph integration (ZER-2 wrapper, ZER-3 ancestry capture).

Public API::

    from zeroth.integrations.langgraph import govern_graph

    graph = govern_graph(graph)                      # one-line install
    graph = govern_graph(graph, on_run_start=hook)   # optional stability seam
    tools = govern_tools(tools, context=context)     # raw tool lists (ZER-6)

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

:func:`govern_tools` is the second install surface (ZER-6): it returns governed
twins of a raw tool list -- sync and async ``BaseTool`` instances and plain
callables alike -- without mutating any original or its ``.func`` / ``.coroutine``.
Every wrapper decides through the one shared enforcement core, so allow, deny and
approval mean exactly what they mean on the middleware surface. Wrapping declares
``partial`` coverage; it never promotes a run above ``admission``. It is exported
**lazily**, for the same reason ``emit_genai_spans`` is: it needs ``BaseTool``,
and importing that pulls ``langsmith`` and with it the OpenTelemetry SDK.

:class:`ZerothMiddleware` is the third install surface (ZER-6): an
``AgentMiddleware`` for ``create_agent`` that decides every tool call through the
same shared core the wrappers use, refusing or suspending *without* invoking the
downstream handler. It is exported **lazily** for two reasons: importing it needs
``langchain.agents``, which ships only in the optional ``gateway-conformance``
group, and that import drags in the OpenTelemetry SDK.

Importing this package never imports ``langgraph`` or ``langchain`` (optional,
test-only dependencies): all langgraph use lives in the compiled graph the caller
passes in, and the middleware surface is resolved on demand.
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

_LAZY_EXPORTS = {
    "emit_genai_spans": "zeroth.integrations.langgraph._genai_emit",
    "govern_tools": "zeroth.integrations.langgraph._tool_wrappers",
    "GovernedTool": "zeroth.integrations.langgraph._tool_wrappers",
    "ZerothMiddleware": "zeroth.integrations.langgraph._middleware",
}
"""Names resolved on first access because importing their module imports OpenTelemetry.

``_genai_emit`` needs it directly. The tool wrappers reach it by a longer road:
they need ``BaseTool``, and ``from langchain_core.tools import BaseTool`` pulls
``langsmith``, which imports the OpenTelemetry SDK at module scope. The rest of
this package only ever touches ``langchain_core.runnables`` /
``langchain_core.callbacks``, which do not, so ``import
zeroth.integrations.langgraph`` stays OpenTelemetry-free exactly as long as the
tool surface is resolved on demand.

``ZerothMiddleware`` is lazy for that reason *and* for a second, harder one: it
needs ``langchain.agents``, which ships only in the optional
``gateway-conformance`` group. A module-scope import of it here would turn
``import zeroth.integrations.langgraph`` into an ``ImportError`` for every
existing caller who installed the package without that group.
"""


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
    "govern_tools",
    "GovernedTool",
    "ZerothMiddleware",
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
