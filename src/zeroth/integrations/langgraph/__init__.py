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
downstream handler. **It must be installed last** -- LangChain nests
first-defined-outermost, and only the innermost layer is re-entered for every
physical tool execution, so a retry declared after it would run the tool N times
against one decision; see ``_middleware.py``'s module docstring for why no
supported mechanism detects the position. Like ``govern_tools`` it declares
``partial`` coverage and never promotes a run above ``admission``. It is exported
**lazily** for two reasons: importing it needs ``langchain.agents``, which ships
only in the optional ``gateway-conformance`` group, and that import drags in the
OpenTelemetry SDK.

**The tool-governance vocabulary is part of this surface, not an implementation
detail.** Every gate on the enforcement path is an *exact-type* gate: a verdict
is recognized only when it is exactly a :class:`ToolDecision` carrying exactly a
:class:`ToolDecisionKind`, and an action only when it is exactly a
:class:`ToolAction`. A caller cannot therefore duck-type its way to an allowing
:class:`ToolDecisionClient` -- the types are mandatory, so they are exported.
What is exported is what a caller provably needs: build a
:class:`ToolGovernanceContext`, implement a :class:`ToolDecisionClient`, return a
:class:`ToolDecision`, classify a tool with :class:`SideEffectClass`, catch the
typed refusals, and read a :class:`ToolEnforcementReport`. The normalizers, the
fingerprint digests and the enforcement entry points stay private: normalization
is done *for* a client before it is asked, identities are derived rather than
caller-asserted, and enforcement lives in exactly one place that no supported
caller re-enters.

Importing this package never imports ``langgraph`` or ``langchain`` (optional
dependencies, installed through the ``langgraph`` extra --
``pip install "zeroth-core[langgraph]"``): all langgraph use lives in the
compiled graph the caller passes in, and the middleware surface is resolved on
demand. The vocabulary above is imported eagerly because none of it touches
``langchain`` at module scope; only ``govern_tools`` / ``GovernedTool`` /
``ZerothMiddleware`` / ``emit_genai_spans`` do, and those alone stay lazy.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from zeroth.integrations.langgraph._approval_lifecycle import (
    ApprovalCoordinator,
    ApprovalDecision,
    ApprovalIntent,
    ApprovalRecord,
    ApprovalResolution,
    ApprovalState,
    ApprovalTransition,
    SQLiteApprovalRepository,
)
from zeroth.integrations.langgraph._genai import (
    GENAI_CONVENTION_VERSION,
    MappedGenAiSpan,
    PerfCounterAnchor,
    map_causal_span,
)
from zeroth.integrations.langgraph._handler import ZerothGovernanceCallbackHandler
from zeroth.integrations.langgraph._spans import CausalSpan, SpanKind, SpanStatus
from zeroth.integrations.langgraph._tool_decision_http import (
    DEFAULT_DECISION_TIMEOUT_SECONDS,
    HttpToolDecisionClient,
)
from zeroth.integrations.langgraph._tool_decisions import (
    FailClosedToolDecisionClient,
    ToolDecisionClient,
    UnknownSideEffectPolicy,
)
from zeroth.integrations.langgraph._tool_errors import (
    ApprovalRequiresThreadError,
    GovernanceContextError,
    PolicyViolation,
    ToolGovernanceError,
    UnstableToolIdentityError,
)
from zeroth.integrations.langgraph._tool_guard import ToolAuditSubmitter
from zeroth.integrations.langgraph._tool_inventory import (
    ToolEnforcementReport,
    ToolInventoryMatch,
    attest_complete_inventory,
    match_tool_inventory,
    record_tool_inventory,
    report_tool_enforcement,
)
from zeroth.integrations.langgraph._tool_types import (
    InventoryCoverage,
    SideEffectClass,
    ToolAction,
    ToolDecision,
    ToolDecisionKind,
    ToolGovernanceContext,
    ToolIdentity,
    ToolInventory,
    ToolInventoryEntry,
)
from zeroth.integrations.langgraph._wrapper import (
    GovernedGraph,
    OnRunStart,
    RunStartContext,
    govern_graph,
)

_LAZY_EXPORTS = {
    "emit_genai_spans": "zeroth.integrations.langgraph._genai_emit",
    "LangGraphGatewayClient": "zeroth.integrations.langgraph._gateway_client",
    "LangGraphGatewayError": "zeroth.integrations.langgraph._gateway_client",
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
    # --- tool governance: the vocabulary a decision is made and read in -------
    "ToolGovernanceContext",
    "ToolIdentity",
    "ToolAction",
    "ToolDecision",
    "ToolDecisionKind",
    "SideEffectClass",
    "ToolDecisionClient",
    "FailClosedToolDecisionClient",
    "LangGraphGatewayClient",
    "LangGraphGatewayError",
    "HttpToolDecisionClient",
    "DEFAULT_DECISION_TIMEOUT_SECONDS",
    "UnknownSideEffectPolicy",
    "ToolAuditSubmitter",
    "ApprovalCoordinator",
    "ApprovalDecision",
    "ApprovalIntent",
    "ApprovalRecord",
    "ApprovalResolution",
    "ApprovalState",
    "ApprovalTransition",
    "SQLiteApprovalRepository",
    # --- tool governance: the typed refusals a governed call can raise --------
    "ToolGovernanceError",
    "PolicyViolation",
    "GovernanceContextError",
    "UnstableToolIdentityError",
    "ApprovalRequiresThreadError",
    # --- tool governance: what the governed surface holds and may claim -------
    "ToolInventory",
    "ToolInventoryEntry",
    "InventoryCoverage",
    "ToolInventoryMatch",
    "ToolEnforcementReport",
    "record_tool_inventory",
    "report_tool_enforcement",
    "match_tool_inventory",
    "attest_complete_inventory",
]
