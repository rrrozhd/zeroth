"""Governed wrappers for raw tool lists: ``govern_tools`` and what it returns.

**This module composes; it decides nothing.** Every allow, deny and approval
branch lives in :mod:`~zeroth.integrations.langgraph._tool_guard`, and the
wrappers below call it: the sync surfaces call
:func:`~zeroth.integrations.langgraph._tool_guard.guard_tool_call`, the async
surfaces call :func:`~zeroth.integrations.langgraph._tool_guard.authorize_tool_call`
and then ``await`` their own downstream. There is deliberately no async
enforcement core, because two implementations of "may this tool run" is two
places for the fail-closed rules to diverge -- and the one that drifts is the one
that fails open.

**Wrapping mutates nothing.** The original tool object is never written to, its
``.func`` / ``.coroutine`` are never reassigned, and the supplied container is
copied rather than governed in place. A wrapper that rebound ``tool.func`` would
leave ``is``-identity intact while silently governing -- or breaking -- every
other holder of that tool, which is exactly the failure ``is``-only assertions
cannot see.

**``_run`` / ``_arun`` are the choke point, not ``invoke``.** ``BaseTool.invoke``,
``.run``, ``.ainvoke`` and ``.arun`` all funnel through them (and the inherited
``_arun`` funnels back into ``_run``), so overriding the pair governs every entry
point at once. ``BaseTool`` exposes no instance ``__call__`` in ``langchain-core``
1.x, so there is no fifth door.

**One validation, so the authorized call is the executed call.** ``BaseTool``
validates its input against ``args_schema`` before it reaches ``_run``, and the
wrapper carries the delegate's schema, so the wrapper's own parse is where that
validation happens. Handing the *parsed* arguments back through the delegate's
public ``invoke`` would validate them a second time, and a validator that is
stateful or otherwise non-idempotent answers differently on the second pass -- so
policy would authorize the first answer while the body ran on the second. The
delegate is therefore driven through :func:`_executing_delegate`, a twin of it
whose validation stage is a pass-through, and the values the body receives are
exactly the values the decision was made about. See that function for why a twin
rather than the delegate itself, and :func:`_delegate_input` for why the twin is
still driven through ``invoke``.

**Identity is pinned at wrap time and re-derived at every call.** A tool whose
name or declared schema moves between the wrapping and the call cannot carry a
reproducible decision, so the mismatch raises
:class:`~zeroth.integrations.langgraph._tool_errors.UnstableToolIdentityError`
rather than being decided against an identity that will not hold. That is also
what stops a hostile ``__getattr__`` from presenting one identity to the wrapper
and another to the policy.

**Dispatch is by ``isinstance``; safety is not.** Every real tool is a
``BaseTool`` *subclass* -- ``StructuredTool`` is what ``@tool`` produces -- so an
exact-type gate here would reject the entire framework. The hostile-subtype
defense is therefore on the *values*: names and descriptions pass the same
``type(x) is str`` gates as everywhere else in this package, every attribute is
read through a helper that treats a raising property as absent, and no container
the caller owns is trusted or retained.

**Coverage is hard-coded to
:attr:`~zeroth.integrations.langgraph._tool_types.InventoryCoverage.PARTIAL`.**
``govern_tools`` takes no coverage parameter on purpose: declaring a complete
inventory requires an explicit expected tool list whose fingerprints match at
startup, and a parameter would let a caller assert completeness nothing verified.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import BaseTool

from zeroth.governance.identity import ActorIdentity
from zeroth.integrations.langgraph._tool_decisions import (
    ToolDecisionClient,
    UnknownSideEffectPolicy,
)
from zeroth.integrations.langgraph._tool_errors import (
    GovernanceContextError,
    ToolGovernanceError,
    UnstableToolIdentityError,
)
from zeroth.integrations.langgraph._tool_guard import (
    ToolAuditSubmitter,
    authorize_tool_call,
    guard_tool_call,
)
from zeroth.integrations.langgraph._tool_normalize import (
    classify_side_effect,
    normalize_contract_ref,
    normalize_identifier,
    normalize_tool_action,
    normalize_tool_identity,
)
from zeroth.integrations.langgraph._tool_types import (
    InventoryCoverage,
    SideEffectClass,
    ToolAction,
    ToolGovernanceContext,
    ToolIdentity,
)

_POSITIONAL_ARGUMENT_TEMPLATE = "__arg{index}"
"""How a positional tool argument is named for the policy that decides it.

``langchain_core`` already spells a schema-less tool's single input ``__arg1``
when it renders one as an LLM function call
(``langchain_core/utils/function_calling.py``), so a governed call reads the same
whether it arrived positionally or as a structured tool call. Only a schema-less
``BaseTool`` and a callable with no readable signature ever reach this naming;
everything else is decided under the argument's real name.
"""

_TOOL_CALL_ID_KEY = "__zeroth_tool_call_id__"
"""The reserved keyword the parsed tool-call id rides into ``_run`` under.

``BaseTool`` gives the id to ``_to_args_and_kwargs`` and to nothing downstream of
it, so this is the seam that carries it the last step. A tool that declares an
argument under this exact name has its call refused rather than silently
overwritten.
"""

_BASE_TOOL_SURFACE = "base_tool"
_CALLABLE_SURFACE = "callable"
"""Which wrapping surface an identity was derived through.

Part of the fingerprint material so that the same underlying function, governed
once as a bound tool and once as a bare callable, does not collapse onto one
identity: the two carry different declared schemas and are therefore different
things to write a policy against.
"""


@dataclass(frozen=True, slots=True)
class GovernedToolBinding:
    """What ``govern_tools`` observed about one tool before any call was made.

    Attached to every wrapper as ``zeroth_binding`` so the inventory stage can
    report what was governed without re-deriving any of it.

    **Only ``identity`` is an authorization fact.** It is pinned here and
    re-derived on every call, and a call whose identity no longer matches is
    refused. ``side_effect`` and ``contract_ref`` are the *inventory's* reading of
    the tool at wrap time and nothing decides against them:
    :func:`_governed_action` re-runs the resolvers per call, so a tool that
    becomes side-effecting after it was wrapped is decided as what it is now, not
    as what it was.

    Attributes:
        identity: The name and fingerprint the tool is decided under.
        side_effect: How the classifier rated the tool when it was wrapped,
            defaulting to unknown -- which the default policy denies.
        contract_ref: The contract the tool was bound to when it was wrapped,
            when one was declared.
        coverage: What this wrapping can support.
            ``govern_tools`` never sets it to
            :attr:`~zeroth.integrations.langgraph._tool_types.InventoryCoverage.COMPLETE`,
            because completeness is a claim about tools nobody passed in.
    """

    identity: ToolIdentity
    side_effect: SideEffectClass = SideEffectClass.UNKNOWN
    contract_ref: str | None = None
    coverage: InventoryCoverage = InventoryCoverage.PARTIAL


@dataclass(frozen=True, slots=True)
class _ToolFacts:
    """The identifying surface read off one foreign tool object, already gated."""

    name: object
    description: str
    args_schema: Any
    material: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class _Seams:
    """Everything ``govern_tools`` was handed that a call is decided through."""

    context: object = None
    client: ToolDecisionClient | None = None
    unknown_side_effect: UnknownSideEffectPolicy = UnknownSideEffectPolicy.DENY
    audit: ToolAuditSubmitter | None = None
    actor: ActorIdentity | None = None
    interrupt: Callable[[Mapping[str, Any]], Any] | None = None
    side_effect: Callable[[Any], Any] | None = None
    contract_ref: Callable[[Any], Any] | None = None


@dataclass(frozen=True, slots=True)
class _GovernedPlan:
    """One wrapper's pinned identity plus the live seams it enforces through."""

    target: Any
    describe: Callable[[Any], _ToolFacts]
    binding: GovernedToolBinding
    seams: _Seams


def _peek(source: object, attribute: str) -> Any:
    """Read one attribute off a foreign object, treating any failure as absent.

    A tool is allowed to expose ``args_schema`` as a property, and a property is
    allowed to raise. Nothing about reading a tool's surface should be able to
    fail a run in an untyped way, so a raise here means "the tool declared
    nothing", never a traceback out of the wrapping.

    Args:
        source: The object to read.
        attribute: The attribute name.

    Returns:
        The attribute's value, or ``None``.
    """
    try:
        return getattr(source, attribute)
    except Exception:
        return None


def _text(value: object) -> str:
    """Return *value* only when it is exactly a ``str``, else the empty string.

    A ``str`` subclass is not text here for the same reason it is not an
    identifier anywhere else in this package: it can substitute its own content
    at render time, and a description is rendered into a model prompt.
    """
    return value if type(value) is str else ""


def _flag(value: object) -> bool:
    """Return *value* only when it is exactly a ``bool``, else ``False``."""
    return value if type(value) is bool else False


def _string_list(value: object) -> list[str] | None:
    """Copy an exact ``list`` down to its exactly-``str`` items, or report none."""
    if type(value) is not list:
        return None
    return [item for item in value if type(item) is str]


def _string_keyed(value: object) -> dict[str, Any] | None:
    """Copy an exact ``dict`` down to its exactly-``str`` keys, or report none."""
    if type(value) is not dict:
        return None
    return {key: item for key, item in value.items() if type(key) is str}


def _error_handler(value: object) -> Any:
    """Return an error-handling setting in one of the shapes ``BaseTool`` accepts."""
    if type(value) is bool or type(value) is str:
        return value
    return value if callable(value) else False


def _carried_fields(tool: Any) -> dict[str, Any]:
    """Copy the ``BaseTool`` fields a *caller* reads off the wrapper, each exact-type gated.

    Whether a field has to be carried is settled by who reads it. The agent loop
    reads ``return_direct`` off the tool object it was handed -- the wrapper --
    and never off the delegate it cannot see, so leaving it at its default
    silently changes control flow. ``handle_validation_error`` belongs to the
    layer that parses the input, and the wrapper parses first, with the same
    schema; if it raises, the delegate is never reached to handle anything.

    ``callbacks``, ``handle_tool_error`` and ``response_format`` are deliberately
    *not* carried, for one reason: the delegate is the layer that runs, formats
    and therefore fails. Carrying the first would fire every handler twice, the
    second would hide a failure the delegate already handled, and the third would
    make the wrapper re-format an output the delegate had already formatted --
    which, for ``content_and_artifact``, means rejecting the delegate's own
    ``ToolMessage`` for not being a two-tuple. The delegate is handed the whole
    tool call, artifact and all, and its result travels back untouched.

    Args:
        tool: The tool being wrapped.

    Returns:
        Constructor keyword arguments for the governed twin.
    """
    return {
        "return_direct": _flag(_peek(tool, "return_direct")),
        "tags": _string_list(_peek(tool, "tags")),
        "metadata": _string_keyed(_peek(tool, "metadata")),
        "handle_validation_error": _error_handler(_peek(tool, "handle_validation_error")),
    }


def _schema_fields(schema: object) -> list[Any]:
    """List the argument names an ``args_schema`` declares, in whatever form it takes."""
    if type(schema) is dict:
        properties: object = None
        for key, value in schema.items():
            if key == "properties":
                properties = value
        return list(properties) if type(properties) is dict else []
    fields = _peek(schema, "model_fields")
    return list(fields) if type(fields) is dict else []


def _schema_argument_names(schema: object) -> list[str]:
    """Return an ``args_schema``'s argument names, sorted, or nothing.

    Sorted because the result is fingerprint material and the fingerprint is
    re-derived on every call: a declaration order that varied between
    observations would make a stable tool look like a moving one and refuse its
    second invocation.

    Args:
        schema: The tool's declared argument schema, in any form.

    Returns:
        The exactly-``str`` argument names, sorted.
    """
    try:
        fields = _schema_fields(schema)
    except Exception:
        return []
    return sorted({name for name in fields if type(name) is str})


def _signature_argument_names(target: object) -> list[str]:
    """Return a callable's parameter names, or nothing when it has no readable signature."""
    try:
        parameters = inspect.signature(target).parameters
    except (TypeError, ValueError):
        return []
    return sorted(name for name in parameters if type(name) is str)


def _describe_base_tool(tool: Any) -> _ToolFacts:
    """Read a ``BaseTool``'s identifying surface without trusting any of it."""
    description = _text(_peek(tool, "description"))
    args_schema = _peek(tool, "args_schema")
    return _ToolFacts(
        name=_peek(tool, "name"),
        description=description,
        args_schema=args_schema,
        material={
            "surface": _BASE_TOOL_SURFACE,
            "description": description,
            "arguments": _schema_argument_names(args_schema),
        },
    )


def _describe_callable(target: Any) -> _ToolFacts:
    """Read a plain callable's identifying surface, falling back to its ``__name__``.

    A callable may carry an explicit ``name`` / ``description`` / ``args_schema``
    -- a partially decorated function does -- so those are preferred, and the
    dunders stand in only where they are absent.
    """
    description = _text(_peek(target, "description")) or _text(_peek(target, "__doc__"))
    args_schema = _peek(target, "args_schema")
    name = _peek(target, "name")
    if normalize_identifier(name) is None:
        name = _peek(target, "__name__")
    arguments = _schema_argument_names(args_schema) or _signature_argument_names(target)
    return _ToolFacts(
        name=name,
        description=description,
        args_schema=args_schema,
        material={
            "surface": _CALLABLE_SURFACE,
            "description": description,
            "arguments": arguments,
        },
    )


def _resolved(resolver: Callable[[Any], Any] | None, target: Any) -> object:
    """Run an optional per-tool resolver, treating any failure as "it said nothing".

    A classifier that raises has not classified the tool, and an unclassified
    tool is denied by default. Turning the raise into a run failure would make a
    broken classifier louder than the denial it should have produced.
    """
    if resolver is None:
        return None
    try:
        return resolver(target)
    except Exception:
        return None


def _pin(target: Any, facts: _ToolFacts, seams: _Seams) -> GovernedToolBinding:
    """Fix the identity this tool is decided under, and observe the rest for the inventory.

    The identity is the pin: every call re-derives it and refuses if it moved.
    The classification and the contract are read once here for reporting only --
    :func:`_governed_action` resolves both again on every call, so a change
    between the wrapping and the call reaches the policy.

    Args:
        target: The tool being wrapped.
        facts: Its already-gated identifying surface.
        seams: The wrapping seams, including the optional resolvers.

    Returns:
        The binding whose identity every call through the wrapper is checked
        against.

    Raises:
        UnstableToolIdentityError: If the tool carries no usable name, or its
            identifying material is not canonically representable.
    """
    return GovernedToolBinding(
        identity=normalize_tool_identity(facts.name, facts.material),
        side_effect=classify_side_effect(_resolved(seams.side_effect, target)),
        contract_ref=normalize_contract_ref(_resolved(seams.contract_ref, target)),
    )


def _resolve_context(source: object) -> object:
    """Resolve the governance context, calling a provider seam when one was given.

    A context fixed at wrapping time cannot describe a tool list that outlives
    one run, so a zero-argument callable is accepted and invoked per call. What
    it returns is not trusted: the enforcement core re-normalizes it, and
    anything unusable -- including the ``None`` a caller who supplied no context
    at all leaves behind -- refuses the call.
    """
    if type(source) is ToolGovernanceContext:
        return source
    if callable(source):
        try:
            return source()
        except Exception as error:
            raise GovernanceContextError("the governance context provider failed") from error
    return source


def _call_arguments(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> dict[str, Any]:
    """Name one call's arguments so a policy sees exactly what the tool will run with.

    Args:
        args: The positional arguments, named ``__arg1`` onward.
        kwargs: The named arguments.

    Returns:
        A private mapping of argument name to value.

    Raises:
        ToolGovernanceError: If a named argument's key is not exactly ``str``, or
            a positional argument's synthesized name collides with a named one.
            A collision is refused rather than merged: one of the two values
            would otherwise vanish, and a policy that never saw an argument
            cannot deny it.
    """
    arguments: dict[str, Any] = {}
    for key, value in kwargs.items():
        if type(key) is not str:
            raise ToolGovernanceError("tool argument keys must be exactly str")
        arguments[key] = value
    for index, value in enumerate(args, start=1):
        name = _POSITIONAL_ARGUMENT_TEMPLATE.format(index=index)
        if any(existing == name for existing in arguments):
            raise ToolGovernanceError("a positional tool argument collides with a named one")
        arguments[name] = value
    return arguments


def _callable_arguments(
    target: Any, args: tuple[Any, ...], kwargs: Mapping[str, Any]
) -> dict[str, Any]:
    """Name a plain callable's arguments the way a structured tool call would.

    Binding against the callable's own signature is what keeps this surface and
    the middleware surface describing the same call: the middleware is handed
    named arguments and never positional ones, so a wrapper that reported
    ``__arg1`` for ``search("cats")`` would decide a differently-shaped call than
    the middleware decides for the same tool. Only a callable with no retrievable
    signature -- a builtin, most C functions -- falls back to positional naming.
    """
    try:
        bound = inspect.signature(target).bind(*args, **kwargs)
    except (TypeError, ValueError):
        return _call_arguments(args, kwargs)
    return _call_arguments((), bound.arguments)


def _governed_action(
    plan: _GovernedPlan, arguments: Mapping[str, Any]
) -> tuple[ToolAction, object]:
    """Rebuild the decided descriptor from the live tool, refusing an identity that moved.

    **Every authorization fact is resolved now, not at wrap time.** The
    classification and the contract binding are re-read from the live resolvers
    on each call, exactly as
    :meth:`~zeroth.integrations.langgraph._middleware.ZerothMiddleware._describe`
    reads them, for two reasons that point the same way. The first is R8: two
    surfaces that resolve the same fact at different *times* decide the same tool
    differently the moment the fact moves, and a fact pinned before the tool
    became side-effecting is the one that permits. The second is that staleness
    here is always the unsafe direction -- a classification cached from when the
    tool was read-only outlives the change that made it dangerous.

    The wrap-time values on :class:`GovernedToolBinding` survive as the
    inventory's *observation* of the tool and are deliberately not consulted
    here.

    Args:
        plan: The wrapper's pinned identity and live seams.
        arguments: The named call arguments.

    Returns:
        The normalized action and the governance context it was attributed to,
        both handed on to the enforcement core unchanged.

    Raises:
        UnstableToolIdentityError: If the tool's identity is not the one it was
            wrapped under.
        GovernanceContextError: If the call cannot be attributed.
        ToolGovernanceError: If the arguments are not canonically representable.
    """
    context = _resolve_context(plan.seams.context)
    facts = plan.describe(plan.target)
    action = normalize_tool_action(
        name=facts.name,
        arguments=arguments,
        context=context,
        identity_material=facts.material,
        contract_ref=_resolved(plan.seams.contract_ref, plan.target),
        side_effect=_resolved(plan.seams.side_effect, plan.target),
    )
    if action.identity != plan.binding.identity:
        raise UnstableToolIdentityError("the tool's identity changed after it was governed")
    return action, context


def _enforcement_seams(plan: _GovernedPlan) -> dict[str, Any]:
    """Render the plan's seams as the keyword arguments the enforcement core takes."""
    seams = plan.seams
    return {
        "client": seams.client,
        "unknown_side_effect": seams.unknown_side_effect,
        "audit": seams.audit,
        "actor": seams.actor,
        "interrupt": seams.interrupt,
    }


def _delegate_input(
    args: tuple[Any, ...], kwargs: Mapping[str, Any], tool_call_id: str | None, name: str
) -> Any:
    """Rebuild the input the original tool's own ``invoke`` expects.

    When the governed call arrived as a tool call, the delegate is handed a tool
    call too, id and all. That is what keeps a ``content_and_artifact`` tool's
    artifact alive through the wrapping: the delegate builds the whole
    ``ToolMessage`` itself, and a ``ToolMessage`` travels back out of the
    wrapper's own formatting stage untouched. Rebuilding a bare argument dict
    instead would leave the delegate with no call id, and a delegate with no call
    id returns content and drops its artifact on the floor.

    Args:
        args: The parsed positional arguments.
        kwargs: The parsed named arguments.
        tool_call_id: The id of the tool call being governed, when there is one.
        name: The tool's name, as it goes into a rebuilt tool call.

    Returns:
        The input to hand the delegate.

    Raises:
        ToolGovernanceError: If the call shape cannot be handed on faithfully.
            Guessing would invoke the tool with something other than what was
            decided.
    """
    if args and not kwargs:
        if len(args) != 1:
            raise ToolGovernanceError("a governed tool call carries more than one positional input")
        return args[0]
    if args:
        raise ToolGovernanceError("a governed tool call mixes positional and named inputs")
    arguments = {key: value for key, value in kwargs.items()}
    if tool_call_id is None:
        return arguments
    return {"name": name, "args": arguments, "id": tool_call_id, "type": "tool_call"}


def _executing_delegate(delegate: Any) -> Any:
    """Return a twin of *delegate* that runs the arguments it is handed, unvalidated.

    The wrapper has already parsed the call against the delegate's own
    ``args_schema`` -- it carries that schema -- and the policy was asked about
    the result. Invoking the delegate itself would run ``_parse_input`` over
    those values a second time, and only a *pure* validator is guaranteed to
    answer the same thing twice: a stateful one, a validator that reads the
    clock, or one that consumes a nonce returns something else, and the tool then
    executes arguments no policy ever saw. Clearing ``args_schema`` on the twin
    makes ``BaseTool._parse_input`` a pass-through (``langchain_core/tools/base.py``
    returns its input untouched when there is no schema), so exactly one
    validation happens per governed call and the body receives exactly the values
    the decision was made about.

    A *twin* rather than the delegate, because governing a tool must not change
    it: rebinding ``args_schema`` on the original would strip validation from
    every other holder of that tool. A twin *per call* rather than one pinned at
    wrap time, because the pinned copy would stop tracking a delegate whose
    ``func`` moved, which is the behaviour invoking the delegate directly has
    today.

    The twin keeps everything else -- ``name``, ``response_format``,
    ``callbacks``, ``handle_tool_error`` -- so it still builds its own
    ``ToolMessage`` and a ``content_and_artifact`` tool's artifact still survives
    the wrapping.

    Args:
        delegate: The wrapped tool.

    Returns:
        The tool object to invoke for this call.

    Raises:
        ToolGovernanceError: If the delegate cannot be copied. Refusing is the
            only outcome that does not run the tool against arguments the guard
            could not pin, and it happens after the decision was recorded.
    """
    try:
        return delegate.model_copy(update={"args_schema": None})
    except Exception as error:
        raise ToolGovernanceError(
            "this tool cannot be driven with the arguments its call was authorized under"
        ) from error


class GovernedTool(BaseTool):
    """A ``BaseTool`` that decides before it delegates, and mutates nothing.

    Preserves ``name``, ``description`` and ``args_schema`` from the tool it
    wraps, and reports the wrapped tool's own input schema rather than one
    inferred from this class's ``_run`` signature -- so a schema-less tool stays
    schema-less through the wrapping instead of acquiring ``*args`` / ``**kwargs``.
    It carries the other fields a *caller* reads off a tool object rather than off
    the body behind it -- see :func:`_carried_fields` for which, and why the
    error-handling ones are not among them.

    Attributes:
        zeroth_delegate: The original tool. On an allow it is driven exactly once,
            through the pass-through twin :func:`_executing_delegate` builds, and
            through that twin's own public entry point.
        zeroth_plan: The pinned binding and the seams a call is decided through.
        zeroth_binding: What ``govern_tools`` pinned about the wrapped tool.
    """

    zeroth_delegate: Any = None
    zeroth_plan: Any = None
    zeroth_binding: Any = None

    def get_input_schema(self, config: Any = None) -> Any:
        """Report the delegate's own input schema, not this wrapper's signature."""
        return self.zeroth_delegate.get_input_schema(config)

    def _to_args_and_kwargs(
        self, tool_input: Any, tool_call_id: str | None
    ) -> tuple[tuple[Any, ...], dict[str, Any]]:
        """Parse the input as ``BaseTool`` does, carrying the call id on to ``_run``.

        ``BaseTool`` hands the call id to this method and to nothing further, but
        the delegate needs it to format its own output. Threading it through the
        parsed keyword arguments is what makes it reachable without overriding
        ``run`` and ``arun`` wholesale.

        Raises:
            ToolGovernanceError: If the tool declares an argument under the
                reserved carrier name, which would otherwise be overwritten.
        """
        args, kwargs = super()._to_args_and_kwargs(tool_input, tool_call_id)
        if any(existing == _TOOL_CALL_ID_KEY for existing in kwargs):
            raise ToolGovernanceError("a tool argument collides with the tool-call id carrier")
        return args, {**kwargs, _TOOL_CALL_ID_KEY: tool_call_id}

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        """Govern this call, then invoke the wrapped tool exactly once on an allow.

        Every ``BaseTool`` entry point -- ``invoke``, ``run`` and the inherited
        ``_arun`` fallback -- funnels through here, so there is no way to reach
        the delegate without passing the guard.
        """
        plan = self.zeroth_plan
        call_id = kwargs.pop(_TOOL_CALL_ID_KEY, None)
        action, context = _governed_action(plan, _call_arguments(args, kwargs))
        delegate, name = self.zeroth_delegate, self.name
        return guard_tool_call(
            action,
            context,
            lambda: _executing_delegate(delegate).invoke(
                _delegate_input(args, kwargs, call_id, name)
            ),
            **_enforcement_seams(plan),
        )

    async def _arun(self, *args: Any, **kwargs: Any) -> Any:
        """Govern this call, then await the wrapped tool exactly once on an allow.

        Authorization is the same synchronous core the sync path runs; only the
        downstream invocation is awaited. There is no async enforcement branch to
        drift out of step with the sync one.
        """
        plan = self.zeroth_plan
        call_id = kwargs.pop(_TOOL_CALL_ID_KEY, None)
        action, context = _governed_action(plan, _call_arguments(args, kwargs))
        payload = _delegate_input(args, kwargs, call_id, self.name)
        authorize_tool_call(action, context, **_enforcement_seams(plan))
        return await _executing_delegate(self.zeroth_delegate).ainvoke(payload)


def _govern_base_tool(target: BaseTool, facts: _ToolFacts, plan: _GovernedPlan) -> GovernedTool:
    """Build the governed twin of a ``BaseTool``, leaving the original untouched.

    Raises:
        ToolGovernanceError: If the tool's declared surface will not build a
            wrapper -- an ``args_schema`` of a shape ``BaseTool`` rejects, say.
            Refusing is the only outcome that cannot leave an ungoverned tool in
            the returned list.
    """
    try:
        return GovernedTool(
            name=plan.binding.identity.name,
            description=facts.description,
            args_schema=facts.args_schema,
            zeroth_delegate=target,
            zeroth_plan=plan,
            zeroth_binding=plan.binding,
            **_carried_fields(target),
        )
    except ToolGovernanceError:
        raise
    except Exception as error:
        raise ToolGovernanceError("this tool's declared surface cannot be governed") from error


def _sync_callable_wrapper(target: Any, plan: _GovernedPlan) -> Any:
    """Return a governed function that calls *target* exactly once on an allow."""

    @functools.wraps(target)
    def governed(*args: Any, **kwargs: Any) -> Any:
        """Govern this call, then invoke the wrapped callable exactly once on an allow."""
        action, context = _governed_action(plan, _callable_arguments(target, args, kwargs))
        return guard_tool_call(
            action, context, lambda: target(*args, **kwargs), **_enforcement_seams(plan)
        )

    return governed


def _async_callable_wrapper(target: Any, plan: _GovernedPlan) -> Any:
    """Return a governed coroutine function that awaits *target* once on an allow."""

    @functools.wraps(target)
    async def governed(*args: Any, **kwargs: Any) -> Any:
        """Govern this call, then await the wrapped callable exactly once on an allow."""
        action, context = _governed_action(plan, _callable_arguments(target, args, kwargs))
        authorize_tool_call(action, context, **_enforcement_seams(plan))
        return await target(*args, **kwargs)

    return governed


def _govern_callable(target: Any, facts: _ToolFacts, plan: _GovernedPlan) -> Any:
    """Wrap a plain callable so that calling it decides first, calling it second.

    The wrapper stays directly callable -- that is a bare callable's whole
    interface -- and carries the tool-shaped attributes alongside, so a caller
    that reads ``name`` / ``description`` / ``args_schema`` off a tool list sees
    the same values it saw before.
    """
    if inspect.iscoroutinefunction(target):
        governed = _async_callable_wrapper(target, plan)
    else:
        governed = _sync_callable_wrapper(target, plan)
    governed.name = plan.binding.identity.name
    governed.description = facts.description
    governed.args_schema = facts.args_schema
    governed.zeroth_delegate = target
    governed.zeroth_plan = plan
    governed.zeroth_binding = plan.binding
    return governed


def _govern_one(target: Any, seams: _Seams) -> Any:
    """Wrap one tool, choosing the surface by what it is and pinning its identity.

    Args:
        target: A ``BaseTool`` instance or a plain callable.
        seams: The wrapping seams shared by every tool in this call.

    Returns:
        The governed wrapper.

    Raises:
        ToolGovernanceError: If *target* is neither a tool nor callable.
        UnstableToolIdentityError: If *target* carries no usable identity.
    """
    is_tool = isinstance(target, BaseTool)
    if not is_tool and not callable(target):
        raise ToolGovernanceError("govern_tools accepts BaseTool instances and plain callables")
    describe = _describe_base_tool if is_tool else _describe_callable
    facts = describe(target)
    plan = _GovernedPlan(
        target=target, describe=describe, binding=_pin(target, facts, seams), seams=seams
    )
    if is_tool:
        return _govern_base_tool(target, facts, plan)
    return _govern_callable(target, facts, plan)


def govern_tools(
    tools: Iterable[Any],
    *,
    context: object = None,
    client: ToolDecisionClient | None = None,
    unknown_side_effect: UnknownSideEffectPolicy = UnknownSideEffectPolicy.DENY,
    audit: ToolAuditSubmitter | None = None,
    actor: ActorIdentity | None = None,
    interrupt: Callable[[Mapping[str, Any]], Any] | None = None,
    side_effect: Callable[[Any], Any] | None = None,
    contract_ref: Callable[[Any], Any] | None = None,
) -> list[Any]:
    """Return governed twins of *tools*, without mutating any of them.

    **The install surface for a raw tool list.** The returned wrappers go where
    the originals went -- a ``ToolNode``, a ``StateGraph``, a bind call -- and
    each one is invocable through exactly the interfaces its original was: a
    ``BaseTool`` twin answers to ``invoke`` / ``ainvoke`` / ``run`` / ``arun``, a
    callable twin is called directly.

    Nothing about the originals changes. The supplied container is copied, no
    attribute is written back, and ``.func`` / ``.coroutine`` are never rebound,
    so any other holder of a wrapped tool keeps the ungoverned behaviour it had.

    **Supplying no context refuses every call, deliberately.** The principal is
    injected and never discovered, so a tool list wrapped without one is wrapped
    fail-closed rather than wrapped unattributed.

    Args:
        tools: The tools to govern: ``BaseTool`` instances, plain callables, or
            a mix. The iterable is consumed once, into a private list.
        context: The governance context each call is attributed to, or a
            zero-argument callable returning one per call. ``None`` refuses every
            call.
        client: The decision client, or ``None`` to deny for want of one.
        unknown_side_effect: Whether a tool nobody classified may be invoked.
        audit: Where each decision record is handed off, or ``None`` to decide
            without recording.
        actor: The authenticated actor to attribute records to, when there is one.
        interrupt: The pause seam, defaulting to LangGraph's ``interrupt``.
        side_effect: An optional per-tool classifier, re-run on every call so a
            tool that becomes side-effecting is decided as what it is now. Only a
            real :class:`~zeroth.integrations.langgraph._tool_types.SideEffectClass`
            member classifies a tool; anything else leaves it unknown, and
            unknown is denied unless *unknown_side_effect* says otherwise.
        contract_ref: An optional per-tool contract resolver, also re-run on
            every call.

    Returns:
        A new list of governed wrappers, in the order the tools were supplied.

    Raises:
        ToolGovernanceError: If *tools* is not iterable, or holds something that
            is neither a ``BaseTool`` nor callable.
        UnstableToolIdentityError: If a tool carries no usable identity.
    """
    try:
        supplied = list(tools)
    except TypeError as error:
        raise ToolGovernanceError("govern_tools needs an iterable of tools") from error
    seams = _Seams(
        context=context,
        client=client,
        unknown_side_effect=unknown_side_effect,
        audit=audit,
        actor=actor,
        interrupt=interrupt,
        side_effect=side_effect,
        contract_ref=contract_ref,
    )
    return [_govern_one(target, seams) for target in supplied]


__all__ = [
    "GovernedTool",
    "GovernedToolBinding",
    "govern_tools",
]
