"""The middleware install surface: ``ZerothMiddleware`` for ``create_agent``.

**This module composes; it decides nothing.** Exactly like
:mod:`~zeroth.integrations.langgraph._tool_wrappers`, every allow, deny and
approval branch lives in :mod:`~zeroth.integrations.langgraph._tool_guard`:
:meth:`ZerothMiddleware.wrap_tool_call` calls
:func:`~zeroth.integrations.langgraph._tool_guard.guard_tool_call` and
:meth:`ZerothMiddleware.awrap_tool_call` calls
:func:`~zeroth.integrations.langgraph._tool_guard.authorize_tool_call` before
awaiting its own downstream. There is deliberately no async enforcement core and
no second copy of the fail-closed rules -- two implementations of "may this tool
run" is two places for them to diverge, and the one that drifts is the one that
fails *open*.

**``handler`` is the downstream, so not calling it is the enforcement.** LangChain
hands the tool's execution in as a callback. A denial raises before the callback
is reached and an approval suspends before it is reached, so "the tool did not
run" is observable directly as "the handler was called zero times" rather than
inferred from a return value.

**A denial propagates; it is never rendered as an error ``ToolMessage``.** The
guard's contract is that governance decides *whether* a call happens, not what it
means, and turning
:class:`~zeroth.integrations.langgraph._tool_errors.PolicyViolation` into a
message here would be a second representation of the verdict -- one the wrapper
surface does not produce and could not be held to. Callers who want a denial fed
back to the model catch the exception in their own middleware, outside
governance.

**Nesting order (R12).** LangChain composes ``wrap_tool_call`` middleware with
the *first entry outermost*: for
``create_agent(..., middleware=[A, ZerothMiddleware(...), B])`` a tool call
enters ``A``, then ``ZerothMiddleware``, then ``B``, and unwinds in reverse.
Governance therefore sees every call that middleware *before* it let through, and
``B`` -- with everything nested inside it -- only ever runs on a call governance
allowed. Put ``ZerothMiddleware`` ahead of any middleware that must not observe a
refused call, and behind any that legitimately rewrites the request first.

**The converse constrains it from the other side: governance should be the
innermost layer that touches the request.** A middleware nested *inside* it can
still call ``handler(request.override(tool_call=modified))``, and the tool then
runs with arguments the decision was never made about. Nothing in LangChain
prevents that and nothing here can detect it, so the ordering is the control:
anything that rewrites a tool call belongs *outside* ``ZerothMiddleware``, where
governance sees its output, not inside it, where governance sees only its input.

**Identity is read off the registered tool, not off the model's request.**
:class:`~langchain.agents.middleware.ToolCallRequest` carries both a ``tool_call``
the model produced and the ``tool`` the agent resolved it to. The fingerprint is
derived from the resolved tool through the same
``_describe_base_tool`` the wrapper surface uses -- so one tool fingerprints
identically on both surfaces -- and the requested name must match the resolved
tool's, because a request naming one tool while carrying another is a request
whose decision would be recorded against the wrong thing.

**An unregistered tool is refused, not decided.** ``request.tool`` is ``None``
when the agent could not resolve the call, and a tool with no declared surface
has no material to pin an identity against. It raises
:class:`~zeroth.integrations.langgraph._tool_errors.UnstableToolIdentityError`
rather than being decided against a name alone.

**No callback handler is registered here.** ``_callbacks.py`` normalizes a run
down to exactly one canonical
:class:`~zeroth.integrations.langgraph._handler.ZerothGovernanceCallbackHandler`,
and a second governance identity attached from this surface would be a second
thing claiming to be that one. Tool enforcement needs none: its evidence travels
the audit submitter the guard already writes to.

Importing this module imports ``langchain.agents``, which is an optional
(``gateway-conformance``) dependency and drags in the OpenTelemetry SDK by way of
``langsmith``. It is therefore exported **lazily** from the package -- see
``__init__.py`` -- exactly as ``govern_tools`` is.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest
from langchain_core.tools import BaseTool

from zeroth.governance.identity import ActorIdentity
from zeroth.integrations.langgraph._tool_decisions import (
    ToolDecisionClient,
    UnknownSideEffectPolicy,
)
from zeroth.integrations.langgraph._tool_errors import UnstableToolIdentityError
from zeroth.integrations.langgraph._tool_guard import (
    ToolAuditSubmitter,
    authorize_tool_call,
    guard_tool_call,
)
from zeroth.integrations.langgraph._tool_normalize import (
    normalize_identifier,
    normalize_tool_action,
)
from zeroth.integrations.langgraph._tool_types import ToolAction
from zeroth.integrations.langgraph._tool_wrappers import (
    _describe_base_tool,
    _peek,
    _resolve_context,
    _resolved,
)

_TOOL_CALL_NAME = "name"
_TOOL_CALL_ARGUMENTS = "args"
"""The two ``ToolCall`` keys a decision is made from.

Read by iterating the mapping once rather than by keying into it, so an object
that answers one thing to iteration and another to a lookup cannot present a
benign call to the gate and a different one to the tool.
"""


def _requested_call(request: object) -> tuple[object, object]:
    """Read the model's requested tool name and arguments off a middleware request.

    Args:
        request: The middleware request, which is not trusted to be one.

    Returns:
        The requested name and arguments, both still ungated.

    Raises:
        UnstableToolIdentityError: If the request carries no plain ``dict`` tool
            call. An exact-type gate, so a ``dict`` subclass overriding
            ``items()`` cannot smuggle a second reading of the same call.
    """
    call = _peek(request, "tool_call")
    if type(call) is not dict:
        raise UnstableToolIdentityError("a governed tool call needs a plain tool-call mapping")
    name: object = None
    arguments: object = None
    for key, value in call.items():
        # The *key* passes the same exact-type gate every identifier in this
        # package does. A ``str`` subclass whose ``__eq__`` answers ``True`` to
        # everything would otherwise claim the name slot and leave the arguments
        # unread -- decided as an empty call while the tool ran with real ones.
        # Skipping it instead leaves the name absent, which refuses the call.
        if type(key) is not str:
            continue
        if key == _TOOL_CALL_NAME:
            name = value
        elif key == _TOOL_CALL_ARGUMENTS:
            arguments = value
    return name, arguments


def _requested_tool(request: object) -> BaseTool:
    """Return the tool the agent resolved this call to, refusing a call with none.

    ``isinstance`` rather than an exact-type gate for the reason the wrapper
    surface gives: every real tool is a ``BaseTool`` *subclass*, so an exact gate
    would reject the whole framework. The hostile-subtype defense is on the
    values read off it, all of which pass the same gates as everywhere else.

    Raises:
        UnstableToolIdentityError: If the request carries no resolved tool. An
            unregistered call has no declared surface to fingerprint, so it is
            refused rather than decided against its name alone.
    """
    tool = _peek(request, "tool")
    if not isinstance(tool, BaseTool):
        raise UnstableToolIdentityError("a governed tool call needs a resolved BaseTool")
    return tool


def _matched_name(requested: object, resolved: object) -> str:
    """Return the name a call is decided under, refusing a request that renames it.

    Both sides go through the same identifier gate, so a ``str`` subclass on
    either cannot make two different names compare equal.

    Raises:
        UnstableToolIdentityError: If either name is unusable, or the requested
            name is not the resolved tool's.
    """
    wanted = normalize_identifier(requested)
    actual = normalize_identifier(resolved)
    if wanted is None or actual is None or wanted != actual:
        raise UnstableToolIdentityError("the requested tool name is not the resolved tool's")
    return actual


class ZerothMiddleware(AgentMiddleware):
    """Governs every tool call a ``create_agent`` agent makes, deciding before it runs.

    **The second install surface for tool enforcement.** Pass it to
    ``create_agent(..., middleware=[ZerothMiddleware(context=...)])`` and every
    tool call the agent makes is decided by the same core
    :func:`~zeroth.integrations.langgraph._tool_wrappers.govern_tools` decides
    through. Allow, deny and approval mean exactly what they mean there, because
    neither surface carries an enforcement branch of its own.

    **Supplying no context refuses every call, deliberately.** The principal is
    injected and never discovered, so an agent governed without one is governed
    fail-closed rather than governed unattributed.

    Attributes:
        tools: Declared empty and never populated. A middleware's ``tools`` are
            *injected into* the agent, and a governance layer that added tools
            would be widening the surface it exists to narrow.
    """

    tools: Sequence[BaseTool] = ()

    def __init__(
        self,
        *,
        context: object = None,
        client: ToolDecisionClient | None = None,
        unknown_side_effect: UnknownSideEffectPolicy = UnknownSideEffectPolicy.DENY,
        audit: ToolAuditSubmitter | None = None,
        actor: ActorIdentity | None = None,
        interrupt: Callable[[Mapping[str, Any]], Any] | None = None,
        side_effect: Callable[[Any], Any] | None = None,
        contract_ref: Callable[[Any], Any] | None = None,
    ) -> None:
        """Pin the seams every call through this middleware is decided through.

        Args:
            context: The governance context each call is attributed to, or a
                zero-argument callable returning one per call. ``None`` refuses
                every call.
            client: The decision client, or ``None`` to deny for want of one.
            unknown_side_effect: Whether a tool nobody classified may be invoked.
            audit: Where each decision record is handed off, or ``None`` to
                decide without recording.
            actor: The authenticated actor to attribute records to, when there
                is one.
            interrupt: The pause seam, defaulting to LangGraph's ``interrupt``.
            side_effect: An optional per-tool classifier. Only a real
                :class:`~zeroth.integrations.langgraph._tool_types.SideEffectClass`
                member classifies a tool; anything else leaves it unknown, and
                unknown is denied unless *unknown_side_effect* says otherwise.
            contract_ref: An optional per-tool contract resolver.
        """
        super().__init__()
        self._context = context
        self._client = client
        self._unknown_side_effect = unknown_side_effect
        self._audit = audit
        self._actor = actor
        self._interrupt = interrupt
        self._side_effect = side_effect
        self._contract_ref = contract_ref

    def _seams(self) -> dict[str, Any]:
        """Render the pinned seams as the keyword arguments the enforcement core takes."""
        return {
            "client": self._client,
            "unknown_side_effect": self._unknown_side_effect,
            "audit": self._audit,
            "actor": self._actor,
            "interrupt": self._interrupt,
        }

    def _describe(self, request: object) -> tuple[ToolAction, object]:
        """Build the normalized descriptor one middleware request is decided from.

        The single place a request becomes a decidable action: both the sync and
        the async surface call this and then hand the result to the shared
        enforcement core, so the two cannot describe the same call differently.

        Args:
            request: The middleware request, which is not trusted to be one.

        Returns:
            The normalized action and the governance context it is attributed
            to, both handed on to the enforcement core unchanged.

        Raises:
            UnstableToolIdentityError: If the request carries no resolved tool,
                no plain tool call, or a name that is not the resolved tool's.
            GovernanceContextError: If the call cannot be attributed.
            ToolGovernanceError: If the arguments are not canonically
                representable.
        """
        tool = _requested_tool(request)
        requested_name, arguments = _requested_call(request)
        facts = _describe_base_tool(tool)
        name = _matched_name(requested_name, facts.name)
        context = _resolve_context(self._context)
        action = normalize_tool_action(
            name=name,
            arguments={} if arguments is None else arguments,
            context=context,
            identity_material=facts.material,
            contract_ref=_resolved(self._contract_ref, tool),
            side_effect=_resolved(self._side_effect, tool),
        )
        return action, context

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        """Govern this call, then run the downstream handler exactly once on an allow.

        ``handler`` is passed to the enforcement core as the invocation, so it is
        called once on an allow, outside any ``try`` and any loop, and not at all
        on any other verdict. An exception the tool raises propagates unchanged
        and a failed call is never retried.

        Args:
            request: The tool call the agent is about to make.
            handler: LangChain's downstream execution of that call.

        Returns:
            Whatever the handler returned.

        Raises:
            ToolGovernanceError: Whenever the call did not proceed. See
                :func:`~zeroth.integrations.langgraph._tool_guard.authorize_tool_call`
                for which subclass names which condition.
        """
        action, context = self._describe(request)
        return guard_tool_call(action, context, lambda: handler(request), **self._seams())

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """Govern this call, then await the downstream handler once on an allow.

        Authorization is the same synchronous core the sync path runs; only the
        downstream is awaited. There is no async enforcement branch to drift out
        of step with the sync one.

        Args:
            request: The tool call the agent is about to make.
            handler: LangChain's downstream execution of that call.

        Returns:
            Whatever the handler returned.

        Raises:
            ToolGovernanceError: Whenever the call did not proceed.
        """
        action, context = self._describe(request)
        authorize_tool_call(action, context, **self._seams())
        return await handler(request)


__all__ = ["ZerothMiddleware"]
