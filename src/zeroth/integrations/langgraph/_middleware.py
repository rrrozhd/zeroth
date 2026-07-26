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

**The converse is a hard requirement, not a preference: ``ZerothMiddleware`` must
be the LAST ``wrap_tool_call`` middleware in the list.** Two distinct failures
follow from nesting anything inside it, and neither is detectable from here.

*Rewritten arguments.* A middleware nested inside can call
``handler(request.override(tool_call=modified))``, and the tool then runs with
arguments the decision was never made about. Anything that rewrites a tool call
belongs *outside*, where governance sees its output rather than its input.

*Undecided executions.* LangChain hands each layer a handler its own body may
call **as many times as it likes** -- ``_chain_tool_call_wrappers.compose_two``
in ``langchain/agents/factory.py`` says so in as many words ("Outer can call
call_inner multiple times"), and the shipped ``ToolRetryMiddleware`` does exactly
that. So a retrying middleware nested inside governance runs the tool body N
times against **one** decision and **one** audit record.
Innermost, the same retry re-enters ``wrap_tool_call`` per attempt, and every
physical execution gets its own decision and its own record -- which is the
property the guarantee is actually about.

**"Exactly once" here is a statement about the handler, and the handler is not
the body.** :meth:`ZerothMiddleware.wrap_tool_call` calls its downstream once per
decision; how many times the tool body runs underneath that downstream is
whatever the layers below it do. The two coincide only when governance is
innermost, which is why the ordering is the contract rather than a suggestion.

**No supported mechanism detects the position, so nothing here tries.**
:class:`~langchain.agents.middleware.AgentMiddleware` exposes no hook that
receives the middleware list, ``create_agent`` composes the chain into a closure
the middleware never sees, and the only difference between an innermost install
and a nested one is whether ``handler`` is the tool executor or LangChain's
private ``compose_two.<locals>.call_inner``. Telling those apart means reading
another library's local closures: it would break silently on a refactor there,
and a guard that silently stops guarding is worse than a documented contract with
tests behind it. The contract is pinned instead by
``test_an_outer_retry_gets_a_decision_and_a_record_per_physical_execution`` and
its async twin, with the nested-retry limitation pinned by
``test_a_retry_nested_inside_governance_runs_the_body_undecided`` so it can never
quietly become a claim.

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

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest
from langchain_core.tools import BaseTool

from zeroth.governance.identity import ActorIdentity
from zeroth.integrations.langgraph._tool_decisions import (
    ToolDecisionClient,
    UnknownSideEffectPolicy,
)
from zeroth.integrations.langgraph._tool_errors import (
    ToolGovernanceError,
    UnstableToolIdentityError,
)
from zeroth.integrations.langgraph._tool_guard import (
    ToolAuditSubmitter,
    authorize_tool_call,
    guard_tool_call,
)
from zeroth.integrations.langgraph._tool_inventory import (
    ToolEnforcementReport,
    record_binding_inventory,
    report_tool_enforcement,
)
from zeroth.integrations.langgraph._tool_normalize import (
    normalize_identifier,
    normalize_tool_action,
)
from zeroth.integrations.langgraph._tool_types import ToolAction, ToolInventory
from zeroth.integrations.langgraph._tool_wrappers import (
    GovernedToolBinding,
    _describe_base_tool,
    _peek,
    _pin,
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


def _declared_binding(tool: object) -> GovernedToolBinding:
    """Pin one declared tool the way a call through it will be described.

    Through ``_pin`` and ``_describe_base_tool``, which is what makes the
    inventory's fingerprint the fingerprint the *decision* is made under: an
    inventory derived any other way could name a tool the guard would refuse to
    recognize, and the report would be about tools nothing actually governs.

    **No authorization resolver is invoked.** Recording the declared inventory
    used to ask the caller's classifier and contract resolver about each tool,
    and a resolver is allowed to be live: asking it *consumes* an answer, so the
    first real call was decided under the second answer and every call after it
    was shifted by one. Declaring an inventory could therefore flip a denial into
    an allow -- the same installation, differing only in ``expected_tools``. The
    identity is recorded and nothing else is; the classification and the contract
    are resolved live, per call, in :meth:`ZerothMiddleware._describe`.

    Args:
        tool: The declared tool, which is not trusted to be one.

    Returns:
        What was pinned about the tool, for the inventory only.

    Raises:
        UnstableToolIdentityError: If *tool* is not a ``BaseTool`` -- the same
            gate a live call passes, so a declared list cannot hold something the
            middleware could never be handed -- or carries no usable identity.
    """
    if not isinstance(tool, BaseTool):
        raise UnstableToolIdentityError("a declared tool must be a resolved BaseTool")
    return _pin(_describe_base_tool(tool))


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

    **Install it LAST.** ``middleware=[...everything else..., ZerothMiddleware()]``
    makes it the innermost ``wrap_tool_call`` layer, which is what makes every
    physical tool execution -- including each attempt of an outer retry -- its own
    decision and its own audit record. See the module docstring for the two
    failures that follow from nesting a middleware inside it, and for why no
    supported mechanism detects the position.

    Attributes:
        tools: Declared empty and never populated. A middleware's ``tools`` are
            *injected into* the agent, and a governance layer that added tools
            would be widening the surface it exists to narrow. ``expected_tools``
            is not this: it is recorded for the inventory and never handed to the
            agent.
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
        expected_tools: Iterable[object] = (),
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
            expected_tools: The tools this installation is declared to govern,
                recorded into :attr:`tool_inventory` and reported by
                :meth:`enforcement_report`. **Not injected**: they are not added
                to :attr:`tools`, not handed to the agent, and not wrapped.
                Declaring none reports an empty surface.

        Raises:
            UnstableToolIdentityError: If a declared tool is not a ``BaseTool``,
                carries no usable identity, or two of them share a name. The
                recording happens here so an unusable declaration fails at
                install rather than at the moment somebody asks for the report.
            ToolGovernanceError: If *expected_tools* is not iterable.
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
        # Materialized before anything is pinned, so a ``TypeError`` raised
        # *inside* the pinning cannot be reported as "the list was not iterable".
        try:
            declared = list(expected_tools)
        except TypeError as error:
            raise ToolGovernanceError("an expected tool list must be iterable") from error
        self._inventory = record_binding_inventory(
            [_declared_binding(tool) for tool in declared]
        )

    @property
    def tool_inventory(self) -> ToolInventory:
        """The tools this installation was declared to govern, as it recorded them.

        Always
        :attr:`~zeroth.integrations.langgraph._tool_types.InventoryCoverage.PARTIAL`.
        A middleware is handed one tool at a time, per call, and never sees the
        agent's tool list, so it cannot know it saw everything -- and a
        declaration is not a discovery. Pass it and a declared identity list to
        :func:`~zeroth.integrations.langgraph._tool_inventory.match_tool_inventory`
        to compare the two.
        """
        return self._inventory

    def enforcement_report(self) -> ToolEnforcementReport:
        """Report what this installation governs, and the level that honestly supports.

        The middleware's own report, through the one
        :func:`~zeroth.integrations.langgraph._tool_inventory.report_tool_enforcement`
        the wrapper surface reports through -- so neither surface can claim a
        level the other could not.

        **It can never be
        :attr:`~zeroth.core.langgraph_gateway.models.GovernanceLevel.ENFORCED`.**
        That level needs signed, fresh, ``tool_manifest_complete`` run evidence;
        nothing in this package mints any, and this middleware mints none either.
        A tool-only run reports
        :attr:`~zeroth.core.langgraph_gateway.models.GovernanceLevel.OBSERVED`
        with ``partial`` coverage and an explicit list of the tools declared
        governed, or
        :attr:`~zeroth.core.langgraph_gateway.models.GovernanceLevel.ADMISSION`
        when none were.

        **The inventory is a description, never a gate.** A call naming a tool
        nobody declared is decided exactly as any other call is: the enforcement
        core is the only thing that refuses a call, and adding a second refusal
        here would be the second implementation this package exists without.

        Returns:
            What this installation enforces, and the level that supports.
        """
        return report_tool_enforcement(self._inventory)

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
        and this method never retries a call.

        **That is a claim about the handler, not about the tool body.** How many
        times the body runs beneath the handler is decided by whatever layers are
        nested inside this one: a retrying middleware declared *after*
        ``ZerothMiddleware`` executes the tool repeatedly against this single
        decision. Installed last, as required, this method is re-entered per
        physical execution and each one is decided and recorded on its own.

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
        of step with the sync one -- including the ordering contract: the async
        chain composes identically (``_chain_async_tool_call_wrappers`` mirrors
        the sync ``compose_two``), so an awaited handler is one decision too, and
        the tool body underneath it is whatever the inner layers run.

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
