"""Make a tool's instance configuration part of the surface it declares.

**The gap this closes.** A tool's identity covered the code it runs and the
surface it declares, never the configuration bound onto the instance running it.
``HttpTool(endpoint="https://good.example")`` and
``HttpTool(endpoint="https://evil.example")`` were therefore one identity, and a
policy that authorized the first authorized the second. The same held for a
factory closing over its configuration.

**Why configuration could not simply be digested.** Identity is re-derived and
compared on *every* call, so digesting all bound state would make a tool that
counts its own invocations refuse its own second call -- fail-closed on correct
code, on every long-running agent. The trade-off is forced, and the way out of it
is not a better heuristic for telling configuration from state: it is for the
tool's author to say which is which.

**Names, never values.** An author declares which *fields* decide identity. The
projection then reads those fields off the live tool and digests them by value.
So the only thing asserted is a field list, the values are derived exactly as
everything else in :mod:`~zeroth.integrations.langgraph._tool_fingerprint` is, and
the list itself is digested beside them -- stripping a declaration, faking one, or
renaming a field moves the identity rather than matching it. "Derived, never
asserted" survives intact, because a declaration cannot make a tool *look like*
another tool; it can only make it look like less of itself.

**Every carrier, never the first one.** ``StructuredTool`` is a ``BaseTool`` whose
configuration lives in a closure, so a carrier cannot be chosen by tool kind. A
declared name can be answered by:

1. the tool's own **instance attribute**, read through
   :func:`~zeroth.integrations.langgraph._tool_execution.static_instance_dict`
   rather than ``getattr``, so no property and no ``__getattr__`` runs;
2. the **free variable** of each implementation slot -- the ``co_freevars``
   position of the name, resolved to its ``__closure__`` cell.

All of them are digested, keyed by the carrier they came from. An earlier design
took the first that answered, and that was wrong in the direction that matters:
the carriers are not alternative spellings of one value, so the fingerprint could
describe an attribute while the body read a differing closed-over variable, or
describe a sync body while ``ainvoke`` ran a differing async one. Two tools then
shared an identity while executing different endpoints -- assurance reported where
there was none, which is worse than declaring nothing at all.

A name that no carrier answers **refuses the tool**. An author who declares
``endpoint`` and has no ``endpoint`` gets an error rather than a declaration that
silently governs nothing.

**Everything is read off the live target.** Every carrier is re-read on every call
from the tool the wrapper was handed, never from a value captured at wrapping time
-- which is the whole point: a reconfigured instance must fail the identity
comparison the next call makes. Non-implementation closure cells survive
:func:`~zeroth.integrations.langgraph._tool_execution.snapshot_guarded_callable`
by reference, so a free variable read here is the one the body will actually see.

**The identity comparison is not the last word, because it is not last.** The
metadata resolver, the decision client, the audit sink and the approval seam all
run after it and all hold the tool, so declared configuration is verified once
more immediately before the body is entered -- see
:func:`~zeroth.integrations.langgraph._tool_wrappers._refuse_configuration_drift`.
"""

from __future__ import annotations

import functools
import types
from collections.abc import Callable, Iterable, Mapping
from typing import Any, TypeVar

from zeroth.integrations.langgraph._tool_errors import UnstableToolIdentityError
from zeroth.integrations.langgraph._tool_execution import (
    _DICT_OF,
    _MRO_OF,
    static_instance_dict,
)
from zeroth.integrations.langgraph._tool_fingerprint import configuration_digest
from zeroth.integrations.langgraph._tool_normalize import normalize_identity_configuration

DECLARATION_ATTRIBUTE = "zeroth_identity_configuration"
"""Where a tool declares which of its fields are identity-bearing configuration.

Read off a ``BaseTool`` subclass as a ``ClassVar`` -- pydantic refuses a
non-annotated class attribute -- and off an implementation function as a plain
attribute, which is what :func:`identity_configuration` sets.
"""

_MAX_UNWRAP_DEPTH = 24
"""How far to walk a decorated implementation before giving up on finding a function.

The same bound the fingerprint's projection uses, for the same reason: deep enough
for a tool behind several layers of decoration, finite so a self-referential
wrapper cannot walk forever.
"""

_Declarable = TypeVar("_Declarable", bound=Callable[..., Any])


def _refuse(reason: str) -> UnstableToolIdentityError:
    """Build the one error this module raises, so every refusal reads alike."""
    return UnstableToolIdentityError(reason)


_normalized = normalize_identity_configuration
"""Normalize a declaration: exactly a ``tuple`` of exact-``str`` names, or refused.

Defined in :mod:`~zeroth.integrations.langgraph._tool_normalize` so the inventory,
which is imported eagerly and must stay free of ``langchain_core``, can gate a
declaration it is handed without reaching this module.
"""


def _entries(namespace: object) -> Any:
    """Return a namespace's entries without calling a method it could have replaced.

    ``namespace.items()`` is an ordinary attribute lookup, so a ``dict`` subclass
    -- which an instance ``__dict__`` is allowed to be -- gets to run its own code
    the moment identity is derived. ``dict.items(namespace)`` is the same C-level
    routine ``dict`` itself would run and reaches no override.

    A class namespace arrives as a ``mappingproxy`` instead. That type cannot be
    subclassed, so its own ``items`` is the only one there is -- but it *delegates*
    to whatever mapping it wraps, so the guarantee holds because of where the
    proxy comes from, not because of the proxy: the only one passed here is a
    class ``__dict__`` obtained from ``type.__dict__``, which always wraps a real
    dictionary. A proxy built by a caller around a hostile mapping would not be
    safe, and none is ever built here.

    Args:
        namespace: The foreign namespace to read.

    Returns:
        Its key/value pairs, or an empty tuple for anything that is neither shape.
    """
    if isinstance(namespace, dict):
        return dict.items(namespace)
    if type(namespace) is types.MappingProxyType:
        return namespace.items()
    return ()


def _exact_entry(namespace: object, name: str) -> tuple[bool, Any]:
    """Find one exactly-``str`` key in a foreign namespace without keying into it.

    ``name in namespace`` hashes *name* and then compares it against whatever
    keys share that hash bucket, which runs a stored key's ``__eq__``. A tool can
    store a ``str`` subclass whose ``__eq__`` executes code, or which answers for
    a field the tool does not really carry. Scanning entries and gating each key
    on ``type(key) is str`` *before* comparing reaches no user-defined hook: the
    gate excludes every subclass, so the comparison that follows is plain text
    against plain text. This is the rule
    :func:`~zeroth.integrations.langgraph._tool_fingerprint._mapping_material`
    already follows. The entries themselves come from :func:`_entries`, because
    asking the namespace for them is its own hook.

    Args:
        namespace: The foreign namespace to search.
        name: The field name to find.

    Returns:
        Whether an exactly-``str`` key matched, and its value.
    """
    for key, value in _entries(namespace):
        if type(key) is str and key == name:
            return True, value
    return False, None


def _class_entry(kind: Any, name: str) -> Any:
    """Resolve one name on a class through its real MRO, or report none.

    :func:`~zeroth.integrations.langgraph._tool_execution.static_class_attribute`
    resolves the MRO the same way and is what every other class read here uses,
    but it finds its answer with ``name in namespace``, and a class dictionary
    holds a ``str`` subclass key exactly as an instance dictionary does. This walk
    is the same walk with :func:`_exact_entry` in place of that lookup, so
    resolving a name cannot run the delegate's comparison code.

    Args:
        kind: The class to resolve against.
        name: The attribute name.

    Returns:
        The first value found along the MRO, or ``None``.
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
        found, value = _exact_entry(namespace, name)
        if found:
            return value
    return None


def _instance_namespace(target: object) -> Mapping[str, Any]:
    """Return a tool's own attribute dictionary, or refuse a tool that fakes one.

    **A tool does not get to decide what its own attributes are.**
    :func:`~zeroth.integrations.langgraph._tool_execution.static_instance_dict`
    reads ``__dict__`` through ``object.__getattribute__``, which finds whatever
    descriptor the *class* defines for that name -- and a class may define a
    ``property``. Reading the instance-attribute carrier would then execute the
    delegate's code, and the value it answered with would be one it chose after
    seeing that it was being asked. That is the same hazard
    :func:`~zeroth.integrations.langgraph._tool_execution.static_instance_field`
    avoids for ordinary fields, one level further down.

    Every shape governance actually wraps -- a plain function, a callable object,
    a ``BaseTool`` subclass, a ``StructuredTool``, a ``functools.partial`` --
    carries the interpreter's own ``getset_descriptor`` here and answers with an
    exact ``dict``, so refusing anything else costs no tool a person wrote.

    **What this proves, and what it does not.** ``getset_descriptor`` is the type
    CPython gives the generic instance-dictionary getter, and it is also the type
    ``PyDescr_NewGetSet`` gives *any* C getter, so an exact-type check establishes
    that no **Python** code answers for the namespace -- not that the interpreter's
    own getter did. A native extension type could still supply its own, and a
    Python subclass could inherit it.

    That residual is outside this package's model rather than a hole in this gate.
    Identity is derived by reading ``__code__``, ``co_consts`` and closure cells
    off the objects a tool presents (see
    :mod:`~zeroth.integrations.langgraph._tool_fingerprint`); an extension module
    that can forge a ``__dict__`` getter can forge those too, and every fingerprint
    in this package would be describing whatever it chose to show. Native code is
    trusted here by construction, and has been since identity was first derived.
    What the gate closes is the reachable case: a tool written in Python that
    answers for its own namespace.

    Args:
        target: The tool whose namespace is being read.

    Returns:
        The instance dictionary, empty for an object that has none.

    Raises:
        UnstableToolIdentityError: If the class supplies its own ``__dict__``, or
            the namespace is not exactly a ``dict`` -- either would make the
            namespace a value the tool computes rather than one it holds.
    """
    descriptor = _class_entry(type(target), "__dict__")
    if descriptor is not None and type(descriptor) is not types.GetSetDescriptorType:
        raise _refuse("this tool decides for itself what its own attributes are")
    namespace = static_instance_dict(target)
    if type(namespace) is not dict:
        raise _refuse("this tool does not present its attributes as a plain namespace")
    return namespace


def identity_configuration(*names: str) -> Callable[[_Declarable], _Declarable]:
    """Declare a tool body's free variables as identity-bearing configuration.

    The counterpart, for a hand-written ``BaseTool`` subclass, is a class-level
    ``zeroth_identity_configuration: ClassVar[tuple[str, ...]]``. Use this one on
    the body a factory returns, so the configuration it closed over decides its
    identity::

        def make_fetcher(endpoint: str):
            @identity_configuration("endpoint")
            def fetch(path: str) -> str:
                return httpx.get(f"{endpoint}/{path}").text
            return fetch

    Two bodies built from different endpoints then carry different fingerprints,
    and a policy written against one does not authorize the other.

    Args:
        *names: The field names that decide this tool's identity.

    Returns:
        A decorator that records the declaration on the body it is given.

    Raises:
        UnstableToolIdentityError: If a name is not a usable identifier, checked
            at decoration time rather than at the first governed call.
    """
    declared = _normalized(names)

    def declare(target: _Declarable) -> _Declarable:
        """Record the declaration on *target* and hand it back unchanged."""
        setattr(target, DECLARATION_ATTRIBUTE, declared)
        return target

    return declare


def _implementation_function(body: object) -> Any:
    """Unwrap an implementation down to the function whose closure holds its state.

    A body reaches this module as whatever the snapshot captured: a bound method,
    a ``partial``, a ``staticmethod``, or a plain function. Only a function has
    ``__code__`` and ``__closure__``, so the others are walked through.

    Args:
        body: The implementation to unwrap.

    Returns:
        The function, or ``None`` when the body is not one and does not hold one.
    """
    current = body
    for _ in range(_MAX_UNWRAP_DEPTH):
        kind = type(current)
        if kind is types.FunctionType:
            return current
        if kind is types.MethodType:
            current = current.__func__
        elif kind is functools.partial:
            current = current.func
        elif kind is staticmethod or kind is classmethod:
            current = current.__func__
        else:
            return None
    return None


def _function_dictionary(body: object) -> Mapping[str, Any]:
    """Return an implementation function's own attribute dictionary, or an empty one."""
    function = _implementation_function(body)
    if function is None:
        return {}
    return static_instance_dict(function)


def declared_configuration_names(target: object, bodies: Mapping[str, Any]) -> tuple[str, ...]:
    """Return every field name *target* declares as identity-bearing configuration.

    Read from the tool's class through the real MRO, and from each implementation
    body's own attribute dictionary. Never through ``getattr``: resolving a
    declaration is not a reason to run the delegate's code.

    A tool's class and its bodies may each declare; the union is taken, so a
    ``BaseTool`` subclass declaring an instance field and holding a body that
    declares its own closure both govern.

    Args:
        target: The tool being described.
        bodies: The implementation slots read off it.

    Returns:
        The normalized declared names, empty when nothing is declared.

    Raises:
        UnstableToolIdentityError: If a declaration is present but unusable.
    """
    declared: set[str] = set()
    declared.update(_normalized(_class_entry(type(target), DECLARATION_ATTRIBUTE)))
    for body in bodies.values():
        _, declaration = _exact_entry(_function_dictionary(body), DECLARATION_ATTRIBUTE)
        declared.update(_normalized(declaration))
    return tuple(sorted(declared))


def _free_variable(name: str, body: object) -> tuple[bool, Any]:
    """Resolve one declared name against the free variables of one body.

    Args:
        name: The declared field name.
        body: The implementation slot to search.

    Returns:
        Whether the name was found, and its value.

    Raises:
        UnstableToolIdentityError: If the name resolves to a cell that holds
            nothing yet. A declared field with no value is a declaration this
            module cannot honour, and honouring it partially is the one thing it
            must not do.
    """
    function = _implementation_function(body)
    if function is None:
        return False, None
    code = function.__code__
    closure = function.__closure__ or ()
    if name not in code.co_freevars:
        return False, None
    index = code.co_freevars.index(name)
    if index >= len(closure):
        return False, None
    try:
        return True, closure[index].cell_contents
    except ValueError as error:
        raise _refuse(
            f"this tool declares identity configuration that is not bound yet: {name}"
        ) from error


def configuration_values(
    names: Iterable[str], target: object, bodies: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    """Resolve every declared name against **every** carrier that holds it.

    **No precedence, because precedence pins the wrong value.** An earlier design
    took the first carrier that answered. But the carriers are not alternative
    spellings of one value: a body closing over ``endpoint`` and an instance
    attribute called ``endpoint`` are two different values, and the one the
    fingerprint recorded was not necessarily the one the body would read. Two
    tools identical but for the closed-over endpoint then shared an identity while
    executing different endpoints -- assurance reported where there was none, which
    is worse than declaring nothing at all. The same held between a tool's sync
    and async bodies: fingerprinting the first meant ``ainvoke`` could run an
    endpoint no policy ever saw.

    So every carrier holding the name is recorded, keyed by the carrier it came
    from. A value that differs in any carrier changes the identity, whichever
    body the framework goes on to call.

    Args:
        names: The declared field names.
        target: The tool the instance-attribute carrier is read from.
        bodies: The implementation slots, by slot name, that the free-variable
            carriers are read from.

    Returns:
        The declared configuration: field name, then carrier, then value.

    Raises:
        UnstableToolIdentityError: If a declared name resolves to no carrier at
            all. An author who declares ``endpoint`` and carries none gets an
            error rather than a declaration that governs nothing.
    """
    attributes = _instance_namespace(target)
    values: dict[str, dict[str, Any]] = {}
    for name in names:
        carriers: dict[str, Any] = {}
        found, value = _exact_entry(attributes, name)
        if found:
            carriers["attribute"] = value
        for slot, body in bodies.items():
            found, value = _free_variable(name, body)
            if found:
                carriers[f"freevar:{slot}"] = value
        if not carriers:
            raise _refuse(f"this tool declares identity configuration it does not carry: {name}")
        values[name] = carriers
    return values


def configuration_fingerprint(target: object, bodies: Mapping[str, Any]) -> str | None:
    """Return the digest of *target*'s declared configuration, or ``None`` for none.

    ``None`` is what keeps the identity of every tool that declares nothing
    exactly what it was: the caller omits the key from its identity material
    rather than recording an empty one, so nothing that predates this module
    changes fingerprint.

    Args:
        target: The tool being described.
        bodies: The implementation slots read off it, by slot name.

    Returns:
        The hex SHA-256 digest of the declared configuration, or ``None`` when
        the tool declares none.

    Raises:
        UnstableToolIdentityError: If a declaration is unusable, names a field
            the tool does not carry, or holds a value that cannot be projected.
    """
    names = declared_configuration_names(target, bodies)
    if not names:
        return None
    return configuration_digest(configuration_values(names, target, bodies))


__all__ = [
    "DECLARATION_ATTRIBUTE",
    "configuration_fingerprint",
    "configuration_values",
    "declared_configuration_names",
    "identity_configuration",
]
