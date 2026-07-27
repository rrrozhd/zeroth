"""Execute the body whose identity was authorized, and nothing the delegate chooses.

Three audit cycles converged on one invariant and kept finding it broken at a new
seam: **the body that runs must be the body whose identity was authorized, and
the arguments it receives must be the arguments the policy saw.** Each cycle
closed the vector that had been demonstrated and the next probe walked around it,
because the defence was a list of banned attributes and the attacker chose the
attribute. The list was never the problem; reading through the delegate at all
was.

Two properties fix that, and this module exists to hold both:

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

The refusal table in :func:`refuse_delegate_dispatch` is kept as well, and is
deliberately *not* the load-bearing part. It fails a tool closed at wrap time and
again per call, which turns a hostile tool into an error rather than a silent
substitution; but a fix that consisted only of refusals would be the fourth
version of a list somebody walks around. The snapshot is what makes the guarantee
hold even when nothing is refused.

**What is still not claimed.** A snapshot pins the *code* a tool will run, not
the state that code reads: a body that consults a mutable attribute of its own
object is fingerprinted by its code, and the code is what executes. Three things
are on the state side of that line and are named here so they are read as the
disclosed boundary rather than found as the next omission: a rebuilt function
keeps its **closure cells and its module globals** by reference, so a body that
reads either reads whatever they hold when it runs -- rebinding them would give
a tool a frozen copy of the world and break every tool that keeps state
anywhere; a bound body keeps the **instance** it was
bound to, because binding it to a copy would make a tool's own state invisible to
its own next call; and a **callable object** or ``functools.partial`` is captured
as it is, so a body that delegates to a mutable attribute of its own instance is
governed by the code of that ``__call__`` and not by the attribute. All three are
the same declared-identity boundary
:mod:`~zeroth.integrations.langgraph._tool_fingerprint` documents -- identity
covers a tool's implementation, not its configuration -- and it is the boundary
the cookbook discloses.
"""

from __future__ import annotations

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
    "callbacks",
    "metadata",
    "tags",
    "return_direct",
    "verbose",
)
"""Fields the *executing* tool needs so its output is shaped as the delegate's was.

``response_format`` is the load-bearing one: a ``content_and_artifact`` tool
builds its own ``ToolMessage`` with the artifact attached, and an executing tool
that dropped the setting would return bare content and lose the artifact.
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
            if implementation is not None and not any(
                implementation is seen for seen in permitted
            ):
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
        kind: The delegate's class, as the fingerprint records it.
        name: The declared name.
        description: The declared description.
        args_schema: The declared schema, which the wrapper parses against.
        bodies: Every implementation slot found, by name, already frozen and
            already bound -- the material the fingerprint digests and the exact
            material execution runs.
        carried: The output-shaping fields the executing tool needs.
    """

    kind: Any
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
        kind=kind,
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
            func=func,
            coroutine=coroutine,
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

    A ``functools.partial`` and a callable object are returned unchanged, which is
    the disclosed boundary rather than an omission: their implementation is the
    code of the ``func`` or the ``__call__`` that the fingerprint already walks,
    and an instance that delegates to a mutable attribute of its own is a tool
    whose *configuration* moved, not one whose code did. Identity covers the
    implementation; :mod:`~zeroth.integrations.langgraph._tool_fingerprint` says
    so, and the cookbook discloses it.

    Args:
        target: The callable being governed.

    Returns:
        The callable to execute for every governed call.
    """
    if type(target) is types.MethodType:
        function = target.__func__
        if type(function) is not types.FunctionType:
            return target
        return types.MethodType(snapshot_callable(function), target.__self__)
    if type(target) is not types.FunctionType:
        return target
    rebuilt = types.FunctionType(
        target.__code__,
        target.__globals__,
        target.__name__,
        target.__defaults__,
        target.__closure__,
    )
    rebuilt.__kwdefaults__ = target.__kwdefaults__
    rebuilt.__qualname__ = target.__qualname__
    rebuilt.__doc__ = target.__doc__
    rebuilt.__module__ = target.__module__
    rebuilt.__dict__.update(target.__dict__)
    return rebuilt


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
