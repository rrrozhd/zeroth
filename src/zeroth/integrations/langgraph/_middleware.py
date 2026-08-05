"""The middleware install surface: ``ZerothMiddleware`` for ``create_agent``.

**This module installs the wrapper surface; it decides nothing and enforces
nothing.** It holds no call to
:func:`~zeroth.integrations.langgraph._tool_guard.guard_tool_call` or
:func:`~zeroth.integrations.langgraph._tool_guard.authorize_tool_call` at all.
What :meth:`ZerothMiddleware.wrap_tool_call` does is substitute a *governed twin*
of ``request.tool`` -- built by the same
:func:`~zeroth.integrations.langgraph._tool_wrappers.govern_tools` machinery, per
call -- into the request it hands downstream. LangChain's ``ToolNode`` then
executes that twin, and the twin is the thing that decides. Both install
surfaces therefore enter the enforcement core at one point,
:meth:`~zeroth.integrations.langgraph._tool_wrappers.GovernedTool._run`, through
one implementation of the fail-closed rules (R8).

**The decision is made about the arguments the body will actually receive.** This
is the whole reason the twin exists. ``request.tool_call["args"]`` is what the
*model* emitted: raw JSON, before ``BaseTool`` coerces it against
``args_schema``, before a default is filled in, before ``ToolNode`` injects a
state or store argument. A middleware that decided from that mapping authorized
the string ``"7"`` while the body ran on the integer ``7`` -- and never saw an
injected argument at all. Substituting the twin puts validation, coercion,
defaulting and injection *ahead* of the guard, exactly once, so the authorized
call is the executed call.

**Not calling the downstream is no longer the enforcement, and that is a real
change.** The verdict now travels up *through* ``handler``: a denial raises
:class:`~zeroth.integrations.langgraph._tool_errors.PolicyViolation` from inside
the twin's ``_run``, and an approval interrupts there. ``handler`` is reached on
every verdict, so "the tool did not run" is measured at the tool *body*, never at
a handler count. ``ToolNode`` propagates both -- it renders no error
``ToolMessage`` for them -- which
``test_a_denied_call_raises_the_typed_error_out_of_a_real_agent`` and its async
and approval twins pin through a real ``create_agent``.

**A denial propagates; it is never rendered as an error ``ToolMessage``.** The
guard's contract is that governance decides *whether* a call happens, not what it
means. Callers who want a denial fed back to the model catch the exception in
their own middleware, outside governance.

**Nesting order (R12).** LangChain composes ``wrap_tool_call`` middleware with
the *first entry outermost*: for
``create_agent(..., middleware=[A, ZerothMiddleware(...), B])`` a tool call
enters ``A``, then ``ZerothMiddleware``, then ``B``, and unwinds in reverse. The
substitution happens at governance's own position, so ``A`` is handed the raw
tool and ``B`` is handed the governed twin -- which is how
``test_three_middleware_nest_first_defined_outermost`` observes the position now
that the decision itself happens below every layer.

**``ZerothMiddleware`` must be the LAST ``wrap_tool_call`` middleware in the
list.** One failure follows from nesting anything inside it, and it is not
detectable from here.

*Un-substitution.* A middleware nested inside receives the governed twin and can
hand its own downstream something else -- ``handler(request.override(tool=raw))``
-- and the raw tool then runs with **no decision and no audit record at all**.
Rewriting ``tool_call`` has the same shape. Anything that rewrites a request
belongs *outside* governance, where the substitution happens after it. The
limitation is pinned by
``test_a_middleware_nested_inside_governance_can_strip_the_governed_twin`` so it
can never quietly become a claim.

*A retry nested inside is no longer a hole.* LangChain hands each layer a handler
its own body may call **as many times as it likes** --
``_chain_tool_call_wrappers.compose_two`` in ``langchain/agents/factory.py`` says
so in as many words ("Outer can call call_inner multiple times"), and the shipped
``ToolRetryMiddleware`` does exactly that. Each of those calls now reaches the
twin, so each physical execution gets its own decision and its own record
whichever side of governance the retry sits on. Installed outermost or innermost,
the accounting is the same; only un-substitution defeats it.

**No supported mechanism detects the position, so nothing here tries.**
:class:`~langchain.agents.middleware.AgentMiddleware` exposes no hook that
receives the middleware list and ``create_agent`` composes the chain into a
closure the middleware never sees. A guard that silently stops guarding is worse
than a documented contract with tests behind it.

**Identity is read off the registered tool, not off the model's request.**
:class:`~langchain.agents.middleware.ToolCallRequest` carries both a ``tool_call``
the model produced and the ``tool`` the agent resolved it to. The twin is built
from the resolved tool through the same
``_describe_base_tool`` the wrapper surface uses -- so one tool fingerprints
identically on both surfaces -- and the requested name must match the name the
twin was pinned under, because a request naming one tool while carrying another
is a request whose decision would be recorded against the wrong thing.

**An unregistered tool is refused, not decided.** ``request.tool`` is ``None``
when the agent could not resolve the call, and a tool with no declared surface
has no material to pin an identity against. It raises
:class:`~zeroth.integrations.langgraph._tool_errors.UnstableToolIdentityError`
rather than being decided against a name alone.

**A tool whose entry path governance cannot execute past is refused here too.**
Building the twin runs
:func:`~zeroth.integrations.langgraph._tool_execution.refuse_delegate_dispatch`,
so a delegate that overrides ``_parse_input`` -- or ``invoke``, or any other
pre-body hook -- is refused on this surface exactly as it is on the wrapper
surface. An **already-governed** tool is refused by the same gate, because
:class:`~zeroth.integrations.langgraph._tool_wrappers.GovernedTool` overrides
``_to_args_and_kwargs``: governing a governed tool twice is a configuration
error, and refusing it is what stops one call carrying two decisions and two
records.

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
from zeroth.integrations.langgraph._tool_guard import ToolAuditSubmitter
from zeroth.integrations.langgraph._tool_inventory import (
    ToolEnforcementReport,
    record_binding_inventory,
    report_tool_enforcement,
)
from zeroth.integrations.langgraph._tool_normalize import normalize_identifier
from zeroth.integrations.langgraph._tool_types import ToolInventory
from zeroth.integrations.langgraph._tool_wrappers import (
    GovernedToolBinding,
    _describe_base_tool,
    _govern_one,
    _peek,
    _pin,
    _Seams,
)

_TOOL_CALL_NAME = "name"
_TOOL_CALL_ARGUMENTS = "args"
_TOOL_CALL_ID = "id"
"""The ``ToolCall`` keys a decision and its stable per-call identity are read from.

Read by iterating the mapping once rather than by keying into it, so an object
that answers one thing to iteration and another to a lookup cannot present a
benign call to the gate and a different one to the tool.
"""


def _requested_call(request: object) -> tuple[object, object, str | None]:
    """Read the model's requested tool name and arguments off a middleware request.

    Args:
        request: The middleware request, which is not trusted to be one.

    Returns:
        The requested name and arguments, both still ungated, plus the validated
        stable tool-call identifier when the framework supplied one.

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
    call_id: object = None
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
        elif key == _TOOL_CALL_ID:
            call_id = value
    normalized_call_id = None if call_id is None else normalize_identifier(call_id)
    if call_id is not None and (normalized_call_id is None or normalized_call_id != call_id):
        raise UnstableToolIdentityError("a governed middleware call needs a stable tool-call id")
    return name, arguments, normalized_call_id


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


def _declared_binding(tool: object, seams: _Seams) -> GovernedToolBinding:
    """Pin one declared tool the way a call through it will be described.

    Through ``_pin`` and ``_describe_base_tool``, which is what makes the
    inventory's fingerprint the fingerprint the *decision* is made under: an
    inventory derived any other way could name a tool the guard would refuse to
    recognize, and the report would be about tools nothing actually governs.

    Authorization metadata is resolved into the same immutable binding the
    inventory records. A per-call twin must still match it before execution.

    Args:
        tool: The declared tool, which is not trusted to be one.
        seams: The metadata resolvers whose normalized answers are reviewed.

    Returns:
        What was pinned about the tool, for the inventory only.

    Raises:
        UnstableToolIdentityError: If *tool* is not a ``BaseTool`` -- the same
            gate a live call passes, so a declared list cannot hold something the
            middleware could never be handed -- or carries no usable identity.
    """
    if not isinstance(tool, BaseTool):
        raise UnstableToolIdentityError("a declared tool must be a resolved BaseTool")
    return _pin(_describe_base_tool(tool), tool, seams)


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


def _carrying(request: object, twin: BaseTool) -> Any:
    """Rewrite one request onto the governed twin, refusing a rewrite that did not take.

    ``ToolCallRequest.override`` is LangChain's own copy-with-replacement, and it
    is the only supported way to change what the downstream executes. Two things
    are checked rather than assumed, because this substitution *is* the
    enforcement now: a request that cannot be rewritten has no governed path
    downstream, and a request whose ``tool`` came back as something other than
    the twin would run ungoverned while every count in this package still read as
    if it had not.

    The check is an identity refusal, not a second policy decision -- it decides
    nothing about the call, it only refuses to hand on a request that would
    escape the one decision.

    Args:
        request: The middleware request, which is not trusted to be one.
        twin: The governed tool the downstream must execute.

    Returns:
        The request to hand the downstream handler.

    Raises:
        UnstableToolIdentityError: If the request cannot carry the governed tool,
            or did not.
    """
    override = _peek(request, "override")
    if not callable(override):
        raise UnstableToolIdentityError("a governed tool call needs a rewritable request")
    try:
        carried = override(tool=twin)
    except Exception as error:
        raise UnstableToolIdentityError(
            "this tool call could not be rewritten onto its governed tool"
        ) from error
    if _peek(carried, "tool") is not twin:
        raise UnstableToolIdentityError("this tool call did not carry its governed tool downstream")
    return carried


class ZerothMiddleware(AgentMiddleware):
    """Governs every tool call a ``create_agent`` agent makes, deciding before it runs.

    **The second install surface for tool enforcement, and the second only in the
    sense of where it is installed.** Pass it to
    ``create_agent(..., middleware=[ZerothMiddleware(context=...)])`` and every
    tool call the agent makes runs through a per-call governed twin built by the
    same :func:`~zeroth.integrations.langgraph._tool_wrappers.govern_tools`
    machinery. Allow, deny and approval mean exactly what they mean there,
    because this class carries no decision of its own to mean anything else with.

    **Supplying no context refuses every call, deliberately.** The principal is
    injected and never discovered, so an agent governed without one is governed
    fail-closed rather than governed unattributed.

    **Install it LAST.** ``middleware=[...everything else..., ZerothMiddleware()]``
    makes it the innermost ``wrap_tool_call`` layer, which is what stops a nested
    layer from handing its own downstream a request the twin was taken back out
    of. See the module docstring for that failure, and for why no supported
    mechanism detects the position.

    **Do not hand it tools that ``govern_tools`` already wrapped.** Governing a
    governed tool is refused at every call, on this surface as on the other:
    pick one install surface per tool list.

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
        capability_refs: Callable[[Any], Any] | None = None,
        requires_approval: Callable[[Any], Any] | None = None,
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
            capability_refs: An optional per-tool required-capability resolver.
            requires_approval: An optional per-tool explicit-approval resolver.
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
        self._capability_refs = capability_refs
        self._requires_approval = requires_approval
        # Materialized before anything is pinned, so a ``TypeError`` raised
        # *inside* the pinning cannot be reported as "the list was not iterable".
        try:
            declared = list(expected_tools)
        except TypeError as error:
            raise ToolGovernanceError("an expected tool list must be iterable") from error
        seams = self._seams()
        self._inventory = record_binding_inventory(
            [_declared_binding(tool, seams) for tool in declared]
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

        **The inventory is not an allowlist.** A call naming a tool nobody
        declared is still decided normally. For a declared tool, its reviewed
        metadata is an integrity binding: a later mismatch refuses the call
        before policy evaluation.

        Returns:
            What this installation enforces, and the level that supports.
        """
        return report_tool_enforcement(self._inventory)

    def _seams(self, *, tool_call_id: str | None = None) -> _Seams:
        """Render the pinned seams as the wrapping surface's own seam record.

        Every field is handed straight through, so a tool governed by this
        middleware is governed under exactly the seams
        :func:`~zeroth.integrations.langgraph._tool_wrappers.govern_tools` would
        have been given -- which is what makes the two surfaces' decisions
        comparable rather than merely similar.
        """
        return _Seams(
            context=self._context,
            client=self._client,
            unknown_side_effect=self._unknown_side_effect,
            audit=self._audit,
            actor=self._actor,
            interrupt=self._interrupt,
            side_effect=self._side_effect,
            contract_ref=self._contract_ref,
            capability_refs=self._capability_refs,
            requires_approval=self._requires_approval,
            tool_call_id=tool_call_id,
        )

    def _governed(self, request: object) -> Any:
        """Return the request to hand downstream, with a governed twin in the tool slot.

        The single place a request becomes a governed one: both the sync and the
        async surface call this, so the two cannot install the twin differently.

        Nothing here decides anything. The twin does, when ``ToolNode`` executes
        it -- after ``BaseTool`` has validated, coerced and defaulted the
        arguments, and after any injected argument has been resolved.

        **The name check is against the twin's pinned name, not the raw tool's.**
        That is the name the decision will be recorded under, so matching the
        request against anything else would leave a gap between the name that was
        checked and the name that was decided.

        Building the twin pins each tool-only metadata seam, then the wrapper
        rechecks it before the action. When the tool was declared in
        ``expected_tools``, the twin must also match that reviewed inventory entry.

        Args:
            request: The middleware request, which is not trusted to be one.

        Returns:
            The rewritten request, carrying the governed twin.

        Raises:
            UnstableToolIdentityError: If the request carries no resolved tool,
                no plain tool call, a name that is not the twin's, a tool whose
                entry path governance cannot execute past -- including one that
                is already governed -- or a request that will not carry the twin.
            ToolGovernanceError: If the tool's declared surface will not build a
                governed twin.
        """
        tool = _requested_tool(request)
        requested_name, _arguments, tool_call_id = _requested_call(request)
        twin = _govern_one(tool, self._seams(tool_call_id=tool_call_id))
        _matched_name(requested_name, _peek(twin, "name"))
        binding = _peek(twin, "zeroth_binding")
        [current] = record_binding_inventory([binding]).entries
        reviewed = next(
            (
                entry
                for entry in self._inventory.entries
                if entry.identity.name == current.identity.name
            ),
            None,
        )
        if reviewed is not None and current != reviewed:
            raise ToolGovernanceError("the tool metadata changed after it was reviewed")
        return _carrying(request, twin)

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        """Install the governed twin into this call and hand it downstream once.

        ``handler`` is called exactly once, on every verdict, outside any loop --
        this method neither decides nor retries. The decision happens *inside*
        it, when ``ToolNode`` executes the twin, so a denial or an approval
        travels back out through ``handler`` as the typed exception the wrapper
        surface raises.

        **"The tool did not run" is therefore a statement about the body, never
        about this call.** Every enforcement assertion counts executions of the
        tool function; a handler count would now read ``1`` on a denial.

        Args:
            request: The tool call the agent is about to make.
            handler: LangChain's downstream execution of that call.

        Returns:
            Whatever the handler returned.

        Raises:
            ToolGovernanceError: Whenever the call did not proceed -- raised
                either by the gates here or, through ``handler``, by the shared
                enforcement core. See
                :func:`~zeroth.integrations.langgraph._tool_guard.authorize_tool_call`
                for which subclass names which condition.
        """
        return handler(self._governed(request))

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """Install the governed twin into this call and await it downstream once.

        The same substitution the sync path makes, differing only in the await.
        There is no async enforcement branch here because there is no enforcement
        branch here at all: the twin's own ``_arun`` runs the one synchronous
        core, exactly as its ``_run`` does.

        Args:
            request: The tool call the agent is about to make.
            handler: LangChain's downstream execution of that call.

        Returns:
            Whatever the handler returned.

        Raises:
            ToolGovernanceError: Whenever the call did not proceed.
        """
        return await handler(self._governed(request))


__all__ = ["ZerothMiddleware"]
