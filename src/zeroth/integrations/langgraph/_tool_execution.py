"""Capture and execute the exact callable body whose identity was authorized.

All delegate reads are static and all captured implementations are frozen before
caller-controlled policy seams run. After authorization, execution performs only
a slot-local shared-state escalation check and a direct body call. Async entry
awaits a native coroutine or uses LangChain's executor helper for a sync body;
there is no inner BaseTool invocation or callback/configuration boundary.
"""

from __future__ import annotations

import functools
import types
from abc import ABCMeta
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from langchain_core.runnables.config import run_in_executor
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
class FrozenCallableSnapshot:
    """A detached callable plus the shared state cells it still depends on."""

    body: Any
    state_cells: tuple[Any, ...]


class _StateCellCollector:
    """Collect shared closure cells once by identity across a recursive freeze."""

    def __init__(self) -> None:
        self.cells: list[Any] = []
        self._identities: set[int] = set()

    def add(self, cell: Any) -> None:
        identity = id(cell)
        if identity not in self._identities:
            self._identities.add(identity)
            self.cells.append(cell)


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
        state_cells: Shared non-implementation closure cells, grouped by body
            slot so execution checks only the body it is about to enter.
    """

    name: Any
    description: Any
    args_schema: Any
    bodies: Mapping[str, Any]
    state_cells: Mapping[str, tuple[Any, ...]]


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


def _bound_method_body(
    implementation: Any, delegate: Any, kind: Any, method: str
) -> FrozenCallableSnapshot:
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
        frozen = snapshot_guarded_callable(implementation)
        return FrozenCallableSnapshot(types.MethodType(frozen.body, delegate), frozen.state_cells)
    if type(implementation) is staticmethod:
        return snapshot_guarded_callable(implementation.__func__)
    if type(implementation) is classmethod:
        frozen = snapshot_guarded_callable(implementation.__func__)
        return FrozenCallableSnapshot(types.MethodType(frozen.body, kind), frozen.state_cells)
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
    state_cells: dict[str, tuple[Any, ...]] = {}
    for field in _BODY_FIELDS:
        body = static_instance_field(delegate, field)
        if body is not None:
            frozen = snapshot_guarded_callable(body)
            bodies[field] = frozen.body
            state_cells[field] = frozen.state_cells
    for method in _BODY_METHODS:
        implementation = static_class_attribute(kind, method)
        if implementation is None or _is_framework_boilerplate(implementation, method):
            continue
        frozen = _bound_method_body(implementation, delegate, kind, method)
        bodies[method] = frozen.body
        state_cells[method] = frozen.state_cells
    return ToolSnapshot(
        name=static_instance_field(delegate, "name"),
        description=static_instance_field(delegate, "description"),
        args_schema=static_instance_field(delegate, "args_schema"),
        bodies=bodies,
        state_cells=state_cells,
    )


def _snapshot_body_with_state(
    snapshot: ToolSnapshot, field: str, method: str
) -> tuple[Any, tuple[Any, ...]]:
    """Select one body slot together with only that slot's shared state cells."""
    body = snapshot.bodies.get(field)
    if body is not None:
        return body, snapshot.state_cells.get(field, ())
    return snapshot.bodies.get(method), snapshot.state_cells.get(method, ())


def refuse_state_cell_escalation(state_cells: tuple[Any, ...]) -> None:
    """Refuse when shared state became executable after authorization."""
    for cell in state_cells:
        try:
            value = cell.cell_contents
        except ValueError:
            continue
        if _is_implementation(value):
            raise ToolGovernanceError(
                "a shared state cell became implementation after authorization"
            )


def execute_snapshot(
    snapshot: ToolSnapshot, args: tuple[Any, ...], kwargs: Mapping[str, Any]
) -> Any:
    """Execute the captured sync body directly on the authorized arguments."""
    body, state_cells = _snapshot_body_with_state(snapshot, "func", "_run")
    if body is None:
        raise NotImplementedError("StructuredTool does not support sync invocation.")
    refuse_state_cell_escalation(state_cells)
    return body(*args, **kwargs)


async def aexecute_snapshot(
    snapshot: ToolSnapshot, args: tuple[Any, ...], kwargs: Mapping[str, Any]
) -> Any:
    """Await the captured async body, falling back to LangChain's executor."""
    body, state_cells = _snapshot_body_with_state(snapshot, "coroutine", "_arun")
    if body is not None:
        refuse_state_cell_escalation(state_cells)
        return await body(*args, **kwargs)
    return await run_in_executor(None, execute_snapshot, snapshot, args, kwargs)


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
    return snapshot_guarded_callable(target).body


def snapshot_guarded_callable(target: Any) -> FrozenCallableSnapshot:
    """Freeze *target* and retain every non-implementation cell left shared."""
    collector = _StateCellCollector()
    body = _frozen_implementation(target, 0, collector)
    return FrozenCallableSnapshot(body, tuple(collector.cells))


def _frozen_implementation(target: Any, depth: int, collector: _StateCellCollector) -> Any:
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
        collector: The identity-deduplicating shared-state cell collector.

    Returns:
        The frozen equivalent, callable exactly as *target* was.

    Raises:
        UnstableToolIdentityError: If the nesting exceeds the fingerprint's bound.
    """
    _guard_depth(depth)
    kind = type(target)
    if kind is types.FunctionType:
        return _frozen_function(target, depth, collector)
    if kind is types.MethodType:
        return types.MethodType(
            _frozen_implementation(target.__func__, depth + 1, collector),
            target.__self__,
        )
    if kind is functools.partial:
        return _frozen_partial(target, depth, collector)
    if kind is types.CodeType:
        return target
    if kind is staticmethod:
        return staticmethod(_frozen_implementation(target.__func__, depth + 1, collector))
    if kind is classmethod:
        return classmethod(_frozen_implementation(target.__func__, depth + 1, collector))
    return _frozen_callable_object(target, depth, collector)


def _frozen_capture(value: Any, depth: int, collector: _StateCellCollector) -> Any:
    """Freeze one captured entry when it is implementation, else keep it as it is.

    The projection ``_bound_material`` makes, inverted: it hands an
    implementation-typed entry to ``_implementation_material`` and records
    everything else by value or by type, so an entry it walks as code is one this
    must detach, and an entry it records as a value is state a tool is entitled to
    keep. The test is applied at this position and never one level down, because
    a function inside a list default is not walked by identity either.
    """
    if _is_implementation(value):
        return _frozen_implementation(value, depth + 1, collector)
    return value


def _frozen_cell(cell: Any, depth: int, collector: _StateCellCollector) -> Any:
    """Return the cell a rebuilt function should close over in place of *cell*.

    A cell holding implementation is replaced by a **new** cell holding the frozen
    value: the contents are what moves -- ``cell.cell_contents = other`` is an
    assignment -- so freezing the value alone would close nothing and the cell
    itself has to stop being shared.

    A cell holding anything else is handed back untouched. That is not a
    concession: it is the state side of the same line ``_cell_material`` draws by
    recording a non-implementation cell as a bare type name, and a tool that
    rebinds a ``nonlocal`` between calls keeps working only because of it.

    An **unbound** cell is also handed back untouched, but recorded so a later
    state-to-implementation transition is refused. A fresh cell would have to hold
    something, and putting ``None`` in it would turn a body's ``NameError`` into a
    silent ``None`` -- a behaviour change, in the one direction this module must
    never make one.
    """
    try:
        captured = cell.cell_contents
    except ValueError:
        collector.add(cell)
        return cell
    if not _is_implementation(captured):
        collector.add(cell)
        return cell
    return types.CellType(_frozen_implementation(captured, depth + 1, collector))


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
and an implementation being two separately-readable things. Direct execution
does not consult either attribute; carrying them across a freeze would recreate
the same second description on the published snapshot.
"""


def _frozen_function(target: Any, depth: int, collector: _StateCellCollector) -> Any:
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
        (
            None
            if defaults is None
            else tuple(_frozen_capture(item, depth, collector) for item in defaults)
        ),
        (
            None
            if closure is None
            else tuple(_frozen_cell(cell, depth, collector) for cell in closure)
        ),
    )
    rebuilt.__kwdefaults__ = (
        None
        if keyword_defaults is None
        else {
            key: _frozen_capture(value, depth, collector) for key, value in keyword_defaults.items()
        }
    )
    rebuilt.__qualname__ = target.__qualname__
    rebuilt.__doc__ = target.__doc__
    rebuilt.__module__ = target.__module__
    rebuilt.__dict__.update(
        {key: value for key, value in target.__dict__.items() if key not in _DISOWNING_ATTRIBUTES}
    )
    return rebuilt


def _frozen_partial(target: Any, depth: int, collector: _StateCellCollector) -> Any:
    """Rebuild a ``functools.partial`` around a frozen callable and its own arguments.

    Only ``func`` is implementation. The bound arguments are the configuration
    that makes ``partial(run, "read")`` a different tool from
    ``partial(run, "delete")``, they are recorded by value in
    ``_partial_material``, and they are carried across unchanged -- with the two
    *containers* rebuilt, so neither the argument tuple nor the keyword mapping the
    call resolves against is one the delegate can still write into.
    """
    return functools.partial(
        _frozen_implementation(target.func, depth + 1, collector),
        *(_frozen_capture(item, depth, collector) for item in target.args),
        **{key: _frozen_capture(value, depth, collector) for key, value in target.keywords.items()},
    )


def _frozen_callable_object(target: Any, depth: int, collector: _StateCellCollector) -> Any:
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
    frozen = _frozen_implementation(call, depth + 1, collector)
    if type(call) is types.FunctionType:
        return types.MethodType(frozen, target)
    if type(call) is classmethod:
        return types.MethodType(frozen.__func__, type(target))
    if type(call) is staticmethod:
        return frozen.__func__
    return frozen


__all__ = [
    "ToolSnapshot",
    "aexecute_snapshot",
    "execute_snapshot",
    "refuse_delegate_dispatch",
    "snapshot_callable",
    "snapshot_tool",
    "static_class_attribute",
    "static_instance_dict",
    "static_instance_field",
]
