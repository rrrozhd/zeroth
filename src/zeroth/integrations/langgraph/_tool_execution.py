"""Execute the body whose identity was authorized, and nothing the delegate chooses.

Three audit cycles converged on one invariant and kept finding it broken at a new
seam: **the body that runs must be the body whose identity was authorized, and
the arguments it receives must be the arguments the policy saw.** Each cycle
closed the vector that had been demonstrated and the next probe walked around it,
because the defence was a list of banned attributes and the attacker chose the
attribute. The list was never the problem; reading through the delegate at all
was.

Three properties fix that, and this module exists to hold all three:

**Every read is static.** ``getattr`` on a foreign object asks the object what it
would like to say. Identity material read that way is delegate-authored, so a
custom ``__getattribute__`` controls the fingerprint as easily as it controls the
body. :func:`static_class_attribute` walks the real ``__mro__`` through
``type``'s own descriptors and reads each class ``__dict__`` directly;
:func:`static_instance_field` reads the instance dictionary through
``object.__getattribute__``. Neither can be answered by delegate code, and
neither routes through a metaclass.

**The body is captured by value, before anything caller-supplied runs.**
:func:`snapshot_tool` is taken *before* the side-effect classifier, the contract
resolver and the decision client -- all of which are caller code the wrapper
invokes by design, all of which execute in the window between identity
derivation and execution. A snapshot taken after any of them describes a tool
that has already had the chance to become a different one. Execution then runs
:func:`executing_tool`, a ``StructuredTool`` **this module constructs**, carrying
the snapshotted callables. Its type is framework-owned, so it has no hostile
``__getattribute__``, no instance-dictionary shadow and no overridden
``model_copy``: there is no attribute on it the delegate can reach.

"By value" is meant literally, because a captured callable is not one. Storing
the delegate's own function object left ``body.__code__ = other`` as a swap that
moved nothing the snapshot pointed at -- same object, same field, same
signature, different body -- so every captured slot is rebuilt through
:func:`snapshot_callable` and the snapshot holds code nothing else can reach. A
class-defined ``_run``/``_arun`` is **bound here too**, by
:func:`types.MethodType`, rather than by calling the delegate's ``__get__``
during execution: a descriptor's ``__get__`` is delegate-written code that used
to run after all three resolvers and decide what the authorized call finally
invoked, which is the ``model_copy`` shape one attribute further down. Only the
three descriptor kinds Python's own binding is defined for are admitted; a tool
whose body is anything else is refused rather than bound by its own code.

**Nothing the delegate supplied runs between the decision and the body, and
nothing the framework invents is added to the call.** The body half of the
invariant was the whole story for three cycles; the argument half has the same
two-reads shape one layer out. The executing tool used to carry the delegate's
``callbacks``, so ``on_tool_start`` ran after approval and before
``BaseTool.run`` built the call -- and the mapping that hook is handed shares
every container one level down with the mapping the body receives, which made a
list the policy inspected as ``["safe"]`` a list the body ran on as
``["safe", "evil"]``. The field is no longer carried; see
:data:`_CARRIED_FIELDS`. Separately, ``StructuredTool`` reads the *body's* own
signature and injects a live callback manager under a declared ``callbacks``
parameter and the run configuration under a ``RunnableConfig``-annotated one, so
each half is installed behind :func:`_adapted`, whose ``(*args, **kwargs)``
surface neither injection matches. Both are deletions of the second read, not
guards around it.

The refusal table in :func:`refuse_delegate_dispatch` is kept as well, and is
deliberately *not* the load-bearing part. It fails a tool closed at wrap time and
again per call, which turns a hostile tool into an error rather than a silent
substitution; but a fix that consisted only of refusals would be the fourth
version of a list somebody walks around. The snapshot is what makes the guarantee
hold even when nothing is refused.

"By value" reaches everything a body *captured*, too. Rebuilding the outermost
callable and handing it the delegate's own closure tuple, keyword-default mapping
and ``partial`` left four ways to move what runs without moving anything the
snapshot points at: ``partial.func.__code__``, ``type(obj).__call__.__code__``, a
closure cell rebound to another function or another code object, and a keyword
default rewritten in the mapping the body resolves against. The walk in
:func:`snapshot_callable` therefore descends -- through exactly the shapes
:mod:`~zeroth.integrations.langgraph._tool_fingerprint` walks as implementation,
because it imports that module's own predicate to decide.

**What is still not claimed.** A snapshot pins the *code* a tool will run, not
the state that code reads: a body that consults a mutable attribute of its own
object is fingerprinted by its code, and the code is what executes. Four things
are on the state side of that line and are named here so they are read as the
disclosed boundary rather than found as the next omission: a rebuilt function
keeps its **module globals** by reference, and keeps every **closure cell that
holds state** -- only a cell holding implementation is detached -- so a body that
rebinds a counter or reads a client goes on reading whatever it holds when it
runs, because rebinding those would give a tool a frozen copy of the world and
break every tool that keeps state anywhere; a bound body keeps the **instance**
it was bound to, and a callable object's frozen ``__call__`` is re-bound to that
same instance, because binding to a copy would make a tool's own state invisible
to its own next call; a ``functools.partial``'s **bound arguments** are carried
across as the values they are, so a ``partial`` over a mutable client is governed
by the code of its ``func`` and not by what that client does next; and an
instance that **delegates to a mutable attribute of its own** is a tool whose
configuration moved, not one whose code did. All four are the same
declared-identity boundary
:mod:`~zeroth.integrations.langgraph._tool_fingerprint` documents -- identity
covers a tool's implementation, not its configuration -- and it is the boundary
the cookbook discloses.

Nothing on the *argument* side is on that boundary, and an earlier revision of
this paragraph said otherwise. It disclosed the handlers a caller attaches to
their own run as acceptable -- on the grounds that a caller who installs one is
attacking themselves, which is a different thing from a tool they do not control
doing it. **That was wrong, and it is withdrawn rather than superseded.** Who
installed a handler has no bearing on whether the body ran on the arguments the
policy inspected, and that second question is the whole invariant. Dropping
``callbacks`` from :data:`_CARRIED_FIELDS` closed the *delegate's* route to
``on_tool_start``; the executor was still invoked with no config at all, so it
inherited the outer run's handlers from ``var_child_runnable_config`` and fired
them again after the verdict, one shallow copy away from the mapping the body
receives. Both routes are shut now: the field list deletes the delegate's, and
:func:`~zeroth.integrations.langgraph._tool_wrappers._callback_free_config`
deletes the ambient one, at the two lines that invoke this module's executing
tool. The claim is consequently about the call rather than about whose code it
is: **no callback handler runs between the verdict and the body**, whoever
attached it.

Suppression stops at the executor. The governed twin is itself a ``BaseTool``,
and the handlers a caller attached to the run still see its start and its end,
exactly once, before the decision and after the body; that is observability, and
losing it would be its own failure rather than a stricter fix.

The suppression does reach further than the executor's own run, and that is a
cost rather than a second guarantee. The executor's child config is what a body's
context carries, so a handler the caller attached to the run no longer sees what
the *body* invokes either -- a nested tool, a model, a ``RunnableLambda``. Those
handlers fire after the body has started and cannot change the call it was
authorized with, so restoring them would be a separate change; it is written down
here, and pinned by a test, so that making it is a decision.

Two residuals are named rather than left to be found, and neither is offered as
acceptable. ``ensure_config`` calls ``.copy()`` on the ambient ``tags``,
``metadata`` and ``configurable`` *values*, so a caller object's method does
execute during the internal invoke -- it is handed no reference to the call, and
the body was measured receiving the authorized arguments unchanged, but it is
code and it runs there. And ``langchain_core``'s ``register_configure_hook``
registry is process-global and keyed off its own ``ContextVar``s, so a handler
registered through it is added to every callback manager in the process,
including this one; ``callbacks=[]`` does not remove it and emptying
``var_child_runnable_config`` does not either. Closing that one is not a config
argument at either invoke site.
"""

from __future__ import annotations

import functools
import types
from abc import ABCMeta
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic._internal._model_construction import ModelMetaclass

from zeroth.integrations.langgraph._tool_errors import (
    ToolGovernanceError,
    UnstableToolIdentityError,
)
from zeroth.integrations.langgraph._tool_fingerprint import (
    _guard_depth,
    _is_implementation,
)

# Both are imported rather than restated, and they are the fingerprint's own
# private names on purpose: what this module must freeze is *exactly* what that
# one walks as implementation, so the two have to be the same predicate rather
# than two spellings of one intention that drift apart at the next shape either
# of them learns about. ``_guard_depth`` carries ``_MAX_MATERIAL_DEPTH`` and the
# refusal that goes with it, so a self-referential closure terminates here on the
# same bound and with the same error it terminates on there.

_MRO_OF = type.__dict__["__mro__"].__get__
"""``type``'s own ``__mro__`` descriptor, so a metaclass cannot answer with another list."""

_DICT_OF = type.__dict__["__dict__"].__get__
"""``type``'s own ``__dict__`` descriptor, for the same reason."""

_ENTRY_HOOKS = ("_parse_input", "_to_args_and_kwargs", "invoke", "ainvoke", "run", "arun")
"""Every ``BaseTool`` hook that runs *before* the body and can rewrite its input."""

_COPY_HOOKS = ("model_copy", "__copy__", "__deepcopy__")
"""The copy machinery. Execution used to call ``model_copy`` *on the delegate*.

That is one attribute lookup on a foreign object, and the object chose what came
back: a same-schema tool with another body was authorized and executed under the
original's fingerprint. Nothing here calls them any more -- the executing tool is
constructed rather than copied -- and they are refused as well, so a tool that
overrides one is rejected instead of quietly taking the other path.
"""

_DISPATCH_HOOKS = ("__getattribute__", "__getattr__")
"""Attribute dispatch itself, which decides the answer to every other read.

``__getattribute__`` is defined by neither ``BaseTool``, ``StructuredTool`` nor
``pydantic``'s ``BaseModel``, so the permitted implementation is ``object``'s and
an override costs no legitimate tool. ``__getattr__`` *is* defined by
``BaseModel`` (private attributes, extras), so it is permitted by identity rather
than refused outright -- refusing it would reject every pydantic tool there is.
"""

_GOVERNED_HOOKS = _ENTRY_HOOKS + _COPY_HOOKS + _DISPATCH_HOOKS
"""Every name whose implementation governance has to know it can trust."""

_PERMITTED_METACLASSES = (type, ABCMeta, ModelMetaclass)
"""The metaclasses a governable tool's class may have.

A metaclass runs on class-attribute access, so an unknown one is another
delegate-controlled dispatch layer. These three are what ``langchain_core``'s
tools and plain Python classes actually use.
"""

_BODY_FIELDS = ("func", "coroutine")
"""The callables a ``StructuredTool``-shaped tool keeps its body in."""

_BODY_METHODS = ("_run", "_arun")
"""Where a hand-written ``BaseTool`` subclass keeps its body instead."""

_CARRIED_FIELDS = (
    "response_format",
    "handle_tool_error",
    "handle_validation_error",
    "metadata",
    "tags",
    "return_direct",
    "verbose",
)
"""Fields the *executing* tool needs so its output is shaped as the delegate's was.

``response_format`` is the load-bearing one: a ``content_and_artifact`` tool
builds its own ``ToolMessage`` with the artifact attached, and an executing tool
that dropped the setting would return bare content and lose the artifact.

**``callbacks`` was here and is deliberately gone.** Carrying it ran the
delegate's own handlers inside the post-approval executor -- ``on_tool_start``
fires after the decision and before ``BaseTool.run`` calls
``_to_args_and_kwargs``, and the mapping that hook is handed is a *shallow*
filtered copy of the tool input, so every container one level down is the same
object the body is about to receive. A handler that appended to a list the policy
had inspected as ``["safe"]`` therefore had the body run on ``["safe", "evil"]``,
with nothing reassigned and nothing to see on the tool. That is the same
two-reads shape as ``model_copy``, moved one layer out from the body to the
arguments: the value that was authorized and the value that executed came from
two separate reads, and the second one was reached through a framework hook.
Dropping the field deletes the second read rather than checking it.

Governed execution consequently does not run a delegate tool's own callbacks at
all, and that is the stated behaviour rather than an omission. A caller who wants
to observe governed calls has the governance audit trail, which records the
decision *and* the execution, and their own run-level handlers, which go on
seeing the governed tool's start and end exactly once; a hook that runs between
the decision and the body is not observability, it is a second chance to change
the call. The wrapper side reached the same conclusion for its own reasons long
before this (:func:`~zeroth.integrations.langgraph._tool_wrappers._carried_fields`
refuses to carry ``callbacks`` because doing so would fire every handler twice),
so the two halves of the governed path now agree instead of contradicting each
other.
"""


def static_class_attribute(kind: Any, name: str) -> Any:
    """Resolve *name* on *kind* by walking the real MRO, never by attribute access.

    ``getattr(kind, name)`` routes through the metaclass, which is code the
    delegate may have written; this reads each class's own ``__dict__`` through
    ``type``'s descriptors instead, so the answer is what Python would actually
    execute rather than what the class would like to report.

    Args:
        kind: The class to resolve against.
        name: The attribute name.

    Returns:
        The first implementation found along the MRO, or ``None`` when the name
        is defined nowhere on it.
    """
    try:
        mro = _MRO_OF(kind)
    except Exception:
        return None
    for cls in mro:
        try:
            namespace = _DICT_OF(cls)
        except Exception:
            continue
        if name in namespace:
            return namespace[name]
    return None


def static_instance_dict(target: object) -> Mapping[str, Any]:
    """Return *target*'s own instance dictionary, read through ``object``.

    This is where pydantic keeps field values, so it is where a tool's ``func``
    actually lives -- and where an attacker's shadowing entry for ``invoke``
    lives too.

    Returns:
        The instance dictionary, or an empty mapping for an object without one.
    """
    try:
        namespace = object.__getattribute__(target, "__dict__")
    except Exception:
        return {}
    return namespace if isinstance(namespace, dict) else {}


def static_instance_field(target: object, name: str) -> Any:
    """Read one field off *target*'s instance dictionary, or ``None``.

    A property is deliberately not consulted: a property is code, and a tool that
    keeps its body behind one is a tool whose body governance cannot pin. Such a
    tool still has its ``_run`` fingerprinted and executed, so it is governed --
    just not through a field that could answer differently on the second read.
    """
    return static_instance_dict(target).get(name)


def _permitted_hooks() -> dict[str, tuple[Any, ...]]:
    """Table the framework's own implementation of every governed hook, by identity.

    Built from ``langchain_core``'s ``BaseTool`` and ``StructuredTool`` only, and
    resolved statically. ``StructuredTool`` is what the ``@tool`` decorator
    produces for single- and multi-argument functions alike, so permitting its
    overrides is what keeps the ordinary case working. ``langchain_core``'s
    single-input ``Tool`` is deliberately absent: nothing in this package
    produces one, and admitting a class on the strength of an unread override is
    the direction that fails open.
    """
    table: dict[str, tuple[Any, ...]] = {}
    for hook in _GOVERNED_HOOKS:
        permitted = []
        for source in (BaseTool, StructuredTool):
            implementation = static_class_attribute(source, hook)
            if implementation is not None and not any(implementation is seen for seen in permitted):
                permitted.append(implementation)
        table[hook] = tuple(permitted)
    return table


_PERMITTED_HOOKS = _permitted_hooks()
"""What may appear under each name in :data:`_GOVERNED_HOOKS`, by identity."""


def refuse_delegate_dispatch(delegate: Any) -> None:
    """Refuse a tool that can answer for itself on any path governance travels.

    Three separate refusals, because they are three separate ways to take
    control and closing one leaves the others open:

    * an **overridden implementation** of a governed hook, resolved statically so
      a hostile metaclass cannot report somebody else's;
    * a **per-instance shadow** of one -- ``invoke`` is a plain function on the
      class and therefore a *non-data* descriptor, so an entry of that name in the
      instance dictionary wins ordinary attribute lookup outright. This is the
      vector the previous class-level-only check could not see;
    * an **unknown metaclass**, which is a dispatch layer on every class read.

    Args:
        delegate: The tool whose execution path is being checked.

    Raises:
        UnstableToolIdentityError: If any of the three applies. Fail-closed
            rather than governed: the decision is made about the arguments this
            wrapper parsed, and a delegate that re-derives them afterwards
            executes something no policy saw.
    """
    kind = type(delegate)
    if not any(type(kind) is permitted for permitted in _PERMITTED_METACLASSES):
        raise UnstableToolIdentityError(
            "this tool's class dispatches through a metaclass governance cannot read past"
        )
    for hook, permitted in _PERMITTED_HOOKS.items():
        implementation = static_class_attribute(kind, hook)
        if not any(implementation is candidate for candidate in permitted):
            raise UnstableToolIdentityError(
                f"this tool overrides a tool entry point governance cannot execute past: {hook}"
            )
    shadowed = static_instance_dict(delegate)
    for hook in _GOVERNED_HOOKS:
        if hook in shadowed:
            raise UnstableToolIdentityError(
                f"this tool shadows a tool entry point on the instance: {hook}"
            )


@dataclass(frozen=True, slots=True)
class ToolSnapshot:
    """One tool's body and surface, captured by value before any caller code ran.

    Everything a governed call needs after this point comes from here, and
    **nothing here is produced by running the delegate's code.** The snapshot used
    to carry the delegate itself so that execution could bind a method body with
    ``implementation.__get__(delegate, kind)`` -- one call into delegate-written
    code, made after the classifier, the contract resolver and the decision
    client had all run, deciding what the authorized call would invoke. The
    binding is now done by :func:`types.MethodType` inside :func:`snapshot_tool`,
    so the field has no reader left and is gone with it. A bound body still holds
    the instance it was bound to, which is the tool's own state and the declared
    boundary this module documents; what it does not hold is a route back into
    the delegate's *code*.

    Attributes:
        name: The declared name.
        description: The declared description.
        args_schema: The declared schema, which the wrapper parses against.
        bodies: Every implementation slot found, by name, already frozen and
            already bound -- the material the fingerprint digests and the exact
            material execution runs.
        carried: The output-shaping fields the executing tool needs.
    """

    name: Any
    description: Any
    args_schema: Any
    bodies: Mapping[str, Any]
    carried: Mapping[str, Any]


def _is_framework_boilerplate(implementation: Any, method: str) -> bool:
    """Report whether *implementation* is ``langchain_core``'s own ``_run``/``_arun``.

    Framework boilerplate is not a body and must never be captured as one.
    ``BaseTool._arun`` re-dispatches to ``self._run`` when it is called, so a
    snapshot that carried it would hold a body that resolves itself through the
    delegate at execution time -- exactly the hole every other line here closes.
    The check is by identity against the two classes governance reads, so a
    subclass that overrides ``_arun`` with its own implementation is a body and
    is captured as one.
    """
    return any(
        implementation is static_class_attribute(source, method)
        for source in (BaseTool, StructuredTool)
    )


def _bound_method_body(implementation: Any, delegate: Any, kind: Any, method: str) -> Any:
    """Freeze one class-defined body and bind it, before any caller code has run.

    Binding is the step that used to be delegate-controlled: execution called
    ``implementation.__get__(delegate, kind)``, and a ``_run`` that is an object
    with a ``__call__`` and a ``__get__`` is fingerprinted through the first and
    invoked through the second, so it could answer identity with one body and
    execution with another. Python's own binding for the three descriptor kinds
    below is not code the delegate wrote, so it is performed here -- once, before
    the classifier, the contract resolver and the decision client -- and the
    result is what both the fingerprint and the call see.

    Args:
        implementation: The ``_run``/``_arun`` read statically off the class.
        delegate: The tool an instance method is bound to.
        kind: The tool's class, which a ``classmethod`` binds to instead.
        method: The slot name, for the refusal message.

    Returns:
        The frozen, already-bound callable for that slot.

    Raises:
        UnstableToolIdentityError: If the body is any other shape. Refusing costs
            no tool that a person wrote -- a method, a ``staticmethod`` and a
            ``classmethod`` are what a ``BaseTool`` subclass declares -- and
            admitting the rest would mean running the delegate's binding code to
            find out what governance had just authorized.
    """
    if type(implementation) is types.FunctionType:
        return types.MethodType(snapshot_callable(implementation), delegate)
    if type(implementation) is staticmethod:
        return snapshot_callable(implementation.__func__)
    if type(implementation) is classmethod:
        return types.MethodType(snapshot_callable(implementation.__func__), kind)
    raise UnstableToolIdentityError(
        f"this tool keeps its body behind a descriptor governance cannot bind "
        f"without executing the tool's own code: {method}"
    )


def snapshot_tool(delegate: Any) -> ToolSnapshot:
    """Capture what *delegate* is, now, by static reads only.

    Called *before* the side-effect classifier, the contract resolver and the
    decision client -- every one of which is caller-supplied code that runs
    between identity derivation and execution, and every one of which could
    otherwise move the body in that window and have it execute under the
    identity that was pinned before it moved.

    Every slot is captured **as a value**: a field body is rebuilt by
    :func:`snapshot_callable`, and a class-defined body is frozen the same way
    and then bound here rather than at execution time. Reading a slot and keeping
    the object it held was never enough -- the object stays put while its
    ``__code__`` is replaced -- and binding at execution time handed the delegate
    one last chance to choose the callee after the decision had been made.

    Args:
        delegate: The tool being governed.

    Returns:
        The snapshot identity is derived from and execution runs.

    Raises:
        UnstableToolIdentityError: If the tool dispatches through anything
            governance cannot read past (see :func:`refuse_delegate_dispatch`),
            or keeps a body behind a descriptor that would have to run its own
            code to be bound (see :func:`_bound_method_body`).
    """
    refuse_delegate_dispatch(delegate)
    kind = type(delegate)
    bodies: dict[str, Any] = {}
    for field in _BODY_FIELDS:
        body = static_instance_field(delegate, field)
        if body is not None:
            bodies[field] = snapshot_callable(body)
    for method in _BODY_METHODS:
        implementation = static_class_attribute(kind, method)
        if implementation is None or _is_framework_boilerplate(implementation, method):
            continue
        bodies[method] = _bound_method_body(implementation, delegate, kind, method)
    return ToolSnapshot(
        name=static_instance_field(delegate, "name"),
        description=static_instance_field(delegate, "description"),
        args_schema=static_instance_field(delegate, "args_schema"),
        bodies=bodies,
        carried={
            field: static_instance_field(delegate, field)
            for field in _CARRIED_FIELDS
            if static_instance_field(delegate, field) is not None
        },
    )


def _snapshot_body(snapshot: ToolSnapshot, field: str, method: str) -> Any:
    """Pick the callable that runs one half of a snapshot, sync or async.

    Both candidates were frozen and bound when the snapshot was taken, so this is
    a choice between two values and nothing more: a ``func``/``coroutine`` field
    wins because that is where a ``StructuredTool`` keeps its body, and a
    ``_run``/``_arun`` the tool's own class defined stands in when there is no
    field. Framework boilerplate never reaches here -- it is not captured -- so
    the absence of both is a tool with no body rather than a tool whose body is
    somebody else's ``_arun``.
    """
    body = snapshot.bodies.get(field)
    if body is not None:
        return body
    return snapshot.bodies.get(method)


def _adapted(body: Any) -> Any:
    """Present a snapshotted sync body to the framework behind an opaque signature.

    ``StructuredTool._run`` reads the *body's own signature* and adds arguments to
    the call from what it finds there::

        if run_manager and signature(self.func).parameters.get("callbacks"):
            kwargs["callbacks"] = run_manager.get_child()
        if config_param := _get_runnable_config_param(self.func):
            kwargs[config_param] = config
        return self.func(*args, **kwargs)

    Both values are manufactured by the framework *after* the decision, out of the
    run's live state, and neither was ever shown to a policy: a body that declares
    a ``callbacks`` parameter receives a live child callback manager, and one with
    a parameter annotated exactly ``RunnableConfig`` receives the whole run
    configuration -- callback manager included. That is the same second read the
    rest of this module deletes, reached through the framework instead of through
    the delegate, and no list of banned parameter names would close it any better
    than the banned-attribute lists closed the earlier cycles.

    The adapter closes it structurally. ``signature`` of the returned function is
    ``(*args, **kwargs)``, so ``parameters.get("callbacks")`` is ``None``; it
    carries no annotations, so ``_get_runnable_config_param`` finds nothing.
    Neither injection has anywhere to land, and the body is called with exactly
    the arguments the governed call was authorized with. This is also where that
    property becomes *checkable*: every argument the body ever receives passes
    through the single forwarding call below, so "no framework keyword the policy
    never inspected reaches the body" is a claim about one readable line rather
    than about the whole of ``BaseTool``'s dispatch.

    Args:
        body: The snapshotted callable to forward to.

    Returns:
        A plain function that forwards its arguments unchanged.
    """

    def governed_body(*args: Any, **kwargs: Any) -> Any:
        """Forward exactly what the authorized call carried, and nothing else."""
        return body(*args, **kwargs)

    # Deliberately *not* ``functools.wraps``, and deliberately no ``__signature__``.
    # ``inspect.signature`` follows ``__wrapped__`` and honours an explicit
    # ``__signature__``, so either one would hand ``StructuredTool`` the body's own
    # parameter list straight back and re-open both injections -- which is exactly
    # what a later tidy-up reaching for a nicer traceback would do. The adapter's
    # own literal name is the whole of the debugging affordance on purpose: copying
    # ``body.__name__`` across would be an attribute read on a captured callable,
    # and this module does not make those.
    return governed_body


def _adapted_async(body: Any) -> Any:
    """Present a snapshotted async body to the framework behind an opaque signature.

    The ``coroutine`` half of :func:`_adapted`, and it exists because
    ``StructuredTool._arun`` repeats the same two injections verbatim against
    ``self.coroutine``. Everything :func:`_adapted` says about ``functools.wraps``,
    ``__wrapped__`` and ``__signature__`` applies here unchanged.

    Args:
        body: The snapshotted awaitable-returning callable to forward to.

    Returns:
        A coroutine function that forwards its arguments unchanged.
    """

    async def governed_body(*args: Any, **kwargs: Any) -> Any:
        """Await exactly what the authorized call carried, and nothing else."""
        return await body(*args, **kwargs)

    return governed_body


def executing_tool(snapshot: ToolSnapshot) -> BaseTool:
    """Build the framework-owned tool a governed call actually invokes.

    A ``StructuredTool`` **constructed here**, never a copy of the delegate. The
    difference is the whole fix: a copy is produced by the delegate's own
    ``model_copy`` and carries the delegate's class, its instance dictionary and
    its attribute dispatch, so every read the executing object makes is a read
    the delegate can answer. This object's class is ``StructuredTool``, its
    dictionary is written here, and its body is the snapshot's -- there is no
    attribute on it the delegate can reach.

    ``args_schema`` is cleared for the reason it always was: the wrapper has
    already parsed the call against the delegate's schema and the policy was
    asked about the result, so validating a second time would run a
    caller-supplied validator twice. Only a *pure* validator answers the same
    thing twice -- a stateful one, one that reads the clock, one that consumes a
    nonce returns something else, and the body would then execute arguments no
    policy saw. With no schema, ``BaseTool._parse_input`` is a pass-through and
    the body receives exactly the values the decision was made about.

    **Each half is installed behind an adapter, not directly.** With the schema
    cleared, the last route by which an unauthorized value could still reach the
    body was the framework reading the *body's own signature* and injecting
    against what it found -- see :func:`_adapted`. Wrapping both halves leaves the
    executing tool with a ``(*args, **kwargs)`` surface that neither injection
    matches, so the body is called with the arguments the decision was made about
    and nothing else. Only the half the snapshot actually holds is wrapped:
    ``StructuredTool`` needs at least one of the two and picks its execution path
    by which of them is set, so manufacturing an adapter over a missing body would
    quietly turn a sync-only tool into one that claims an async body.

    Args:
        snapshot: What the tool was when its identity was pinned.

    Returns:
        The tool to invoke for this call.

    Raises:
        ToolGovernanceError: If the snapshot holds no runnable body, or will not
            build an executing tool. Refusing is the only outcome that does not
            run something the guard could not pin.
    """
    func = _snapshot_body(snapshot, "func", "_run")
    coroutine = _snapshot_body(snapshot, "coroutine", "_arun")
    if func is None and coroutine is None:
        raise ToolGovernanceError(
            "this tool exposes no body that can be executed under the identity it was authorized by"
        )
    try:
        return StructuredTool(
            name=str(snapshot.name),
            description=str(snapshot.description or ""),
            args_schema=None,
            func=None if func is None else _adapted(func),
            coroutine=None if coroutine is None else _adapted_async(coroutine),
            **dict(snapshot.carried),
        )
    except Exception as error:
        raise ToolGovernanceError(
            "this tool cannot be driven with the arguments its call was authorized under"
        ) from error


def snapshot_callable(target: Any) -> Any:
    """Return a body that a later mutation cannot move, for any callable.

    The vector this answers is not substitution of the object but mutation of the
    code inside it: ``target.__code__ = other`` swaps the body while the
    closed-over reference, the field that held it, the signature and the whole
    surface stay put. Every route into this module now passes through here --
    ``govern_tools``'s plain-callable surface, a ``StructuredTool``'s ``func`` and
    ``coroutine`` fields, and the ``_run``/``_arun`` a subclass defines -- because
    a mutation the callable surface was rebuilt against was equally available on
    the tool surface, which was not.

    A plain function is rebuilt from its own parts, so the governed wrapper holds
    a function whose ``__code__`` nothing else has a reference to. A **bound
    method** is rebuilt the same way and re-bound to the same instance: it is the
    shape a tool built from ``StructuredTool.from_function(func=obj.run)`` holds,
    and ``obj.run`` is one ``__code__`` assignment away from being another body.
    Re-binding to the *same* instance is deliberate -- an object's attributes are
    the state its body reads, and binding a copy would hide a tool's own state
    from its own next call.

    **Rebuilding only the outermost callable was half a snapshot.** A body is not
    only its own ``__code__``: it is also every implementation it captured, and
    each of those was reached through a container the delegate still held. A
    ``functools.partial`` was returned as it was, so ``partial.func.__code__``
    moved what ran; a callable object was returned as it was, so
    ``type(obj).__call__.__code__`` did; a rebuilt function was handed the
    delegate's own ``__closure__`` tuple, so ``cell.cell_contents = other`` moved
    the helper it called; and its ``__kwdefaults__`` was assigned across by
    reference, so one ``__setitem__`` rewrote a default the executing body
    materializes for itself. None of the four moves an object the snapshot points
    at. All four move what it runs.

    **What is frozen is what identity calls implementation, and nothing else.**
    The line is not drawn again here: ``_is_implementation`` is imported from
    :mod:`~zeroth.integrations.langgraph._tool_fingerprint`, so the walk descends
    into exactly the shapes that module walks -- a function, a bound method, a
    ``partial``, a code object, a ``staticmethod``, a ``classmethod`` -- and stops
    where it stops. A captured entry is frozen **at its own position only**: a
    function in a cell, a default or a ``partial``'s keyword is implementation and
    is detached, while a client object, a mapping or a list is state and is passed
    through by reference, exactly as ``_bound_material`` records it by value and
    ``_cell_material`` records it by type. Nothing recurses into a container
    looking for implementation, because identity does not look there either.

    That rule is what keeps the freeze from becoming the opposite failure. A cell
    holding **state** stays the delegate's own cell -- a body that rebinds a
    counter through ``nonlocal`` must still see its own count on its next call,
    and a fresh cell would give every call a copy of the world. A ``partial``'s
    bound arguments are copied into the rebuilt one *by reference*: they are the
    configuration that makes it a distinct tool, they must arrive intact, and the
    keywords *mapping* is copied so that the mapping cannot be rewritten
    underneath the call. A callable object's ``__call__`` is resolved statically
    off its type, frozen, and re-bound to the **same instance**, for the reason a
    bound method is.

    The recursion is bounded by ``_guard_depth`` -- the fingerprint's own
    ``_MAX_MATERIAL_DEPTH`` and its own refusal -- so a self-referential closure
    stops on the bound identity derivation stops on, with the error that surface
    already raises, rather than recursing until the stack ends.

    Args:
        target: The callable being governed.

    Returns:
        The callable to execute for every governed call.

    Raises:
        UnstableToolIdentityError: If the implementation is nested past the
            fingerprint's depth bound.
    """
    return _frozen_implementation(target, 0)


def _frozen_implementation(target: Any, depth: int) -> Any:
    """Return *target* with every implementation it captured detached from the delegate.

    Shape for shape, this is ``_implementation_material``'s walk with a rebuild
    where that one has a projection, and the correspondence is the completeness
    argument: a shape identity *walks* and this does not freeze is a body the
    fingerprint pinned and the delegate can still move, which is the whole of the
    vector. The one shape that is returned as it is, is the one identity refuses
    -- a C-implemented ``__call__`` carries no code, so the tool has no
    fingerprint and is denied a moment later rather than run unfrozen.

    Args:
        target: The implementation to freeze.
        depth: How deep in the implementation this value sits.

    Returns:
        The frozen equivalent, callable exactly as *target* was.

    Raises:
        UnstableToolIdentityError: If the nesting exceeds the fingerprint's bound.
    """
    _guard_depth(depth)
    kind = type(target)
    if kind is types.FunctionType:
        return _frozen_function(target, depth)
    if kind is types.MethodType:
        return types.MethodType(_frozen_implementation(target.__func__, depth + 1), target.__self__)
    if kind is functools.partial:
        return _frozen_partial(target, depth)
    if kind is types.CodeType:
        return target
    if kind is staticmethod:
        return staticmethod(_frozen_implementation(target.__func__, depth + 1))
    if kind is classmethod:
        return classmethod(_frozen_implementation(target.__func__, depth + 1))
    return _frozen_callable_object(target, depth)


def _frozen_capture(value: Any, depth: int) -> Any:
    """Freeze one captured entry when it is implementation, else keep it as it is.

    The projection ``_bound_material`` makes, inverted: it hands an
    implementation-typed entry to ``_implementation_material`` and records
    everything else by value or by type, so an entry it walks as code is one this
    must detach, and an entry it records as a value is state a tool is entitled to
    keep. The test is applied at this position and never one level down, because
    a function inside a list default is not walked by identity either.
    """
    if _is_implementation(value):
        return _frozen_implementation(value, depth + 1)
    return value


def _frozen_cell(cell: Any, depth: int) -> Any:
    """Return the cell a rebuilt function should close over in place of *cell*.

    A cell holding implementation is replaced by a **new** cell holding the frozen
    value: the contents are what moves -- ``cell.cell_contents = other`` is an
    assignment -- so freezing the value alone would close nothing and the cell
    itself has to stop being shared.

    A cell holding anything else is handed back untouched. That is not a
    concession: it is the state side of the same line ``_cell_material`` draws by
    recording a non-implementation cell as a bare type name, and a tool that
    rebinds a ``nonlocal`` between calls keeps working only because of it.

    An **unbound** cell is also handed back untouched. A fresh cell would have to
    hold something, and putting ``None`` in it would turn a body's ``NameError``
    into a silent ``None`` -- a behaviour change, in the one direction this module
    must never make one.
    """
    try:
        captured = cell.cell_contents
    except ValueError:
        return cell
    if not _is_implementation(captured):
        return cell
    return types.CellType(_frozen_implementation(captured, depth + 1))


_DISOWNING_ATTRIBUTES = frozenset({"__signature__", "__wrapped__"})
"""The two attributes a function may use to describe itself as something it is not.

``inspect.signature`` honours an explicit ``__signature__`` and follows
``__wrapped__`` before it ever looks at the ``__code__`` it was handed, so either
one answers "what parameters does this take?" with something the body is free to
disagree with -- a value that raises, or a valid ``Signature`` naming fewer
parameters than the body will materialize defaults for.

Both live in a function's ``__dict__``, which is why they are named here rather
than simply not being copied: the rebuild carries ``__dict__`` across on purpose,
because a tool's own attributes are its state. These two are not state. They are
descriptions of an implementation, and this module exists to stop a description
and an implementation being two separately-readable things. :func:`_adapted`
already refuses to *write* either of them for exactly this reason; carrying them
across a freeze was the same mistake in the other direction.
"""


def _frozen_function(target: Any, depth: int) -> Any:
    """Rebuild a plain function around frozen code, frozen cells and copied defaults.

    ``__globals__`` is deliberately shared. A module's globals are the world a
    body runs in rather than the body itself -- ``_code_material`` records the
    *names* a code object reads, never their values -- and rebinding them would
    hand every tool a frozen copy of its own module.

    ``__signature__`` and ``__wrapped__`` are deliberately **not** carried across;
    see :data:`_DISOWNING_ATTRIBUTES`. The rebuilt function's parameters are
    whatever its frozen ``__code__``, ``__defaults__`` and ``__kwdefaults__`` say
    they are, and there is no longer a second place to read them from.
    """
    keyword_defaults = target.__kwdefaults__
    defaults = target.__defaults__
    closure = target.__closure__
    rebuilt = types.FunctionType(
        target.__code__,
        target.__globals__,
        target.__name__,
        None if defaults is None else tuple(_frozen_capture(item, depth) for item in defaults),
        None if closure is None else tuple(_frozen_cell(cell, depth) for cell in closure),
    )
    rebuilt.__kwdefaults__ = (
        None
        if keyword_defaults is None
        else {key: _frozen_capture(value, depth) for key, value in keyword_defaults.items()}
    )
    rebuilt.__qualname__ = target.__qualname__
    rebuilt.__doc__ = target.__doc__
    rebuilt.__module__ = target.__module__
    rebuilt.__dict__.update(
        {key: value for key, value in target.__dict__.items() if key not in _DISOWNING_ATTRIBUTES}
    )
    return rebuilt


def _frozen_partial(target: Any, depth: int) -> Any:
    """Rebuild a ``functools.partial`` around a frozen callable and its own arguments.

    Only ``func`` is implementation. The bound arguments are the configuration
    that makes ``partial(run, "read")`` a different tool from
    ``partial(run, "delete")``, they are recorded by value in
    ``_partial_material``, and they are carried across unchanged -- with the two
    *containers* rebuilt, so neither the argument tuple nor the keyword mapping the
    call resolves against is one the delegate can still write into.
    """
    return functools.partial(
        _frozen_implementation(target.func, depth + 1),
        *(_frozen_capture(item, depth) for item in target.args),
        **{key: _frozen_capture(value, depth) for key, value in target.keywords.items()},
    )


def _frozen_callable_object(target: Any, depth: int) -> Any:
    """Bind a frozen copy of an instance's ``__call__`` to that same instance.

    ``__call__`` is read off the **type**, through the same static walk every
    other read in this module makes, and never through the instance: an object
    whose ``__getattribute__`` answers the lookup would be choosing its own
    implementation, which is the vector this module exists to delete.

    The binding then re-applies exactly what CPython applies for the shape found,
    measured rather than assumed: a plain function is bound to the instance, a
    ``classmethod`` to the type, and a ``staticmethod``, a ``partial`` or a nested
    callable object is called with the arguments alone because none of them is a
    descriptor. The instance is the same one throughout -- its attributes are its
    state, and a tool that reads its own configuration must go on reading it.

    A ``__call__`` that is not a shape identity can walk -- a slot wrapper, a
    builtin -- is left alone with the target, which is not a gap: that tool has no
    fingerprint at all and ``_callable_object_material`` refuses it before it can
    run.
    """
    call = static_class_attribute(type(target), "__call__")
    if not _is_implementation(call):
        return target
    frozen = _frozen_implementation(call, depth + 1)
    if type(call) is types.FunctionType:
        return types.MethodType(frozen, target)
    if type(call) is classmethod:
        return types.MethodType(frozen.__func__, type(target))
    if type(call) is staticmethod:
        return frozen.__func__
    return frozen


__all__ = [
    "ToolSnapshot",
    "executing_tool",
    "refuse_delegate_dispatch",
    "snapshot_callable",
    "snapshot_tool",
    "static_class_attribute",
    "static_instance_dict",
    "static_instance_field",
]
