"""Fingerprint the code a governed tool will run, not just the surface it presents.

**A tool's surface is not its identity.** Name, description and argument *names*
are metadata a substituted tool reproduces exactly: two functions returning
different answers, bound under the same name with the same description and the
same declared schema, are indistinguishable by surface alone -- and a tool that
fingerprints alike inherits the authorization the original was granted. What
cannot be reproduced without being the same tool is the *code that runs*, so that
is what this module digests, alongside the tool's complete declared schema rather
than its argument names.

**Derived, never asserted.** The alternative primitive -- an implementation
fingerprint the caller supplies -- is a *claim*, and the party best placed to make
that claim is the party that substituted the tool. Nothing here reads a
fingerprint off a tool object or off a caller seam: the material is projected out
of the callable's own code object, and a projection cannot be asserted, only
observed.

**Fail closed, never weaker.** Deriving identity from a Python callable is partial
by construction: a C-implemented function carries no bytecode, a slot wrapper has
no ``__code__``, a callable object keeps part of its behaviour in state this
module refuses to read. Every one of those raises
:class:`~zeroth.integrations.langgraph._tool_errors.UnstableToolIdentityError`,
never a silent fall-back to the surface-only identity the finding was about. A
tool that cannot be pinned is a tool that cannot be governed, and refusing it is
the only answer that does not hand a substitution the original's authorization.

**Stable across processes and runs.** A digest is SHA-256 over a canonical JSON
serialization with sorted keys. No ``id()``, no memory address, no unqualified
``hash()`` -- ``PYTHONHASHSEED`` randomizes ``str`` hashing per process, so
anything ordered by it differs in the next one. The single hash-ordered value a
code object can carry is a ``frozenset`` constant, which is what ``x in {"a",
"b"}`` compiles to, and it is projected as a *sorted* list of its serialized
members for exactly that reason. ``repr`` is used only where it cannot embed an
address, and :func:`_stable_repr` refuses one that does.

**``co_filename`` and ``co_firstlineno`` are deliberately excluded.** Identity is
a claim about what the code does, and a blank line inserted above a tool changes
neither. ``__module__`` and ``__qualname__`` already separate two same-bodied
functions that live in different places, without making an edit elsewhere in the
file read as a substitution.

**Bound values: by value where they are fixed, by type where they are state.** A
``functools.partial``'s arguments and a function's defaults are fixed when the
callable is built, so they are digested by value and ``mode="safe"`` does not
fingerprint as ``mode="unsafe"``. A *closure cell* is not fixed -- ``nonlocal``
rebinds one mid-run -- and the identity is re-derived and compared on every call,
so digesting a cell's value would make a tool that counts its own invocations
refuse the second one as a substitution of the first. Cells are therefore recursed
into when they hold code, which is how a decorated tool's real body is reached,
and reduced to a type name otherwise. The residual is stated plainly: two closures
over different *configuration* still collapse onto one identity. What does not
collapse is two different bodies, which is the substitution this module exists to
catch.

Nothing here imports ``langchain_core``: a tool is read structurally, so the same
projection serves a ``BaseTool``, a plain function and anything else a caller
calls a tool. Structurally does not mean trustingly -- an implementation slot that
*raises* rather than answering is refused rather than treated as absent, because a
tool hiding its body would otherwise be identified by the boilerplate every tool
of its class shares (:func:`_slot_value`).
"""

from __future__ import annotations

import functools
import hashlib
import json
from collections.abc import Callable
from types import CodeType, FunctionType, MethodType
from typing import Any

from zeroth.integrations.langgraph._tool_errors import UnstableToolIdentityError

_MAX_MATERIAL_DEPTH = 24
"""Deepest nesting the implementation projection will walk before refusing.

Deep enough for a tool behind several layers of decoration -- each layer costs a
closure cell and a function -- and finite, so a self-referential closure cannot
walk forever. Past it the tool is refused rather than described partially: a
truncated implementation is a weaker identity, which is the thing this module
does not produce.
"""

_TOOL_BODY_SLOTS = ("func", "coroutine")
"""Where a ``StructuredTool`` keeps the callables it will actually run."""

_TOOL_METHOD_SLOTS = ("_run", "_arun")
"""Where a ``BaseTool`` subclass keeps its implementation instead.

Read off the *class*, not the instance, so what is fingerprinted is the function
the subclass defined rather than a bound method's re-derived wrapper. Every slot
that is present must fingerprint: a tool whose ``func`` is C-implemented must not
pass on the strength of the boilerplate ``_run`` it inherited.
"""


def _peek(source: object, attribute: str) -> Any:
    """Read one attribute off a foreign object, treating any failure as absent.

    A local copy of the wrappers' helper rather than an import of it, so this
    module stays free of ``langchain_core``. A tool is allowed to expose ``func``
    as a property and a property is allowed to raise; a raise means "the tool
    declared nothing" and is then handled by the caller, which for an
    implementation slot means refusing the tool.

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


def _refuse(reason: str) -> UnstableToolIdentityError:
    """Build the one error this module raises, so every refusal reads alike."""
    return UnstableToolIdentityError(reason)


def _guard_depth(depth: int) -> None:
    """Refuse a projection that has walked past :data:`_MAX_MATERIAL_DEPTH`.

    Args:
        depth: How deep in the implementation this projection has walked.

    Raises:
        UnstableToolIdentityError: If the bound is exceeded.
    """
    if depth > _MAX_MATERIAL_DEPTH:
        raise _refuse("this tool's implementation is nested past the fingerprint depth")


def _optional_text(value: object) -> str:
    """Return *value* only when it is exactly a ``str``, else the empty string.

    Used for a callable's ``__module__`` and ``__qualname__``, which are labels
    beside the code digest rather than the identity itself: a function built by
    ``exec`` legitimately carries no module, and refusing it would refuse a tool
    whose body this module can fingerprint perfectly well. A ``str`` subclass is
    not text here, for the reason it is not text anywhere else in this package.
    """
    return value if type(value) is str else ""


def _type_name(value: object) -> str:
    """Return the qualified name of *value*'s type, or of *value* when it is a type.

    Args:
        value: The object to name.

    Returns:
        ``module.qualname``, both read through exact-``str`` gates.

    Raises:
        UnstableToolIdentityError: If either half is not exactly a ``str``. A
            hostile metaclass can answer a ``str`` subclass, and a subclass can
            substitute its own content at render time.
    """
    kind = value if isinstance(value, type) else type(value)
    module = _peek(kind, "__module__")
    qualname = _peek(kind, "__qualname__")
    if type(module) is not str or type(qualname) is not str:
        raise _refuse("this tool's type does not name itself in plain text")
    return f"{module}.{qualname}"


def _serialize(material: Any) -> str:
    """Render projected material as the one canonical JSON string it digests from.

    Args:
        material: The projected material.

    Returns:
        The canonical serialization: sorted keys, no spacing, ASCII-escaped.

    Raises:
        UnstableToolIdentityError: If the material is not serializable, which
            means the projection let something through that it should not have.
    """
    try:
        return json.dumps(
            material, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        )
    except (TypeError, ValueError) as error:
        raise _refuse("this tool's identity material is not canonically serializable") from error


def _digest(material: Any) -> str:
    """Return the stable hex SHA-256 digest of projected material.

    Not :func:`~zeroth.integrations.langgraph._tool_normalize.argument_fingerprint`,
    whose copy budget is sized for one call's arguments: a code object's constant
    tree is far wider than that, and a tool would be refused for the size of its
    own body. The serialization rules are the same ones.
    """
    return hashlib.sha256(_serialize(material).encode("utf-8")).hexdigest()


def _stable_repr(value: object) -> str:
    """Return a value's ``repr`` only when it cannot be carrying a memory address.

    ``repr`` describes a type annotation or a constraint object stably and
    describes anything inheriting ``object.__repr__`` by its address, which is
    different in the next process. An identity built on one would report every
    restart as a substitution, and would match nothing against a tool list a
    different process declared.

    Args:
        value: The object to describe.

    Returns:
        Its ``repr``.

    Raises:
        UnstableToolIdentityError: If the rendering looks like an address.
    """
    rendered = repr(value)
    if " at 0x" in rendered or " object at " in rendered:
        raise _refuse("this tool's schema is described only by an unstable identity")
    return rendered


def _literal_text(value: Any, _depth: int) -> Any:
    """Project an exact ``str`` constant as itself."""
    return ["str", value]


def _literal_bytes(value: Any, _depth: int) -> Any:
    """Project a ``bytes`` constant as hex, which is JSON and is exact."""
    return ["bytes", value.hex()]


def _literal_scalar(value: Any, _depth: int) -> Any:
    """Project a numeric, boolean or singleton constant through its exact ``repr``.

    ``repr`` is exact for these types and carries no address: ``repr(0.1)``
    round-trips, ``repr(None)`` is ``None``, and neither depends on the process.
    """
    return [type(value).__name__, repr(value)]


def _literal_tuple(value: Any, depth: int) -> Any:
    """Project a ``tuple`` constant element by element, in order."""
    return ["tuple", [_literal_material(item, depth + 1) for item in value]]


def _literal_frozenset(value: Any, depth: int) -> Any:
    """Project a ``frozenset`` constant as its *sorted* serialized members.

    ``x in {"a", "b"}`` compiles to a ``frozenset`` constant, and a ``frozenset``
    of strings iterates in an order ``PYTHONHASHSEED`` randomizes per process.
    Sorting the serialized members is what keeps a tool containing one
    fingerprinting alike in the next process.
    """
    return ["frozenset", sorted(_serialize(_literal_material(item, depth + 1)) for item in value)]


def _literal_code(value: Any, depth: int) -> Any:
    """Project a nested code constant -- a comprehension, a nested def -- in full."""
    return ["code", _code_material(value, depth + 1)]


_LITERAL_HANDLERS: tuple[tuple[type, Callable[[Any, int], Any]], ...] = (
    (str, _literal_text),
    (bytes, _literal_bytes),
    (bool, _literal_scalar),
    (int, _literal_scalar),
    (float, _literal_scalar),
    (complex, _literal_scalar),
    (type(None), _literal_scalar),
    (type(...), _literal_scalar),
    (tuple, _literal_tuple),
    (frozenset, _literal_frozenset),
    (CodeType, _literal_code),
)
"""Every type a code object's constants can hold, paired with how to project it.

Scanned with ``is`` rather than looked up in a dict, as the canonical argument
projection is: a dict lookup hashes and compares the key, and the key would be
``type(value)`` -- a metaclass defining ``__hash__`` / ``__eq__`` could make a
hostile class answer as ``tuple``. Identity comparison reaches no user-defined
hook, and a subclass is simply absent from the table.
"""


def _literal_material(value: Any, depth: int) -> Any:
    """Project one constant onto its canonical material, or refuse it.

    Args:
        value: The constant to project.
        depth: How deep in the implementation this constant sits.

    Returns:
        JSON-shaped material describing *value* exactly.

    Raises:
        UnstableToolIdentityError: If the constant is of a type the projection
            cannot describe, or the structure is nested past the bound.
    """
    _guard_depth(depth)
    for candidate, handler in _LITERAL_HANDLERS:
        if type(value) is candidate:
            return handler(value, depth)
    raise _refuse("this tool's code carries a constant that cannot be fingerprinted")


def _code_material(code: Any, depth: int) -> dict[str, Any]:
    """Project a code object onto everything about it that decides what it does.

    The bytecode, the constants it closes over, the names it reads and writes,
    and the shape of its parameter list. Not its file or its line number: see the
    module docstring for why an edit above a tool is not a substitution of it.

    Args:
        code: The code object.
        depth: How deep in the implementation this code sits.

    Returns:
        JSON-shaped material describing the code.

    Raises:
        UnstableToolIdentityError: If a constant cannot be projected, or the
            nesting exceeds the bound.
    """
    _guard_depth(depth)
    return {
        "name": _optional_text(code.co_name),
        "argcount": code.co_argcount,
        "posonlyargcount": code.co_posonlyargcount,
        "kwonlyargcount": code.co_kwonlyargcount,
        "flags": code.co_flags,
        "nlocals": code.co_nlocals,
        "bytecode": code.co_code.hex(),
        "names": [_optional_text(name) for name in code.co_names],
        "varnames": [_optional_text(name) for name in code.co_varnames],
        "freevars": [_optional_text(name) for name in code.co_freevars],
        "cellvars": [_optional_text(name) for name in code.co_cellvars],
        "consts": [_literal_material(const, depth + 1) for const in code.co_consts],
    }


def _is_implementation(value: object) -> bool:
    """Whether *value* is something whose code this module can walk into.

    The predicate that keeps the closure and callable-object walks from
    recursing forever: only these shapes are followed, and everything else is
    either described by its type or refused, depending on which walk asked.
    """
    kind = type(value)
    return any(
        kind is candidate
        for candidate in (
            FunctionType,
            MethodType,
            functools.partial,
            CodeType,
            staticmethod,
            classmethod,
        )
    )


def _cell_value(cell: object) -> Any:
    """Read one closure cell, treating an unbound cell as empty rather than failing."""
    try:
        return cell.cell_contents  # type: ignore[attr-defined]
    except ValueError:
        return None


def _cell_material(value: Any, depth: int) -> Any:
    """Project one closure cell: into the code it holds, or down to its type.

    A cell holding a function is a decorated tool's real body and is followed. A
    cell holding anything else is *state* -- ``nonlocal`` rebinds it while the run
    is in flight, and the identity is re-derived and compared on every call -- so
    only its type is recorded. See the module docstring for the residual this
    leaves and why it is the safer of the two failures.
    """
    if _is_implementation(value):
        return _implementation_material(value, depth + 1)
    return ["type", _type_name(value)]


def _bound_material(value: Any, depth: int) -> Any:
    """Project a value bound at construction time -- a default, a partial argument.

    Fixed when the callable was built rather than rebound as it runs, so these
    are recorded *by value* where the projection can represent them: a default of
    ``mode="safe"`` must not fingerprint as ``mode="unsafe"``. A value the literal
    projection cannot represent -- a client object, a mutable container -- is
    recorded by type instead of refused, because a tool that closes over a
    connection is a tool, not an attack, and its body is where its behaviour is.
    """
    if _is_implementation(value):
        return _implementation_material(value, depth + 1)
    try:
        return _literal_material(value, depth + 1)
    except UnstableToolIdentityError:
        return ["type", _type_name(value)]


def _function_material(func: Any, depth: int) -> dict[str, Any]:
    """Project a plain function: where it was defined, what it runs, what it carries.

    Raises:
        UnstableToolIdentityError: If the function exposes no code object, or its
            closure or defaults cannot be projected.
    """
    code = _peek(func, "__code__")
    if type(code) is not CodeType:
        raise _refuse("this tool's implementation exposes no code to fingerprint")
    cells = _peek(func, "__closure__") or ()
    defaults = _peek(func, "__defaults__") or ()
    keyword_defaults = _peek(func, "__kwdefaults__") or {}
    return {
        "kind": "function",
        "module": _optional_text(_peek(func, "__module__")),
        "qualname": _optional_text(_peek(func, "__qualname__")),
        "code": _code_material(code, depth + 1),
        "closure": [_cell_material(_cell_value(cell), depth + 1) for cell in cells],
        "defaults": [_bound_material(item, depth + 1) for item in defaults],
        "kwdefaults": _mapping_material(keyword_defaults, depth + 1),
    }


def _mapping_material(source: Any, depth: int) -> dict[str, Any]:
    """Project a mapping of bound values, refusing a key that is not exactly ``str``.

    Iterated through ``items()`` and never keyed back into, so a stored key's
    ``__hash__`` / ``__eq__`` is never given a chance to run.

    Raises:
        UnstableToolIdentityError: If *source* is not a mapping of exact ``str``
            keys. A dropped key is a bound value the fingerprint would not see.
    """
    material: dict[str, Any] = {}
    try:
        items = list(source.items())
    except Exception as error:
        raise _refuse("this tool's implementation carries an unreadable mapping") from error
    for key, value in items:
        if type(key) is not str:
            raise _refuse("this tool's implementation is keyed by something other than str")
        material[key] = _bound_material(value, depth + 1)
    return material


def _method_material(method: Any, depth: int) -> dict[str, Any]:
    """Project a bound method: the function it runs, and what it is bound to.

    The binding is recorded by type rather than by value, for the reason a
    closure cell is: an instance's attributes are the state a tool keeps while it
    runs, and an identity that moved with them would refuse the tool's own next
    call.
    """
    return {
        "kind": "method",
        "self": _type_name(_peek(method, "__self__")),
        "func": _implementation_material(_peek(method, "__func__"), depth + 1),
    }


def _partial_material(partial: Any, depth: int) -> dict[str, Any]:
    """Project a ``functools.partial``: the callable, and the arguments fixed onto it.

    The bound arguments are read-only on a ``partial`` object, so they are
    recorded by value: ``partial(run, "read")`` and ``partial(run, "delete")`` are
    two different tools and must not share one identity.
    """
    return {
        "kind": "partial",
        "func": _implementation_material(_peek(partial, "func"), depth + 1),
        "args": [_bound_material(item, depth + 1) for item in (_peek(partial, "args") or ())],
        "keywords": _mapping_material(_peek(partial, "keywords") or {}, depth + 1),
    }


def _callable_object_material(value: Any, depth: int) -> dict[str, Any]:
    """Project an instance whose ``__call__`` is the implementation, or refuse it.

    The last shape a tool can take before there is nothing left to read. A
    C-implemented callable lands here too and is refused: its ``__call__`` is a
    slot wrapper, which carries no code, and a builtin therefore has no
    fingerprint rather than a weak one.

    Raises:
        UnstableToolIdentityError: If ``__call__`` is not itself a shape whose
            code can be walked.
    """
    call = _peek(type(value), "__call__")
    if not _is_implementation(call):
        raise _refuse("this tool's implementation cannot be fingerprinted stably")
    return {
        "kind": "callable_object",
        "type": _type_name(value),
        "call": _implementation_material(call, depth + 1),
    }


def _implementation_material(value: Any, depth: int) -> Any:
    """Project whatever will run when a tool is invoked, or refuse to describe it.

    Args:
        value: The implementation: a function, a bound method, a ``partial``, a
            code object, or an instance that is callable.
        depth: How deep in the implementation this value sits.

    Returns:
        JSON-shaped material describing the code that will run.

    Raises:
        UnstableToolIdentityError: If no stable fingerprint can be derived, or
            the nesting exceeds the bound. Never a weaker description.
    """
    _guard_depth(depth)
    kind = type(value)
    if kind is FunctionType:
        return _function_material(value, depth)
    if kind is MethodType:
        return _method_material(value, depth)
    if kind is functools.partial:
        return _partial_material(value, depth)
    if kind is CodeType:
        return {"kind": "code", "code": _code_material(value, depth)}
    if kind is staticmethod or kind is classmethod:
        return _implementation_material(_peek(value, "__func__"), depth + 1)
    return _callable_object_material(value, depth)


def _model_field_material(schema: object) -> Any:
    """Project a model's declared fields when its JSON schema will not build.

    Types and constraints, not names: a field's annotation, the constraint
    objects hung off it, and its default. Reached only when
    ``model_json_schema()`` refuses -- a model holding an unserializable default
    does -- and refusing outright there would make a legitimate tool ungovernable.

    Raises:
        UnstableToolIdentityError: If the fields cannot be described stably.
    """
    fields = _peek(schema, "model_fields")
    if type(fields) is not dict:
        raise _refuse("this tool's argument schema cannot be fingerprinted")
    return {
        name: [
            _stable_repr(_peek(field, "annotation")),
            _stable_repr(_peek(field, "metadata")),
            _stable_repr(_peek(field, "is_required")),
        ]
        for name, field in fields.items()
        if type(name) is str
    }


def _model_schema_material(schema: object) -> Any | None:
    """Project a pydantic model's complete JSON schema, or report it is not one."""
    builder = _peek(schema, "model_json_schema")
    if not callable(builder):
        return None
    try:
        built = builder()
    except Exception:
        return _model_field_material(schema)
    return built if type(built) is dict else _model_field_material(schema)


def _annotation_material(schema: object) -> Any | None:
    """Project the annotations of a schema that is not a pydantic model.

    A ``TypedDict`` or a dataclass declares its fields' *types* in
    ``__annotations__``, which is the schema material a name list leaves out.
    """
    annotations = _peek(schema, "__annotations__")
    if type(annotations) is not dict:
        return None
    return {name: _stable_repr(value) for name, value in annotations.items() if type(name) is str}


def schema_digest(schema: object) -> str | None:
    """Return the digest of a tool's *complete* declared schema, or ``None`` for none.

    Complete means field types and constraints, not field names: two tools that
    both take ``path`` are two different tools when one takes any string and the
    other takes a bounded enum, and an identity keyed on names alone lets the
    second be substituted for the first.

    Args:
        schema: The tool's ``args_schema``, in any of the shapes one takes: a
            pydantic model, a raw JSON-schema mapping, an annotated class, or
            ``None`` for a tool that declares no arguments.

    Returns:
        The hex SHA-256 digest of the canonical schema, or ``None`` when the tool
        declares no schema at all -- which is a real answer, not a failure.

    Raises:
        UnstableToolIdentityError: If a schema is declared but cannot be
            described canonically. A schema that cannot be pinned is one a
            substitution could hide behind.
    """
    if schema is None:
        return None
    if type(schema) is dict:
        return _digest({"kind": "json_schema", "schema": schema})
    model = _model_schema_material(schema)
    if model is not None:
        return _digest({"kind": "model", "schema": model})
    annotations = _annotation_material(schema)
    if annotations is None:
        raise _refuse("this tool's argument schema cannot be fingerprinted")
    return _digest({"kind": "annotations", "type": _type_name(schema), "schema": annotations})


def callable_implementation_digest(target: object) -> str:
    """Return the digest of the code a plain callable will run.

    Args:
        target: The callable being governed.

    Returns:
        The hex SHA-256 digest of its implementation material.

    Raises:
        UnstableToolIdentityError: If no stable fingerprint can be derived --
            a builtin, a C extension function, anything whose code is not
            reachable from Python.
    """
    return _digest(_implementation_material(target, 0))


def _slot_value(source: object, attribute: str) -> Any:
    """Read one implementation slot, separating "absent" from "would not answer".

    :func:`_peek` treats both alike, and here they are not alike at all. A tool
    that simply has no ``func`` is a tool with one fewer body to digest; a tool
    whose ``func`` *raises* is a tool hiding its body from the fingerprint, and
    swallowing that would leave its identity resting on the boilerplate ``_run``
    it inherited -- which every other tool of its class shares.

    Args:
        source: The tool, or its class.
        attribute: The slot name.

    Returns:
        The slot's value, or ``None`` when the slot is genuinely absent.

    Raises:
        UnstableToolIdentityError: If reading the slot raises anything else.
    """
    try:
        return getattr(source, attribute)
    except AttributeError:
        return None
    except Exception as error:
        raise _refuse("this tool will not say what it runs") from error


def tool_implementation_digest(tool: object) -> str:
    """Return the digest of the code a ``BaseTool`` will run.

    Every implementation slot the tool exposes is projected: the callables a
    ``StructuredTool`` holds, and the ``_run`` / ``_arun`` its class defines. A
    slot that is present and cannot be fingerprinted refuses the tool rather than
    being skipped -- a tool whose ``func`` is C-implemented must not pass on the
    strength of the inherited boilerplate beside it.

    Args:
        tool: The tool being governed.

    Returns:
        The hex SHA-256 digest of its implementation material.

    Raises:
        UnstableToolIdentityError: If the tool exposes no implementation at all,
            or any slot it does expose cannot be fingerprinted stably.
    """
    slots: dict[str, Any] = {}
    for attribute in _TOOL_BODY_SLOTS:
        body = _slot_value(tool, attribute)
        if body is not None:
            slots[attribute] = _implementation_material(body, 0)
    for attribute in _TOOL_METHOD_SLOTS:
        method = _slot_value(type(tool), attribute)
        if method is not None:
            slots[attribute] = _implementation_material(method, 0)
    if not slots:
        raise _refuse("this tool exposes no implementation to fingerprint")
    return _digest({"type": _type_name(tool), "slots": slots})


__all__ = [
    "callable_implementation_digest",
    "schema_digest",
    "tool_implementation_digest",
]
