"""Govern LangGraph tool and callable surfaces without mutating their sources.

Each call snapshots identity before caller-controlled seams run, authorizes the
canonical arguments once, and executes the matching frozen body directly. The
governed BaseTool is the sole framework execution layer, so it owns output and
ToolException shaping while genuine nested LangChain work inherits its outer
callback context normally.
"""

from __future__ import annotations

import ast
import builtins
import contextlib
import functools
import inspect
import types
import typing
import weakref
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any, get_args, get_origin

from langchain_core.tools import BaseTool, InjectedToolCallId
from pydantic import BaseModel, Field, PrivateAttr, create_model
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from zeroth.governance.identity import ActorIdentity
from zeroth.integrations.langgraph._approval_lifecycle import SQLiteApprovalRepository
from zeroth.integrations.langgraph._tool_decisions import (
    ToolDecisionClient,
    UnknownSideEffectPolicy,
)
from zeroth.integrations.langgraph._tool_errors import (
    GovernanceContextError,
    ToolGovernanceError,
    UnstableToolIdentityError,
)
from zeroth.integrations.langgraph._tool_execution import (
    ToolSnapshot,
    _snapshot_body_with_state,
    aexecute_snapshot,
    execute_snapshot,
    refuse_delegate_dispatch,
    refuse_state_cell_escalation,
    snapshot_callable,
    snapshot_guarded_callable,
    snapshot_tool,
)
from zeroth.integrations.langgraph._tool_fingerprint import (
    _is_implementation,
    callable_implementation_digest,
    schema_digest,
    tool_slots_digest,
)
from zeroth.integrations.langgraph._tool_guard import (
    ToolAuditSubmitter,
    aguard_tool_call,
    guard_tool_call,
)
from zeroth.integrations.langgraph._tool_normalize import (
    classify_side_effect,
    normalize_capability_refs,
    normalize_contract_ref,
    normalize_identifier,
    normalize_requires_approval,
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

_PLAN_ATTRIBUTE = "_zeroth_plan"
"""Where a governed ``BaseTool`` keeps the one plan it executes through."""

_SEALED_ATTRIBUTES = (_PLAN_ATTRIBUTE, "zeroth_plan", "zeroth_delegate", "zeroth_binding")
"""Names :meth:`GovernedTool.__setattr__` refuses, whether or not they are fields.

The first three are the execution path: rebinding any of them is the
confused-deputy substitution the sealing exists to stop. ``zeroth_binding`` is
sealed too because it is what the inventory reports this wrapper governs, and a
report somebody rewrote after the wrapping describes tools nothing governs.
``zeroth_plan`` and ``zeroth_delegate`` are no longer fields at all -- pydantic
already refuses an unknown one under ``extra="ignore"`` -- and are listed so the
refusal is a typed governance failure rather than a bare ``ValueError``.
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

    Identity and reviewed tool metadata are pinned into the inventory binding.
    Every live action re-resolves the metadata and must match that binding before
    policy evaluation, so drift cannot inherit the reviewed authorization.

    Attributes:
        identity: The name and fingerprint the tool is decided under.
        side_effect: How this tool is described in an inventory, defaulting to
            unknown -- which the default policy denies.
        contract_ref: The contract this tool is described as bound to, when a
            caller declared one.
        capability_refs: Capabilities every call through this binding requires.
        requires_approval: Whether every call through this binding requires approval.
        coverage: What this wrapping can support.
            ``govern_tools`` never sets it to
            :attr:`~zeroth.integrations.langgraph._tool_types.InventoryCoverage.COMPLETE`,
            because completeness is a claim about tools nobody passed in.
    """

    identity: ToolIdentity
    side_effect: SideEffectClass = SideEffectClass.UNKNOWN
    contract_ref: str | None = None
    capability_refs: tuple[str, ...] = ()
    requires_approval: bool = False
    coverage: InventoryCoverage = InventoryCoverage.PARTIAL


@dataclass(frozen=True, slots=True)
class _ToolFacts:
    """The identifying surface read off one foreign tool object, already gated.

    ``snapshot`` is what makes the identity and the execution one fact rather
    than two reads of the same object: the ``BaseTool`` surface derives
    ``material`` from it and then *runs* it, so there is no window in which the
    thing that was fingerprinted and the thing that executes can differ. The
    callable surface leaves it ``None`` and carries its executable body in
    ``body`` instead, because a plain callable has no tool object to snapshot.
    """

    name: object
    description: str
    args_schema: Any
    material: Mapping[str, Any]
    snapshot: ToolSnapshot | None = None
    body: Any = None
    state_cells: tuple[Any, ...] = ()


@dataclass(frozen=True, slots=True)
class _Seams:
    """Everything ``govern_tools`` was handed that a call is decided through."""

    context: object = None
    client: ToolDecisionClient | None = None
    unknown_side_effect: UnknownSideEffectPolicy = UnknownSideEffectPolicy.DENY
    audit: ToolAuditSubmitter | None = None
    actor: ActorIdentity | None = None
    interrupt: Callable[[Mapping[str, Any]], Any] | None = None
    approval_lifecycle: SQLiteApprovalRepository | None = None
    side_effect: Callable[[Any], Any] | None = None
    contract_ref: Callable[[Any], Any] | None = None
    capability_refs: Callable[[Any], Any] | None = None
    requires_approval: Callable[[Any], Any] | None = None
    tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class _GovernedPlan:
    """One wrapper's pinned identity plus the live seams it enforces through."""

    target: Any
    describe: Callable[[Any], _ToolFacts]
    facts: _ToolFacts
    observed_identity: ToolIdentity
    binding: GovernedToolBinding
    seams: _Seams


@dataclass(frozen=True, slots=True)
class _CallableMetadata:
    """Immutable identifying metadata for one frozen callable source."""

    name: str
    description: str
    arguments: tuple[str, ...]
    schema: str | None


@dataclass(frozen=True, slots=True)
class _CallablePlan:
    """A plain callable's private execution plan, held behind an opaque token."""

    target: Any
    source: Any
    state_cells: tuple[Any, ...]
    metadata: _CallableMetadata
    facts: _ToolFacts
    observed_identity: ToolIdentity
    binding: GovernedToolBinding
    seams: _Seams


_CALLABLE_PLANS: dict[object, _CallablePlan] = {}
"""Callable plans keyed by fresh, non-callable tokens closed over by wrappers."""


@dataclass(slots=True)
class _BaseToolCall:
    """One validated native ``BaseTool.invoke`` identity awaiting its body."""

    owner: object
    tool_call_id: str | None
    entered: bool = False
    parsed: bool = False
    claimed: bool = False


_BASE_TOOL_CALL: ContextVar[_BaseToolCall | None] = ContextVar(
    "zeroth_base_tool_call", default=None
)
"""Trusted call-local identity carried from native ``BaseTool`` entry points."""

_SAFE_ANNOTATION_ATOMS = (
    str,
    bytes,
    int,
    float,
    bool,
    complex,
    object,
    type,
    list,
    dict,
    tuple,
    set,
    frozenset,
    type(None),
)
"""Exact builtin type objects admitted as annotation atoms."""

_SAFE_ANNOTATION_ORIGINS = _SAFE_ANNOTATION_ATOMS + (
    types.UnionType,
    Callable,
    Sequence,
    Mapping,
    typing.Annotated,
    typing.Union,
    typing.Literal,
    typing.ClassVar,
    typing.Final,
    typing.Required,
    typing.NotRequired,
    typing.TypeGuard,
    typing.Unpack,
    typing.Concatenate,
)
"""Exact origins whose recursively attested arguments define a safe typing graph."""

_SAFE_ANNOTATION_SUBSCRIPT_BASES = _SAFE_ANNOTATION_ORIGINS + tuple(
    vars(typing)[name]
    for name in (
        "Optional",
        "List",
        "Dict",
        "Tuple",
        "Set",
        "FrozenSet",
        "Type",
        "Callable",
        "Sequence",
        "Mapping",
    )
)
"""Exact builtin and typing values whose subscription cannot dispatch caller code."""

_MAX_STATIC_ATTESTATION_DEPTH = 32
_MAX_ATTESTATION_WORK = 8_192

_BUILTIN_CARRIER_BASES = frozenset(
    value for value in vars(builtins).values() if isinstance(value, type)
)
_PYDANTIC_CARRIER_BASES = frozenset(type.__dict__["__mro__"].__get__(BaseModel))
"""Exact framework and builtin bases whose implementation namespaces are trusted."""

_PYDANTIC_METACLASS_BASES = frozenset(type.__dict__["__mro__"].__get__(type(BaseModel)))
"""Exact Pydantic and builtin metaclasses whose namespaces are framework-owned."""

_PYDANTIC_GENERATED_ATTRIBUTES = frozenset(
    {
        "_abc_impl",
        "__abstractmethods__",
        "__class_vars__",
        "__private_attributes__",
        "__pydantic_complete__",
        "__pydantic_computed_fields__",
        "__pydantic_core_schema__",
        "__pydantic_custom_init__",
        "__pydantic_decorators__",
        "__pydantic_fields__",
        "__pydantic_generic_metadata__",
        "__pydantic_parent_namespace__",
        "__pydantic_post_init__",
        "__pydantic_serializer__",
        "__pydantic_setattr_handlers__",
        "__pydantic_validator__",
        "__signature__",
        "__weakref__",
    }
)
"""Exact framework-generated class fields, never caller-defined name patterns."""


def _pristine_generated_schema() -> type[BaseModel]:
    """Build a nested baseline that includes Pydantic's generated parent namespace."""
    marker = str

    class PristineGeneratedSchema(BaseModel):
        value: marker

    return PristineGeneratedSchema


_PristineGeneratedSchema = _pristine_generated_schema()


def _generated_attribute_types() -> Mapping[str, frozenset[type]]:
    """Derive each generated field's exact top-level shapes from pristine Pydantic state."""
    expected: dict[str, set[type]] = {}
    for owner in (BaseModel, _PristineGeneratedSchema):
        namespace = type.__dict__["__dict__"].__get__(owner)
        for name in _PYDANTIC_GENERATED_ATTRIBUTES:
            if name in namespace:
                expected.setdefault(name, set()).add(type(namespace[name]))
    expected.setdefault("__signature__", set()).add(inspect.Signature)
    return {name: frozenset(kinds) for name, kinds in expected.items()}


_GENERATED_ATTRIBUTE_TYPES = _generated_attribute_types()
"""Per-name framework shapes allowed before recursive generated-state attestation."""


def _collect_trusted_generated_types(
    value: Any,
    trusted: set[type],
    seen: dict[int, Any],
    depth: int = 0,
) -> None:
    """Collect exact value types from pristine generated state without type dispatch."""
    if depth > _MAX_STATIC_ATTESTATION_DEPTH:
        return
    identity = id(value)
    if identity in seen:
        return
    seen[identity] = value
    kind = type(value)
    trusted.add(kind)
    if kind in (dict, types.MappingProxyType):
        for pair in value.items():
            for item in pair:
                _collect_trusted_generated_types(item, trusted, seen, depth + 1)
        return
    if kind in (tuple, list, set, frozenset):
        for item in value:
            _collect_trusted_generated_types(item, trusted, seen, depth + 1)
        return
    if kind in (
        str,
        bytes,
        int,
        float,
        bool,
        complex,
        type(None),
        types.CodeType,
        types.FunctionType,
        types.MethodType,
    ) or isinstance(value, type):
        return
    namespace = None
    with contextlib.suppress(AttributeError, TypeError):
        namespace = object.__getattribute__(value, "__dict__")
    if type(namespace) is dict:
        _collect_trusted_generated_types(namespace, trusted, seen, depth + 1)
    for owner in type.__dict__["__mro__"].__get__(kind):
        if owner is object:
            break
        owner_namespace = type.__dict__["__dict__"].__get__(owner)
        slots = owner_namespace.get("__slots__")
        if type(slots) is str:
            slot_names = (slots,)
        elif type(slots) is tuple and all(type(name) is str for name in slots):
            slot_names = slots
        else:
            continue
        for slot in slot_names:
            if slot in ("__dict__", "__weakref__"):
                continue
            with contextlib.suppress(AttributeError):
                item = object.__getattribute__(value, slot)
                _collect_trusted_generated_types(item, trusted, seen, depth + 1)


def _trusted_generated_carrier_types() -> frozenset[type]:
    """Derive exact trusted types only from pristine framework-generated values."""
    trusted: set[type] = set()
    seen: dict[int, Any] = {}
    for owner in (BaseModel, _PristineGeneratedSchema, type(BaseModel)):
        namespace = type.__dict__["__dict__"].__get__(owner)
        for name, value in namespace.items():
            if owner is type(BaseModel) or name in _PYDANTIC_GENERATED_ATTRIBUTES:
                _collect_trusted_generated_types(value, trusted, seen)
    return frozenset(trusted)


_TRUSTED_GENERATED_CARRIER_TYPES = _trusted_generated_carrier_types()
"""Exact value types allowed to retain generated-field opaque traversal semantics."""


def _drop_callable_plan(token: object) -> None:
    """Remove one collected callable wrapper's plan without retaining the wrapper."""
    _CALLABLE_PLANS.pop(token, None)


def _callable_plan(token: object) -> _CallablePlan:
    """Resolve an opaque wrapper token or fail closed if its plan no longer exists."""
    try:
        return _CALLABLE_PLANS[token]
    except KeyError as error:
        raise ToolGovernanceError(
            "this governed callable's execution plan is unavailable"
        ) from error


def _attest_public_value(
    value: Any,
    *,
    annotation: bool = False,
    _active: set[int] | None = None,
    _depth: int = 0,
    _work: list[int] | None = None,
) -> None:
    """Refuse values whose recursively published graph is executable or opaque."""
    work = [0] if _work is None else _work
    work[0] += 1
    if work[0] > _MAX_ATTESTATION_WORK:
        raise ToolGovernanceError(
            "callable publication metadata attestation exceeded its work bound"
        )
    if _depth > _MAX_STATIC_ATTESTATION_DEPTH:
        raise ToolGovernanceError(
            "callable publication metadata attestation exceeded its depth bound"
        )
    if value is inspect.Signature.empty or value is Any or value is None or value is Ellipsis:
        return
    if type(value) in (str, bytes, int, float, bool, complex):
        return
    items: Iterable[Any]
    if type(value) in (tuple, list, set, frozenset):
        items = value
    elif type(value) is dict:
        items = (item for pair in value.items() for item in pair)
    elif annotation and any(value is atom for atom in _SAFE_ANNOTATION_ATOMS):
        return
    elif annotation:
        origin = get_origin(value)
        if origin is not None:
            if not any(origin is safe for safe in _SAFE_ANNOTATION_ORIGINS):
                raise ToolGovernanceError("callable annotations must be recursively attestable")
            items = get_args(value)
            annotation = True
        else:
            raise ToolGovernanceError(
                "callable publication metadata must be recursively attestable"
            )
    else:
        raise ToolGovernanceError("callable publication metadata must be recursively attestable")

    active = set() if _active is None else _active
    identity = id(value)
    if identity in active:
        raise ToolGovernanceError("callable publication metadata cannot be cyclic")
    active.add(identity)
    try:
        for item in items:
            _attest_public_value(
                item,
                annotation=annotation,
                _active=active,
                _depth=_depth + 1,
                _work=work,
            )
    finally:
        active.remove(identity)


def _reaches_forbidden_static_value(
    value: Any,
    forbidden: tuple[Any, ...],
    seen: dict[int, Any],
    depth: int = 0,
    *,
    opaque_is_safe: bool = False,
    work: list[int] | None = None,
) -> bool:
    """Traverse owned static dictionaries and exact containers without dispatch."""
    work = [0] if work is None else work
    work[0] += 1
    if work[0] > _MAX_ATTESTATION_WORK:
        raise ToolGovernanceError("callable argument schema attestation exceeded its work bound")
    if depth > _MAX_STATIC_ATTESTATION_DEPTH:
        raise ToolGovernanceError("callable argument schema attestation exceeded its depth bound")
    generated_context = opaque_is_safe
    opaque_is_safe = generated_context and type(value) in _TRUSTED_GENERATED_CARRIER_TYPES
    if any(value is candidate for candidate in forbidden):
        return True
    identity = id(value)
    if identity in seen:
        return False
    seen[identity] = value
    try:
        return _reaches_forbidden_active_value(
            value,
            forbidden,
            seen,
            depth,
            opaque_is_safe=opaque_is_safe,
            generated_context=generated_context,
            work=work,
        )
    finally:
        seen.pop(identity)


def _reaches_forbidden_active_value(
    value: Any,
    forbidden: tuple[Any, ...],
    seen: dict[int, Any],
    depth: int,
    *,
    opaque_is_safe: bool,
    generated_context: bool,
    work: list[int],
) -> bool:
    """Traverse one value already retained in the active recursion path."""

    def descend(item: Any) -> bool:
        return _reaches_forbidden_static_value(
            item,
            forbidden,
            seen,
            depth + 1,
            opaque_is_safe=generated_context,
            work=work,
        )

    kind = type(value)
    if kind in (str, bytes, int, float, bool, complex, type(None), types.CodeType):
        return False
    if any(value is atom for atom in _SAFE_ANNOTATION_ATOMS):
        return False
    if kind in (dict, types.MappingProxyType):
        return any(descend(item) for pair in value.items() for item in pair)
    if kind in (tuple, list, set, frozenset):
        return any(descend(item) for item in value)
    if kind is property:
        return any(
            descend(item) for item in (value.fget, value.fset, value.fdel) if item is not None
        )
    if kind in (staticmethod, classmethod):
        return descend(value.__func__)
    if kind is types.MethodType:
        return descend(value.__func__) or descend(value.__self__)
    if kind is types.MethodWrapperType:
        return descend(value.__self__)
    if kind is functools.partial:
        return any(
            descend(item) for item in (value.func, value.args, value.keywords, value.__dict__)
        )
    if kind is types.CellType:
        try:
            captured = value.cell_contents
        except ValueError:
            return False
        return descend(captured)
    if kind is weakref.ReferenceType:
        referent = value()
        callback = value.__callback__
        return (referent is not None and descend(referent)) or (
            callback is not None and descend(callback)
        )
    if kind is inspect.Signature:
        return descend(object.__getattribute__(value, "_parameters")) or descend(
            object.__getattribute__(value, "_return_annotation")
        )
    if kind is inspect.Parameter:
        parameter_kind = object.__getattribute__(value, "_kind")
        if not any(
            parameter_kind is candidate
            for candidate in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.KEYWORD_ONLY,
                inspect.Parameter.VAR_KEYWORD,
            )
        ):
            raise ToolGovernanceError("callable argument schema carries an invalid parameter")
        return any(
            descend(object.__getattribute__(value, slot))
            for slot in ("_name", "_default", "_annotation")
        )
    if kind is types.FunctionType:
        return any(
            descend(item)
            for item in (
                value.__code__,
                value.__defaults__,
                value.__kwdefaults__,
                value.__annotations__,
                value.__closure__,
                value.__dict__,
            )
            if item is not None
        )
    if kind is types.BuiltinFunctionType:
        owner = value.__self__
        if owner is None or any(owner is base for base in _BUILTIN_CARRIER_BASES):
            return False
        return descend(owner)
    if kind in (
        types.MemberDescriptorType,
        types.GetSetDescriptorType,
        types.WrapperDescriptorType,
        types.MethodDescriptorType,
        types.ClassMethodDescriptorType,
    ):
        owner = value.__objclass__
        if any(owner is base for base in _BUILTIN_CARRIER_BASES):
            return False
        return descend(owner)
    if _is_implementation(value):
        raise ToolGovernanceError("callable argument schema carries unknown executable state")
    if isinstance(value, type):
        mro = type.__dict__["__mro__"].__get__(value)
        if generated_context and any(base is BaseModel for base in mro):
            # Nested models are independently attested and rebuilt when their
            # containing FieldInfo is published. Descending them from a generated
            # Pydantic graph instead walks framework caches unrelated to the field.
            return False
        return any(descend(namespace) for namespace in _class_namespaces(value))
    descriptor_get = None
    for owner in type.__dict__["__mro__"].__get__(type(value)):
        owner_namespace = type.__dict__["__dict__"].__get__(owner)
        if "__get__" in owner_namespace:
            descriptor_get = owner_namespace["__get__"]
            break
    if descriptor_get is not None:
        builtin_descriptor = kind in (
            types.MemberDescriptorType,
            types.GetSetDescriptorType,
            types.WrapperDescriptorType,
            types.MethodDescriptorType,
        )
        if not _is_implementation(descriptor_get) and not opaque_is_safe and not builtin_descriptor:
            raise ToolGovernanceError(
                "callable argument schema carries an uninspectable descriptor"
            )
    is_callable = callable(value)
    if is_callable:
        call = None
        for owner in type.__dict__["__mro__"].__get__(type(value)):
            owner_namespace = type.__dict__["__dict__"].__get__(owner)
            if "__call__" in owner_namespace:
                call = owner_namespace["__call__"]
                break
        if (call is None or not _is_implementation(call)) and not opaque_is_safe:
            raise ToolGovernanceError(
                "callable argument schema carries an uninspectable executable"
            )
    if not opaque_is_safe:
        for owner_namespace in _class_namespaces(type(value)):
            if descend(owner_namespace):
                return True
    namespace = None
    with contextlib.suppress(AttributeError, TypeError):
        namespace = object.__getattribute__(value, "__dict__")
    if namespace is not None:
        if type(namespace) is not dict:
            raise ToolGovernanceError("callable argument schema carries an opaque dictionary")
        if descend(namespace):
            return True
    found_static_state = namespace is not None
    for owner in type.__dict__["__mro__"].__get__(type(value)):
        if owner is object:
            break
        owner_namespace = type.__dict__["__dict__"].__get__(owner)
        slots = owner_namespace.get("__slots__")
        if slots is None:
            continue
        if type(slots) is str:
            slot_names = (slots,)
        elif type(slots) is tuple and all(type(name) is str for name in slots):
            slot_names = slots
        else:
            raise ToolGovernanceError("callable argument schema carries opaque slots")
        for slot in slot_names:
            if slot in ("__dict__", "__weakref__"):
                continue
            found_static_state = True
            try:
                slot_value = object.__getattribute__(value, slot)
            except AttributeError:
                continue
            if descend(slot_value):
                return True
    if found_static_state:
        return False
    if kind in (
        types.MemberDescriptorType,
        types.GetSetDescriptorType,
        types.WrapperDescriptorType,
        types.MethodDescriptorType,
        types.BuiltinFunctionType,
    ):
        return False
    if opaque_is_safe:
        return False
    raise ToolGovernanceError("callable argument schema carries opaque static state")


def _collect_executable_codes(
    value: Any,
    codes: list[types.CodeType],
    seen: set[int],
    depth: int = 0,
) -> None:
    """Collect code identities from statically reachable executable shapes."""
    if depth > _MAX_STATIC_ATTESTATION_DEPTH:
        raise ToolGovernanceError(
            "callable implementation code collection exceeded its depth bound"
        )
    identity = id(value)
    if identity in seen:
        return
    seen.add(identity)
    kind = type(value)
    if kind is types.CodeType:
        if not any(value is code for code in codes):
            codes.append(value)
        return
    if kind is types.FunctionType:
        _collect_executable_codes(value.__code__, codes, seen, depth + 1)
        for captured in (value.__defaults__, value.__kwdefaults__, value.__closure__):
            if captured is not None:
                _collect_executable_codes(captured, codes, seen, depth + 1)
        return
    if kind is types.MethodType:
        _collect_executable_codes(value.__func__, codes, seen, depth + 1)
        return
    if kind is functools.partial:
        _collect_executable_codes(value.func, codes, seen, depth + 1)
        return
    if kind in (staticmethod, classmethod):
        _collect_executable_codes(value.__func__, codes, seen, depth + 1)
        return
    if kind is types.CellType:
        with contextlib.suppress(ValueError):
            _collect_executable_codes(value.cell_contents, codes, seen, depth + 1)
        return
    if kind in (tuple, list, set, frozenset):
        for item in value:
            _collect_executable_codes(item, codes, seen, depth + 1)
        return
    if kind is dict:
        for item in value.values():
            _collect_executable_codes(item, codes, seen, depth + 1)
        return
    if not callable(value):
        return
    call = None
    for owner in type.__dict__["__mro__"].__get__(type(value)):
        namespace = type.__dict__["__dict__"].__get__(owner)
        if "__call__" in namespace:
            call = namespace["__call__"]
            break
    if call is not None:
        _collect_executable_codes(call, codes, seen, depth + 1)


def _class_namespaces(value: type) -> Iterable[Mapping[str, Any]]:
    """Yield caller-owned class and metaclass namespaces, derived statically."""
    for base in type.__dict__["__mro__"].__get__(value):
        if base in _BUILTIN_CARRIER_BASES or base in _PYDANTIC_CARRIER_BASES:
            continue
        yield type.__dict__["__dict__"].__get__(base)
    for metaclass in type.__dict__["__mro__"].__get__(type(value)):
        if metaclass in _PYDANTIC_METACLASS_BASES:
            continue
        yield type.__dict__["__dict__"].__get__(metaclass)


def _attest_schema_namespace(
    namespace: Mapping[str, Any],
    forbidden: tuple[Any, ...],
    seen: dict[int, Any],
    work: list[int],
) -> None:
    """Attest caller-owned schema entries while exempting exact framework products."""
    for name, value in namespace.items():
        if name == "__annotations__":
            if type(value) is not dict or any(type(field_name) is not str for field_name in value):
                raise ToolGovernanceError("callable argument schema annotations are malformed")
            # Field annotations are normalized from Pydantic's exact FieldInfo
            # mapping during publication; traversing typing aliases here reaches
            # interpreter-owned caches that are neither retained nor published.
            continue
        if name in _PYDANTIC_GENERATED_ATTRIBUTES:
            expected = _GENERATED_ATTRIBUTE_TYPES.get(name, ())
            if type(value) not in expected:
                raise ToolGovernanceError(
                    "callable argument schema generated fields have unexpected values"
                )
            if name == "__pydantic_fields__":
                # Every retained FieldInfo component is independently read,
                # normalized, bounded, and rebuilt by _snapshot_field.
                continue
            if name == "__signature__" and type(value) is not inspect.Signature:
                # Pydantic's lazy signature descriptor closes over generated
                # field caches. The snapshot creates its own descriptor; only an
                # explicitly installed concrete Signature is publication input.
                continue
            if _reaches_forbidden_static_value(
                value, forbidden, seen, opaque_is_safe=True, work=work
            ):
                raise ToolGovernanceError(
                    "callable argument schemas cannot retain executable sources"
                )
            continue
        if _reaches_forbidden_static_value(name, forbidden, seen, work=work) or (
            _reaches_forbidden_static_value(value, forbidden, seen, work=work)
        ):
            raise ToolGovernanceError("callable argument schemas cannot retain executable sources")


def _attest_pydantic_schema(schema: type[BaseModel], forbidden: tuple[Any, ...]) -> None:
    """Attest one Pydantic class graph before using or publishing it."""
    seen: dict[int, Any] = {}
    work = [0]
    for namespace in _class_namespaces(schema):
        _attest_schema_namespace(namespace, forbidden, seen, work)


def _schema_has_custom_behavior(namespace: Mapping[str, Any]) -> bool:
    """Return whether rebuilding fields/config alone would lose schema behavior."""
    if namespace["__private_attributes__"] or namespace["__pydantic_computed_fields__"]:
        return True
    decorators = namespace["__pydantic_decorators__"]
    slots = type(decorators).__dict__.get("__slots__", ())
    return any(bool(object.__getattribute__(decorators, slot)) for slot in slots)


_SAFE_FIELD_FACTORIES = (list, dict, set, tuple, frozenset)
_SAFE_FIELD_METADATA = frozenset(
    {
        "strict",
        "gt",
        "ge",
        "lt",
        "le",
        "multiple_of",
        "allow_inf_nan",
        "max_digits",
        "decimal_places",
        "min_length",
        "max_length",
        "pattern",
        "coerce_numbers_to_str",
        "union_mode",
        "fail_fast",
    }
)
_SAFE_FIELD_ATTRIBUTES = frozenset(
    {
        "alias",
        "alias_priority",
        "validation_alias",
        "serialization_alias",
        "title",
        "description",
        "examples",
        "exclude",
        "discriminator",
        "deprecated",
        "json_schema_extra",
        "frozen",
        "validate_default",
        "repr",
        "init",
        "init_var",
        "kw_only",
    }
)


def _snapshot_public_data(value: Any, *, work: list[int]) -> Any:
    """Rebuild exact data containers without invoking a caller copy protocol."""
    work[0] += 1
    if work[0] > _MAX_ATTESTATION_WORK:
        raise ToolGovernanceError("callable argument schema snapshot exceeded its work bound")
    kind = type(value)
    if value is PydanticUndefined or kind in (str, bytes, int, float, bool, complex, type(None)):
        return value
    if kind is list:
        return [_snapshot_public_data(item, work=work) for item in value]
    if kind is tuple:
        return tuple(_snapshot_public_data(item, work=work) for item in value)
    if kind is dict:
        return {
            _snapshot_public_data(key, work=work): _snapshot_public_data(item, work=work)
            for key, item in value.items()
        }
    if kind is set:
        return {_snapshot_public_data(item, work=work) for item in value}
    if kind is frozenset:
        return frozenset(_snapshot_public_data(item, work=work) for item in value)
    raise ToolGovernanceError("callable argument schema carries unsupported public data")


def _field_metadata_arguments(field: FieldInfo, *, work: list[int]) -> dict[str, Any]:
    """Translate a closed set of immutable Pydantic constraints into Field kwargs."""
    arguments: dict[str, Any] = {}
    metadata = object.__getattribute__(field, "metadata")
    if type(metadata) is not list:
        raise ToolGovernanceError("callable argument schema field metadata is unsupported")
    for item in metadata:
        namespace = None
        with contextlib.suppress(AttributeError):
            namespace = object.__getattribute__(item, "__dict__")
        pairs: list[tuple[str, Any]] = []
        if type(namespace) is dict:
            pairs.extend(namespace.items())
        for owner in type.__dict__["__mro__"].__get__(type(item)):
            slots = type.__dict__["__dict__"].__get__(owner).get("__slots__", ())
            if type(slots) is str:
                slots = (slots,)
            if type(slots) is not tuple:
                raise ToolGovernanceError("callable argument schema field metadata is unsupported")
            for slot in slots:
                if slot in ("__dict__", "__weakref__"):
                    continue
                with contextlib.suppress(AttributeError):
                    pairs.append((slot, object.__getattribute__(item, slot)))
        if not pairs:
            raise ToolGovernanceError("callable argument schema field metadata is unsupported")
        for name, value in pairs:
            if name not in _SAFE_FIELD_METADATA or name in arguments:
                raise ToolGovernanceError("callable argument schema field metadata is unsupported")
            arguments[name] = _snapshot_public_data(value, work=work)
    return arguments


def _snapshot_annotation(
    annotation: Any,
    forbidden: tuple[Any, ...],
    memo: list[tuple[type[BaseModel], type[BaseModel]]],
    active: list[type[BaseModel]],
    work: list[int],
) -> Any:
    """Rebuild typing graphs, recursively replacing every nested model class."""
    if isinstance(annotation, type):
        mro = type.__dict__["__mro__"].__get__(annotation)
        if any(base is BaseModel for base in mro):
            return _snapshot_pydantic_schema(annotation, forbidden, memo, active, work)
    if any(annotation is atom for atom in (*_SAFE_ANNOTATION_ATOMS, Any)):
        return annotation
    origin = get_origin(annotation)
    if origin is None or not any(origin is safe for safe in _SAFE_ANNOTATION_ORIGINS):
        raise ToolGovernanceError("callable argument schema annotations are unsupported")
    raw_arguments = get_args(annotation)
    if origin is typing.Literal:
        arguments = tuple(_snapshot_public_data(item, work=work) for item in raw_arguments)
        return typing.Literal[arguments]
    if origin is Callable:
        parameters, result = raw_arguments
        if parameters is Ellipsis:
            published_parameters: Any = Ellipsis
        elif type(parameters) in (list, tuple):
            published_parameters = [
                _snapshot_annotation(item, forbidden, memo, active, work) for item in parameters
            ]
        else:
            raise ToolGovernanceError("callable argument schema annotations are unsupported")
        published_result = _snapshot_annotation(result, forbidden, memo, active, work)
        return typing.Callable[published_parameters, published_result]
    arguments = tuple(
        _snapshot_annotation(item, forbidden, memo, active, work)
        if item is not Ellipsis
        else Ellipsis
        for item in get_args(annotation)
    )
    if origin is types.UnionType:
        rebuilt = arguments[0]
        for item in arguments[1:]:
            rebuilt = rebuilt | item
        return rebuilt
    base = typing.Union if origin is typing.Union else origin
    try:
        return base[arguments if len(arguments) != 1 else arguments[0]]
    except (AttributeError, TypeError, ValueError) as error:
        raise ToolGovernanceError("callable argument schema annotations are unsupported") from error


def _snapshot_field(
    field: FieldInfo,
    forbidden: tuple[Any, ...],
    memo: list[tuple[type[BaseModel], type[BaseModel]]],
    active: list[type[BaseModel]],
    work: list[int],
) -> tuple[Any, FieldInfo]:
    if type(field) is not FieldInfo:
        raise ToolGovernanceError("callable argument schema field type is unsupported")
    annotation = _snapshot_annotation(
        object.__getattribute__(field, "annotation"), forbidden, memo, active, work
    )
    arguments = _field_metadata_arguments(field, work=work)
    for name in _SAFE_FIELD_ATTRIBUTES:
        value = object.__getattribute__(field, name)
        if value is not None:
            arguments[name] = _snapshot_public_data(value, work=work)
    for callback_name in ("field_title_generator", "exclude_if"):
        if object.__getattribute__(field, callback_name) is not None:
            raise ToolGovernanceError("callable argument schema field callbacks are unsupported")
    factory = object.__getattribute__(field, "default_factory")
    default = object.__getattribute__(field, "default")
    if factory is not None:
        if not any(factory is candidate for candidate in _SAFE_FIELD_FACTORIES):
            raise ToolGovernanceError("callable argument schema default factories are unsupported")
        arguments["default_factory"] = factory
        published = Field(**arguments)
    else:
        published = Field(_snapshot_public_data(default, work=work), **arguments)
    return annotation, published


def _snapshot_pydantic_schema(
    schema: type[BaseModel],
    forbidden: tuple[Any, ...],
    memo: list[tuple[type[BaseModel], type[BaseModel]]],
    active: list[type[BaseModel]],
    work: list[int],
) -> type[BaseModel]:
    if type(schema) is not type(BaseModel):
        raise ToolGovernanceError("custom argument schema metaclasses are unsupported")
    for original, published in memo:
        if schema is original:
            return published
    if any(schema is candidate for candidate in active):
        raise ToolGovernanceError("recursive callable argument schemas are unsupported")
    active.append(schema)
    try:
        _attest_pydantic_schema(schema, forbidden)
        namespace = type.__dict__["__dict__"].__get__(schema)
        if _schema_has_custom_behavior(namespace):
            raise ToolGovernanceError(
                "callable argument schemas with custom behavior cannot be snapshotted"
            )
        source_fields = namespace.get("__pydantic_fields__")
        if type(source_fields) is not dict:
            raise ToolGovernanceError("callable argument schema fields are unsupported")
        fields = {
            name: _snapshot_field(field, forbidden, memo, active, work)
            for name, field in source_fields.items()
        }
        source_config = namespace.get("model_config", {})
        if type(source_config) is not dict:
            raise ToolGovernanceError("callable argument schema config is unsupported")
        config = _snapshot_public_data(source_config, work=work)
        snapshot = create_model(
            type.__dict__["__name__"].__get__(schema),
            __config__=config,
            __doc__=type.__dict__["__doc__"].__get__(schema),
            __module__=type.__dict__["__module__"].__get__(schema),
            __qualname__=type.__dict__["__qualname__"].__get__(schema),
            **fields,
        )
        memo.append((schema, snapshot))
        _attest_pydantic_schema(snapshot, forbidden)
        return snapshot
    except ToolGovernanceError:
        raise
    except Exception as error:
        raise ToolGovernanceError(
            "callable argument schema cannot be snapshotted safely"
        ) from error
    finally:
        active.pop()


def _snapshot_args_schema(schema: Any, forbidden: tuple[Any, ...]) -> Any:
    """Return an independent, attestable publication schema or fail closed."""
    _gate_args_schema(schema)
    if schema is None:
        return None
    if type(schema) is dict:
        _attest_public_value(schema)
        return _snapshot_public_data(schema, work=[0])
    if isinstance(schema, type):
        mro = type.__dict__["__mro__"].__get__(schema)
        if any(base is BaseModel for base in mro):
            return _snapshot_pydantic_schema(schema, forbidden, [], [], [0])
    raise ToolGovernanceError("callable argument schemas must be recursively attestable")


def _gate_args_schema(schema: Any) -> None:
    """Reject unsupported schema carriers before any carrier-owned dispatch."""
    if schema is None or type(schema) is dict:
        return
    if not isinstance(schema, type) or type(schema) is not type(BaseModel):
        raise ToolGovernanceError("callable argument schema carrier is unsupported")
    mro = type.__dict__["__mro__"].__get__(schema)
    if not any(base is BaseModel for base in mro):
        raise ToolGovernanceError("callable argument schema carrier is unsupported")


def _annotation_namespace(target: Any) -> Mapping[str, Any]:
    """Return one exact function global mapping for static annotation name lookup."""
    function = target.__func__ if type(target) is types.MethodType else target
    if type(function) is not types.FunctionType:
        return {}
    namespace = object.__getattribute__(function, "__globals__")
    return namespace if type(namespace) is dict else {}


def _resolve_annotation_node(node: ast.AST, namespace: Mapping[str, Any]) -> Any:
    """Interpret a closed annotation grammar without evaluating caller expressions."""
    if type(node) is ast.Name:
        name = node.id
        for atom in (*_SAFE_ANNOTATION_ATOMS, Any):
            if getattr(atom, "__name__", None) == name:
                return atom
        if name in namespace:
            return namespace[name]
        raise ToolGovernanceError("callable annotation names must resolve statically")
    if type(node) is ast.Attribute:
        if type(node.value) is not ast.Name or node.value.id != "typing":
            raise ToolGovernanceError("callable annotation attributes must be typing members")
        if node.attr.startswith("_") or node.attr not in vars(typing):
            raise ToolGovernanceError("callable annotation names must resolve statically")
        return vars(typing)[node.attr]
    if type(node) is ast.Constant:
        if (
            node.value is None
            or node.value is Ellipsis
            or type(node.value)
            in (
                str,
                bytes,
                int,
                float,
                bool,
                complex,
            )
        ):
            return node.value
        raise ToolGovernanceError("callable annotation literals must be recursively safe")
    if type(node) is ast.Tuple:
        return tuple(_resolve_annotation_node(item, namespace) for item in node.elts)
    if type(node) is ast.List:
        return [_resolve_annotation_node(item, namespace) for item in node.elts]
    if type(node) is ast.BinOp and type(node.op) is ast.BitOr:
        left = _resolve_annotation_node(node.left, namespace)
        right = _resolve_annotation_node(node.right, namespace)
        if not any(left is atom for atom in _SAFE_ANNOTATION_ATOMS) and get_origin(left) is None:
            raise ToolGovernanceError("callable annotation unions must be recursively safe")
        if not any(right is atom for atom in _SAFE_ANNOTATION_ATOMS) and get_origin(right) is None:
            raise ToolGovernanceError("callable annotation unions must be recursively safe")
        return left | right
    if type(node) is ast.Subscript:
        origin = _resolve_annotation_node(node.value, namespace)
        if not any(origin is candidate for candidate in _SAFE_ANNOTATION_SUBSCRIPT_BASES):
            raise ToolGovernanceError("callable annotation subscriptions must be recursively safe")
        arguments = _resolve_annotation_node(node.slice, namespace)
        try:
            return origin[arguments]
        except (AttributeError, TypeError, ValueError) as error:
            raise ToolGovernanceError("callable annotation subscription is invalid") from error
    raise ToolGovernanceError("callable annotations cannot contain executable expressions")


def _resolve_annotation(value: Any, target: Any) -> Any:
    """Normalize a postponed annotation into a safe object or fail closed."""
    if type(value) is not str:
        return value
    namespace = _annotation_namespace(target)
    pending = value
    for _ in range(4):
        try:
            expression = ast.parse(pending, mode="eval")
        except SyntaxError as error:
            raise ToolGovernanceError(
                "callable annotation strings must be valid expressions"
            ) from error
        resolved = _resolve_annotation_node(expression.body, namespace)
        if type(resolved) is not str:
            return resolved
        pending = resolved
    raise ToolGovernanceError("callable forward annotations must resolve statically")


def _attested_annotations(target: Any) -> dict[str, Any]:
    """Copy only an exact, recursively attestable annotation mapping."""
    annotations = getattr(target, "__annotations__", None)
    if annotations is None:
        return {}
    if type(annotations) is not dict:
        raise ToolGovernanceError("callable annotations must be an exact dictionary")
    copied: dict[str, Any] = {}
    work = [0]
    for name, value in annotations.items():
        if type(name) is not str:
            raise ToolGovernanceError("callable annotation names must be exactly str")
        value = _resolve_annotation(value, target)
        _attest_public_value(value, annotation=True, _work=work)
        copied[name] = value
    return copied


def _attest_signature_defaults(target: Any) -> None:
    """Refuse any default that could publish executable or opaque caller state."""
    try:
        signature = _declared_signature(target)
    except ToolGovernanceError:
        # A callable with no establishable signature publishes no signature; its
        # existing invocation-time refusal remains the fail-closed boundary.
        return
    work = [0]
    for parameter in signature.parameters.values():
        _attest_public_value(parameter.default, _work=work)


def _strip_frozen_callable_attributes(source: Any) -> None:
    """Remove caller-owned function attributes copied into a fresh frozen source."""
    function = source.__func__ if type(source) is types.MethodType else source
    if type(function) is types.FunctionType:
        function.__dict__.clear()


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
    if type(value) is types.FunctionType or type(value) is types.MethodType:
        return value
    return False


def _carried_fields(tool: Any) -> dict[str, Any]:
    """Copy the ``BaseTool`` fields a *caller* reads off the wrapper, each exact-type gated.

    Whether a field has to be carried is settled by who reads it. The agent loop
    reads ``return_direct`` off the tool object it was handed -- the wrapper --
    and never off the delegate it cannot see, so leaving it at its default
    silently changes control flow. ``handle_validation_error`` belongs to the
    layer that parses the input, and the wrapper parses first, with the same
    schema; if it raises, the delegate is never reached to handle anything.

    ``callbacks`` is deliberately excluded: delegate-attached handlers are not
    allowed inside the governance boundary. ``handle_tool_error`` and
    ``response_format`` are carried because direct execution leaves this wrapper
    as the one and only ``BaseTool`` layer responsible for error/output shaping.

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
        "handle_tool_error": _error_handler(_peek(tool, "handle_tool_error")),
        "response_format": (
            value
            if type(value := _peek(tool, "response_format")) is str
            and value in {"content", "content_and_artifact"}
            else "content"
        ),
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
    """Read a ``BaseTool``'s identity: its surface, its whole schema, and its body.

    The surface -- name, description, argument names -- is what a substituted tool
    reproduces exactly, so it is the *weakest* part of what goes in here. The two
    that a substitution cannot reproduce without being the same tool are the
    digest of the code the tool will run and the digest of its complete declared
    schema, types and constraints included. See
    :mod:`~zeroth.integrations.langgraph._tool_fingerprint`.

    Args:
        tool: The tool being described.

    Returns:
        Its identifying surface and the material its identity is built from.

    Raises:
        UnstableToolIdentityError: If the tool's implementation cannot be
            fingerprinted stably, or it declares a schema that cannot be pinned.
            Both fail closed: a surface-only identity is one a substituted tool
            inherits authorization through.
    """
    snapshot = snapshot_tool(tool)
    description = _text(snapshot.description)
    args_schema = snapshot.args_schema
    _gate_args_schema(args_schema)
    return _ToolFacts(
        name=snapshot.name,
        description=description,
        args_schema=args_schema,
        material={
            "surface": _BASE_TOOL_SURFACE,
            "description": description,
            "arguments": _schema_argument_names(args_schema),
            "schema": schema_digest(args_schema),
            "implementation": tool_slots_digest(tool, snapshot.bodies),
        },
        snapshot=snapshot,
    )


def _describe_callable(target: Any) -> _ToolFacts:
    """Read a plain callable's identifying surface, falling back to its ``__name__``.

    A callable may carry an explicit ``name`` / ``description`` / ``args_schema``
    -- a partially decorated function does -- so those are preferred, and the
    dunders stand in only where they are absent.

    Identity is bound to the callable's own code and complete declared schema,
    exactly as it is for a ``BaseTool``: a bare function is the surface easiest of
    all to imitate, since two functions with the same signature and docstring are
    the same tool to every gate but this one.

    Args:
        target: The callable being described.

    Returns:
        Its identifying surface and the material its identity is built from.

    Raises:
        UnstableToolIdentityError: If the callable's implementation cannot be
            fingerprinted stably -- a builtin, a C extension function -- or it
            declares a schema that cannot be pinned.
    """
    description = _text(_peek(target, "description")) or _text(_peek(target, "__doc__"))
    args_schema = _peek(target, "args_schema")
    _gate_args_schema(args_schema)
    name = _peek(target, "name")
    if normalize_identifier(name) is None:
        name = _peek(target, "__name__")
    arguments = _schema_argument_names(args_schema) or _signature_argument_names(target)
    guarded = snapshot_guarded_callable(target)
    body = guarded.body
    return _ToolFacts(
        name=name,
        description=description,
        args_schema=args_schema,
        material={
            "surface": _CALLABLE_SURFACE,
            "description": description,
            "arguments": arguments,
            "schema": schema_digest(args_schema),
            "implementation": callable_implementation_digest(body),
        },
        body=body,
        state_cells=guarded.state_cells,
    )


def _resolved(resolver: Callable[[Any], Any] | None, target: Any) -> object:
    """Run an optional per-tool resolver, refusing a failed metadata read."""
    if resolver is None:
        return None
    try:
        return resolver(target)
    except Exception as error:
        raise ToolGovernanceError("tool metadata resolver failed") from error


def _pin(
    facts: _ToolFacts, target: object = None, seams: _Seams | None = None
) -> GovernedToolBinding:
    """Fix the identity and, for a live wrapper, its reviewed static metadata.

    The identity is the pin: every call re-derives it and refuses if it moved.

    A declaration-only binding has no seams and therefore carries conservative
    defaults. A wrapper binding resolves each tool-only metadata seam into the
    reviewed inventory value; every live action rechecks those normalized values.

    Args:
        facts: The tool's already-gated identifying surface.
        target: The tool handed to the metadata resolvers.
        seams: The wrapper's metadata resolvers, or ``None`` for identity-only
            declaration records.

    Returns:
        The binding whose identity every call through the wrapper is checked
        against.

    Raises:
        UnstableToolIdentityError: If the tool carries no usable name, or its
            identifying material is not canonically representable.
    """
    identity = normalize_tool_identity(facts.name, facts.material)
    if seams is None:
        return GovernedToolBinding(identity=identity)
    side_effect = _resolved(seams.side_effect, target)
    contract = _resolved(seams.contract_ref, target)
    capabilities = _resolved(seams.capability_refs, target)
    approval = _resolved(seams.requires_approval, target)
    return GovernedToolBinding(
        identity=identity,
        side_effect=classify_side_effect(side_effect),
        contract_ref=normalize_contract_ref(contract),
        capability_refs=normalize_capability_refs(() if capabilities is None else capabilities),
        requires_approval=normalize_requires_approval(False if approval is None else approval),
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


@dataclass(frozen=True, slots=True)
class _EffectiveCall:
    """One call to a plain callable, in the two forms a governed invocation needs.

    ``arguments`` is what the policy is shown; ``args`` and ``kwargs`` are what
    the body is invoked with. They are carried together because they must be the
    same call: deriving them from two separate reads is what let a value exist on
    one side and not the other.

    Attributes:
        arguments: The named mapping a decision is made about.
        args: The positional half of the call to re-issue.
        kwargs: The named half of the call to re-issue.
    """

    arguments: dict[str, Any]
    args: tuple[Any, ...]
    kwargs: Mapping[str, Any]


_VARIADIC_CONTAINERS: tuple[tuple[Any, type], ...] = (
    (inspect.Parameter.VAR_POSITIONAL, tuple),
    (inspect.Parameter.VAR_KEYWORD, dict),
)
"""The two variadic parameter kinds, paired with the exact container each binds into.

Scanned with ``is`` on both halves rather than tested by truthiness alone, for the
reason :data:`~zeroth.integrations.langgraph._tool_normalize._CANONICAL_HANDLERS`
gives: an emptiness test on a value of some other type reaches whatever
``__bool__`` or ``__len__`` that type defines, and nothing here needs to consult a
caller's code to decide whether the binding invented an entry.
"""


def _drop_empty_variadics(bound: inspect.BoundArguments) -> None:
    """Forget the empty ``*args`` / ``**kwargs`` entries ``apply_defaults`` invents.

    ``apply_defaults`` materializes an empty tuple under a ``VAR_POSITIONAL``
    parameter and an empty dict under a ``VAR_KEYWORD`` one even when the caller
    passed neither. Leaving them in would put ``{"args": [], "kwargs": {}}`` into
    the mapping handed to a policy for every variadic tool that exists, changing
    the decided shape -- and therefore the argument fingerprint -- of calls this
    module was not asked to change.

    Deleting them cannot change what executes. ``BoundArguments.args`` extends by
    an empty tuple and ``.kwargs`` updates by an empty dict when the entries are
    present, and both stop at a missing variadic parameter when they are not, so
    the re-issued call is identical either way. It is a projection of the record,
    not an edit of the call.

    A *non-empty* variadic is left exactly as it is -- nested under its parameter
    name, which is the shape this surface has always reported. Flattening it into
    the mapping would describe the call more faithfully, and is deliberately not
    done here: it would move the decided shape of existing calls, which is a
    behaviour change beyond the finding and could break a policy written against
    what the surface reports today.

    Args:
        bound: The binding to project, mutated in place.
    """
    for name, parameter in bound.signature.parameters.items():
        value = bound.arguments.get(name)
        for kind, container in _VARIADIC_CONTAINERS:
            if parameter.kind is kind and type(value) is container and not value:
                del bound.arguments[name]


def _declared_signature(target: Any) -> inspect.Signature:
    """Read the parameters of the thing that runs, not of the label it carries.

    ``inspect.signature`` answers from ``__signature__`` when there is one and
    follows ``__wrapped__`` when there is one, and only reaches the ``__code__``
    the body is actually made of if there is neither. Both are ordinary writable
    attributes on an ordinary admitted Python function, so binding the call
    against what they say was a *second read* of the same shape every other
    finding in this module is about: the description said one thing, the
    implementation did another, and the value the difference was worth was a
    parameter default the body materialized for itself.

    The signature is therefore taken from ``snapshot_callable``'s rebuild, which
    is the object execution is authorized against, and which no longer carries
    either attribute -- see ``_DISOWNING_ATTRIBUTES`` in
    :mod:`~zeroth.integrations.langgraph._tool_execution`. Freezing here as well
    as in :func:`_governed_action` is deliberate rather than wasteful: this call
    needs a clean signature *before* the arguments exist to decide on, and a
    target that moved between the two freezes is caught by the identity check the
    decision already makes.

    Both attributes have to go, not just the one that raises. A ``__signature__``
    naming fewer parameters than the body has raises nothing at all: it binds, it
    applies no defaults, and it hands the policy an empty call. Neither
    ``follow_wrapped=False`` nor catching the error would have seen it.

    Args:
        target: The callable whose real parameters define the call.

    Returns:
        The signature of the executable, as the freeze rebuilt it.

    Raises:
        ToolGovernanceError: If the executable's own signature cannot be
            established. Refusing is the point: the deleted alternative named the
            arguments positionally and re-issued the caller's originals, so a
            callable that would not answer the question got its defaults applied
            by nobody and inspected by no policy.
        UnstableToolIdentityError: If the callable cannot be frozen at all.
    """
    try:
        return inspect.signature(snapshot_callable(target))
    except (TypeError, ValueError) as error:
        raise ToolGovernanceError("this callable's own signature cannot be established") from error


def _published_signature(
    target: Any, annotations: Mapping[str, Any] | None = None
) -> inspect.Signature:
    """Return the executable's real call shape with its declared type metadata.

    :func:`_declared_signature` deliberately reads parameter shape and defaults
    from a frozen executable that carries neither ``__signature__`` nor
    ``__wrapped__``.  The freeze also omits function annotations, which do not
    choose what executes but are required by LangChain's schema inference.  Put
    those declarations back onto the already-trusted shape without consulting a
    caller-supplied signature object.
    """
    signature = _declared_signature(target)
    if annotations is None:
        annotations = getattr(target, "__annotations__", None)
    if type(annotations) is not dict:
        return signature
    parameters = [
        parameter.replace(annotation=annotations.get(parameter.name, parameter.annotation))
        for parameter in signature.parameters.values()
    ]
    return signature.replace(
        parameters=parameters,
        return_annotation=annotations.get("return", signature.return_annotation),
    )


def _effective_call(
    target: Any, args: tuple[Any, ...], kwargs: Mapping[str, Any]
) -> _EffectiveCall:
    """Resolve one call to a plain callable *once*, for both the policy and the body.

    Binding against the callable's own signature is what keeps this surface and
    the middleware surface describing the same call: the middleware is handed
    named arguments and never positional ones, so a wrapper that reported
    ``__arg1`` for ``search("cats")`` would decide a differently-shaped call than
    the middleware decides for the same tool.

    **The binding's defaults are applied, and the bound call is the one that
    runs.** Those are one change, not two. ``bind`` alone describes only what the
    caller passed, so ``remove(path="/danger")`` invoked with no arguments was
    decided as an empty call and then executed with ``"/danger"`` -- the policy
    authorized one call and the signature supplied another. Applying the defaults
    without re-issuing the bound call would fix the record and leave the same two
    reads in place; re-issuing it as ``args`` / ``kwargs`` means every parameter
    arrives explicitly and the body's own ``__defaults__`` are never consulted at
    execution. The second read is deleted rather than checked.

    **There is no unbound path left.** A callable with no retrievable signature
    used to fall through to positional naming, on the stated grounds that only a
    builtin or a C function could get there -- and no such callable can: every
    C-implemented shape is refused by ``callable_implementation_digest`` at
    ``govern_tools`` time, before a wrapper exists to call. What actually reached
    that branch was an admitted Python function that had been *told* to have no
    signature, and it arrived at the one place where saying so was worth
    something: the defaults went unapplied, the policy inspected ``{}``, and the
    body supplied its own ``"/danger"``. The branch is deleted rather than
    narrowed, because a weaker path that only an attacker has a reason to reach
    is not a fallback.

    A call that will not bind is refused for the same reason. It is a call the
    body would have rejected anyway -- ``Signature.bind`` is CPython's own
    argument matching -- so nothing that could run is lost, and no mapping this
    function could not derive from the executable is ever put in front of a
    policy.

    Args:
        target: The callable whose signature defines the call.
        args: The positional arguments the caller passed.
        kwargs: The named arguments the caller passed.

    Returns:
        The one call this invocation will be decided on and executed with.

    Raises:
        ToolGovernanceError: If the executable's signature cannot be established,
            if this call does not fit it, or if the arguments cannot be named
            unambiguously.
    """
    signature = _declared_signature(target)
    try:
        bound = signature.bind(*args, **kwargs)
    except TypeError as error:
        raise ToolGovernanceError("this call does not fit the callable's own signature") from error
    bound.apply_defaults()
    _drop_empty_variadics(bound)
    return _EffectiveCall(_call_arguments((), bound.arguments), bound.args, bound.kwargs)


def _callable_facts(plan: _CallablePlan) -> _ToolFacts:
    """Re-snapshot a frozen source and rebuild identity facts from immutable metadata."""
    guarded = snapshot_guarded_callable(plan.source)
    body = guarded.body
    state_cells = tuple(
        {id(cell): cell for cell in (*plan.state_cells, *guarded.state_cells)}.values()
    )
    metadata = plan.metadata
    return _ToolFacts(
        name=metadata.name,
        description=metadata.description,
        args_schema=None,
        material={
            "surface": _CALLABLE_SURFACE,
            "description": metadata.description,
            "arguments": list(metadata.arguments),
            "schema": metadata.schema,
            "implementation": callable_implementation_digest(body),
        },
        body=body,
        state_cells=state_cells,
    )


def _governed_action(
    plan: _GovernedPlan | _CallablePlan,
    arguments: Mapping[str, Any],
    *,
    tool_call_id: str | None = None,
) -> tuple[ToolAction, object, _ToolFacts]:
    """Snapshot the tool, decide about the snapshot, and hand it back to be executed.

    **The pre-resolver snapshot is what runs.** ``plan.facts`` captured the tool's
    body and surface before any metadata resolver ran. The current source is
    checked against the post-resolver baseline on every call, but execution uses
    the original frozen body, so a resolver cannot substitute what runs while it
    describes the tool.

    The reviewed tool-only metadata is re-resolved and compared with the same
    immutable binding the inventory recorder uses. Context and arguments remain
    live per call.

    Args:
        plan: The wrapper's pinned identity and live seams.
        arguments: The named call arguments.
        tool_call_id: The stable framework-injected call identity, when present.

    Returns:
        The normalized action, the governance context it was attributed to, and
        the snapshot the action describes -- which is the object execution runs.

    Raises:
        UnstableToolIdentityError: If the tool's identity is not the one it was
            wrapped under, or it dispatches through machinery governance cannot
            read past.
        GovernanceContextError: If the call cannot be attributed.
        ToolGovernanceError: If the arguments are not canonically representable.
    """
    observed = _callable_facts(plan) if type(plan) is _CallablePlan else plan.describe(plan.target)
    if normalize_tool_identity(observed.name, observed.material) != plan.observed_identity:
        raise UnstableToolIdentityError("the tool's identity changed after it was governed")
    if _pin(plan.facts, plan.target, plan.seams) != plan.binding:
        raise ToolGovernanceError("the tool metadata changed after it was governed")
    context = _resolve_context(plan.seams.context)
    facts = plan.facts
    action = normalize_tool_action(
        name=facts.name,
        arguments=arguments,
        context=context,
        identity_material=facts.material,
        contract_ref=plan.binding.contract_ref,
        side_effect=plan.binding.side_effect,
        capability_refs=plan.binding.capability_refs,
        requires_approval=plan.binding.requires_approval,
        tool_call_id=plan.seams.tool_call_id if tool_call_id is None else tool_call_id,
    )
    if action.identity != plan.binding.identity:
        raise UnstableToolIdentityError("the governed tool binding is inconsistent")
    return action, context, facts


def _enforcement_seams(plan: _GovernedPlan | _CallablePlan) -> dict[str, Any]:
    """Render the plan's seams as the keyword arguments the enforcement core takes."""
    seams = plan.seams
    return {
        "client": seams.client,
        "unknown_side_effect": seams.unknown_side_effect,
        "audit": seams.audit,
        "actor": seams.actor,
        "interrupt": seams.interrupt,
        "approval_lifecycle": seams.approval_lifecycle,
    }


def _edited_kwargs(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Return an ordinary named edit, refusing positional replay ambiguity."""
    edited = dict(arguments)
    if any(key.startswith("__arg") for key in edited):
        raise ToolGovernanceError("edited positional tool arguments cannot be reissued safely")
    return edited


@dataclass(frozen=True, slots=True)
class _PinnedToolInput:
    """The frozen schema surface native LangChain validation needs."""

    args_schema: Any

    @property
    def _injected_args_keys(self) -> frozenset[str]:
        return frozenset()

    def _parse_input(self, tool_input: Any, tool_call_id: str | None) -> Any:
        return BaseTool._parse_input(self, tool_input, tool_call_id)  # type: ignore[arg-type]


def _injected_tool_call_id(
    plan: _GovernedPlan,
    arguments: Mapping[str, Any],
    outer_tool_call_id: str | None,
) -> str | None:
    """Reconcile native outer and schema-injected call identities."""
    planned_tool_call_id = plan.seams.tool_call_id
    if (
        outer_tool_call_id is not None
        and planned_tool_call_id is not None
        and outer_tool_call_id != planned_tool_call_id
    ):
        raise ToolGovernanceError("trusted tool-call identities are inconsistent")
    trusted_tool_call_id = (
        planned_tool_call_id if outer_tool_call_id is None else outer_tool_call_id
    )
    schema = plan.facts.args_schema
    if not isinstance(schema, type):
        return trusted_tool_call_id
    namespace = type.__dict__["__dict__"].__get__(schema)
    fields = namespace.get("__pydantic_fields__")
    if type(fields) is not dict:
        return trusted_tool_call_id
    names = []
    for name, field in fields.items():
        metadata = object.__getattribute__(field, "metadata")
        if (
            type(name) is str
            and type(metadata) is list
            and any(item is InjectedToolCallId for item in metadata)
        ):
            names.append(name)
    if not names:
        return trusted_tool_call_id
    values = [arguments.get(name) for name in names]
    if any(type(value) is not str for value in values):
        raise ToolGovernanceError("injected tool-call identity is unavailable")
    if any(value != values[0] for value in values[1:]):
        raise ToolGovernanceError("injected tool-call identity is inconsistent")
    value = values[0]
    if normalize_identifier(value) != value:
        raise ToolGovernanceError("injected tool-call identity is unavailable")
    if trusted_tool_call_id is None:
        raise ToolGovernanceError("injected tool-call identity requires a trusted full tool call")
    if value != trusted_tool_call_id:
        raise ToolGovernanceError("injected tool-call identity does not match the outer identity")
    return value


def _validated_base_tool_edit(
    plan: _GovernedPlan,
    action: ToolAction,
    arguments: Mapping[str, Any],
    body: Any,
) -> _EffectiveCall:
    """Validate one edit through LangChain while preserving injected arguments."""
    target = _PinnedToolInput(plan.facts.args_schema)
    edited = dict(arguments)
    public_edit = BaseTool._filter_injected_args(target, edited)  # type: ignore[arg-type]
    if public_edit.keys() != edited.keys():
        raise ToolGovernanceError("approval cannot edit injected tool arguments")
    original = dict(action.arguments)
    original_public = BaseTool._filter_injected_args(target, original)  # type: ignore[arg-type]
    injected = {key: value for key, value in original.items() if key not in original_public}
    args, kwargs = BaseTool._to_args_and_kwargs(  # type: ignore[arg-type]
        target,
        {**edited, **injected},
        action.tool_call_id,
    )
    if plan.facts.args_schema is None:
        return _effective_call(body, args, kwargs)
    return _EffectiveCall(_call_arguments(args, kwargs), args, kwargs)


class GovernedTool(BaseTool):
    """A ``BaseTool`` that decides before directly running a frozen body.

    Preserves ``name``, ``description`` and ``args_schema`` from the tool it
    wraps, and reports the wrapped tool's own input schema rather than one
    inferred from this class's ``_run`` signature -- so a schema-less tool stays
    schema-less through the wrapping instead of acquiring ``*args`` / ``**kwargs``.
    It carries the other fields a *caller* reads off a tool object rather than off
    the body behind it -- see :func:`_carried_fields` for which, and why the
    error-handling ones are not among them.

    **One source reference, sealed.** The ``target`` on the plan is the source a
    fresh static snapshot is captured from before caller code runs; execution
    calls the frozen snapshot body directly, never the target's dispatch. The
    target is held in pydantic's private store and unreachable as a
    field. There is deliberately no second, assignable source handle: identity is
    re-derived from the plan, so another target assigned after wrapping could be
    snapshotted under the plan target's authorization. :meth:`__setattr__` refuses every name in
    :data:`_SEALED_ATTRIBUTES`, so neither the delegate nor the plan nor the
    reported binding can be swapped for another.

    Attributes:
        zeroth_binding: What ``govern_tools`` pinned about the wrapped tool. Read
            by the inventory stage; refused as an assignment target.
    """

    zeroth_binding: Any = None
    _zeroth_plan: Any = PrivateAttr(default=None)

    def __init__(self, *, zeroth_plan: Any = None, **data: Any) -> None:
        """Build the wrapper and seal the plan it will execute through.

        The plan is written straight into pydantic's private store rather than
        through ``setattr``, which is what lets :meth:`__setattr__` refuse
        *every* assignment to the sealed names instead of having to tell the
        first one apart from a later substitution.

        Args:
            zeroth_plan: The pinned binding and the live seams a call is decided
                through, including the source whose frozen body this wrapper
                executes directly.
            data: The ``BaseTool`` fields the wrapper carries.

        Raises:
            ToolGovernanceError: If the plan could not be sealed, which leaves a
                wrapper that refuses every call rather than one that executes
                something unpinned.
        """
        super().__init__(**data)
        private = self.__pydantic_private__
        if private is None:
            raise ToolGovernanceError("a governed tool could not seal its authorized target")
        private[_PLAN_ATTRIBUTE] = zeroth_plan

    def __setattr__(self, name: str, value: Any) -> None:
        """Refuse to rebind anything a call is executed through or reported as.

        Raises:
            ToolGovernanceError: If *name* is one of :data:`_SEALED_ATTRIBUTES`.
        """
        if any(name == sealed for sealed in _SEALED_ATTRIBUTES):
            raise ToolGovernanceError("a governed tool's authorized target cannot be reassigned")
        super().__setattr__(name, value)

    def _plan(self) -> _GovernedPlan:
        """Return the sealed plan, refusing a wrapper that carries none.

        An exact-type gate, as everywhere else in this package: a wrapper built
        without a plan -- or one whose private store somebody reached into --
        refuses its calls rather than executing something no identity was pinned
        against.

        Raises:
            ToolGovernanceError: If no plan was sealed into this wrapper.
        """
        plan = self._zeroth_plan
        if type(plan) is not _GovernedPlan:
            raise ToolGovernanceError("this governed tool carries no authorization plan")
        return plan

    def get_input_schema(self, config: Any = None) -> Any:
        """Report the governed tool's own input schema, not this wrapper's signature."""
        return self._plan().target.get_input_schema(config)

    def _full_tool_call_id(self, tool_input: Any) -> str | None:
        """Validate the complete native call envelope before trusting its id."""
        if not isinstance(tool_input, dict) or dict.get(tool_input, "type") != "tool_call":
            return None
        call_id = dict.get(tool_input, "id")
        if (
            type(tool_input) is not dict
            or dict.get(tool_input, "name") != self.name
            or type(dict.get(tool_input, "args")) is not dict
            or type(call_id) is not str
            or normalize_identifier(call_id) != call_id
        ):
            raise ToolGovernanceError("trusted tool-call identity requires a valid full tool call")
        return call_id

    @functools.wraps(BaseTool.invoke)
    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        """Seed call identity only from a validated native ``ToolCall``."""
        tool_call_id = self._full_tool_call_id(input)
        if tool_call_id is None:
            return super().invoke(input, config, **kwargs)
        token = _BASE_TOOL_CALL.set(_BaseToolCall(self, tool_call_id))
        try:
            return super().invoke(input, config, **kwargs)
        finally:
            _BASE_TOOL_CALL.reset(token)

    @functools.wraps(BaseTool.ainvoke)
    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        """Seed async call identity only from a validated native ``ToolCall``."""
        tool_call_id = self._full_tool_call_id(input)
        if tool_call_id is None:
            return await super().ainvoke(input, config, **kwargs)
        token = _BASE_TOOL_CALL.set(_BaseToolCall(self, tool_call_id))
        try:
            return await super().ainvoke(input, config, **kwargs)
        finally:
            _BASE_TOOL_CALL.reset(token)

    @functools.wraps(BaseTool.run)
    def run(self, *args: Any, tool_call_id: str | None = None, **kwargs: Any) -> Any:
        """Accept an id only from this invocation's validated call envelope."""
        call = _BASE_TOOL_CALL.get()
        if tool_call_id is not None and (
            call is None
            or call.owner is not self
            or call.tool_call_id != tool_call_id
            or call.entered
        ):
            raise ToolGovernanceError("trusted tool-call identity requires a valid full tool call")
        if tool_call_id is not None:
            call.entered = True
        return super().run(*args, tool_call_id=tool_call_id, **kwargs)

    @functools.wraps(BaseTool.arun)
    async def arun(self, *args: Any, tool_call_id: str | None = None, **kwargs: Any) -> Any:
        """Accept an async id only from this invocation's validated call envelope."""
        call = _BASE_TOOL_CALL.get()
        if tool_call_id is not None and (
            call is None
            or call.owner is not self
            or call.tool_call_id != tool_call_id
            or call.entered
        ):
            raise ToolGovernanceError("trusted tool-call identity requires a valid full tool call")
        if tool_call_id is not None:
            call.entered = True
        return await super().arun(*args, tool_call_id=tool_call_id, **kwargs)

    def _to_args_and_kwargs(
        self, tool_input: Any, tool_call_id: str | None
    ) -> tuple[tuple[Any, ...], dict[str, Any]]:
        """Arm the validated identity only after native input parsing succeeds."""
        parsed = super()._to_args_and_kwargs(tool_input, tool_call_id)
        if tool_call_id is None:
            return parsed
        call = _BASE_TOOL_CALL.get()
        if (
            call is None
            or call.owner is not self
            or call.tool_call_id != tool_call_id
            or not call.entered
            or call.parsed
        ):
            raise ToolGovernanceError("trusted tool-call identity requires a valid full tool call")
        call.parsed = True
        return parsed

    def _claim_outer_tool_call_id(self) -> str | None:
        """Claim this invocation's native identity once, never across nested calls."""
        call = _BASE_TOOL_CALL.get()
        if call is None or call.owner is not self or not call.parsed or call.claimed:
            return None
        call.claimed = True
        return call.tool_call_id

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        """Govern this call, then execute its frozen sync body directly."""
        outer_tool_call_id = self._claim_outer_tool_call_id()
        plan = self._plan()
        arguments = _call_arguments(args, kwargs)
        action, context, facts = _governed_action(
            plan,
            arguments,
            tool_call_id=_injected_tool_call_id(plan, arguments, outer_tool_call_id),
        )
        body, _ = _snapshot_body_with_state(facts.snapshot, "func", "_run")
        edited_call: _EffectiveCall | None = None

        def prepare_edited(edited: Mapping[str, Any]) -> Mapping[str, Any]:
            nonlocal edited_call
            edited_call = _validated_base_tool_edit(plan, action, edited, body)
            return edited_call.arguments

        def execute_edited(_arguments: Mapping[str, Any]) -> Any:
            if edited_call is None:
                raise ToolGovernanceError("edited tool arguments were not prepared")
            return execute_snapshot(facts.snapshot, edited_call.args, edited_call.kwargs)

        return guard_tool_call(
            action,
            context,
            lambda: execute_snapshot(facts.snapshot, args, kwargs),
            invoke_with_arguments=execute_edited,
            prepare_edited_arguments=prepare_edited,
            **_enforcement_seams(plan),
        )

    async def _arun(self, *args: Any, **kwargs: Any) -> Any:
        """Govern this call, then execute its frozen async body directly."""
        outer_tool_call_id = self._claim_outer_tool_call_id()
        plan = self._plan()
        arguments = _call_arguments(args, kwargs)
        action, context, facts = _governed_action(
            plan,
            arguments,
            tool_call_id=_injected_tool_call_id(plan, arguments, outer_tool_call_id),
        )
        body, _ = _snapshot_body_with_state(facts.snapshot, "coroutine", "_arun")
        if body is None:
            body, _ = _snapshot_body_with_state(facts.snapshot, "func", "_run")
        edited_call: _EffectiveCall | None = None

        async def execute() -> Any:
            return await aexecute_snapshot(facts.snapshot, args, kwargs)

        def prepare_edited(edited: Mapping[str, Any]) -> Mapping[str, Any]:
            nonlocal edited_call
            edited_call = _validated_base_tool_edit(plan, action, edited, body)
            return edited_call.arguments

        async def execute_edited(_arguments: Mapping[str, Any]) -> Any:
            if edited_call is None:
                raise ToolGovernanceError("edited tool arguments were not prepared")
            return await aexecute_snapshot(facts.snapshot, edited_call.args, edited_call.kwargs)

        return await aguard_tool_call(
            action,
            context,
            execute,
            invoke_with_arguments=execute_edited,
            prepare_edited_arguments=prepare_edited,
            **_enforcement_seams(plan),
        )


def _govern_base_tool(target: BaseTool, facts: _ToolFacts, plan: _GovernedPlan) -> GovernedTool:
    """Build the governed twin of a ``BaseTool``, leaving the original untouched.

    The entry-hook ban is checked here as well as before every execution, so a
    tool governance could never execute faithfully is refused at ``govern_tools``
    rather than at its first call.

    Raises:
        UnstableToolIdentityError: If the tool overrides a pre-body entry point.
        ToolGovernanceError: If the tool's declared surface will not build a
            wrapper -- an ``args_schema`` of a shape ``BaseTool`` rejects, say.
            Refusing is the only outcome that cannot leave an ungoverned tool in
            the returned list.
    """
    refuse_delegate_dispatch(target)
    try:
        return GovernedTool(
            name=plan.binding.identity.name,
            description=facts.description,
            args_schema=facts.args_schema,
            zeroth_plan=plan,
            zeroth_binding=plan.binding,
            **_carried_fields(target),
        )
    except ToolGovernanceError:
        raise
    except Exception as error:
        raise ToolGovernanceError("this tool's declared surface cannot be governed") from error


def _sync_callable_call(token: object, args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> Any:
    """Resolve one sync wrapper's private plan, govern the call, and execute it."""
    plan = _callable_plan(token)
    call = _effective_call(plan.source, args, kwargs)
    action, context, facts = _governed_action(plan, call.arguments)
    body = facts.body
    edited_call: _EffectiveCall | None = None

    def execute() -> Any:
        refuse_state_cell_escalation(facts.state_cells)
        return body(*call.args, **call.kwargs)

    def prepare_edited(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        nonlocal edited_call
        edited_call = _effective_call(plan.source, (), _edited_kwargs(arguments))
        return edited_call.arguments

    def execute_edited(_arguments: Mapping[str, Any]) -> Any:
        if edited_call is None:
            raise ToolGovernanceError("edited callable arguments were not prepared")
        refuse_state_cell_escalation(facts.state_cells)
        return body(*edited_call.args, **edited_call.kwargs)

    return guard_tool_call(
        action,
        context,
        execute,
        invoke_with_arguments=execute_edited,
        prepare_edited_arguments=prepare_edited,
        **_enforcement_seams(plan),
    )


async def _async_callable_call(
    token: object, args: tuple[Any, ...], kwargs: Mapping[str, Any]
) -> Any:
    """Resolve one async wrapper's private plan, govern the call, and execute it."""
    plan = _callable_plan(token)
    call = _effective_call(plan.source, args, kwargs)
    action, context, facts = _governed_action(plan, call.arguments)
    edited_call: _EffectiveCall | None = None

    async def execute() -> Any:
        refuse_state_cell_escalation(facts.state_cells)
        return await facts.body(*call.args, **call.kwargs)

    def prepare_edited(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        nonlocal edited_call
        edited_call = _effective_call(plan.source, (), _edited_kwargs(arguments))
        return edited_call.arguments

    async def execute_edited(_arguments: Mapping[str, Any]) -> Any:
        if edited_call is None:
            raise ToolGovernanceError("edited callable arguments were not prepared")
        refuse_state_cell_escalation(facts.state_cells)
        return await facts.body(*edited_call.args, **edited_call.kwargs)

    return await aguard_tool_call(
        action,
        context,
        execute,
        invoke_with_arguments=execute_edited,
        prepare_edited_arguments=prepare_edited,
        **_enforcement_seams(plan),
    )


def _sync_callable_wrapper(token: object) -> Any:
    """Return a governed function whose sole closure value is an opaque token.

    The body is invoked with the call :func:`_effective_call` resolved, never with
    the caller's own ``args`` / ``kwargs``: re-passing the originals would run the
    body on a call the policy was not shown the whole of, because a parameter
    default is materialized by the binding and by nothing else.
    """

    def governed(*args: Any, **kwargs: Any) -> Any:
        """Govern this call, then invoke the snapshotted body exactly once on an allow."""
        return _sync_callable_call(token, args, kwargs)

    return governed


def _async_callable_wrapper(token: object) -> Any:
    """Return a governed coroutine whose sole closure value is an opaque token.

    Invokes the same resolved call the sync wrapper does, for the same reason.
    The two lines are identical on purpose: an argument-handling fix applied to
    one of them leaves the other surface deciding one call and running another.
    """

    async def governed(*args: Any, **kwargs: Any) -> Any:
        """Govern this call, then await the snapshotted body exactly once on an allow."""
        return await _async_callable_call(token, args, kwargs)

    return governed


def _govern_callable(target: Any, facts: _ToolFacts, seams: _Seams) -> Any:
    """Wrap a plain callable so that calling it decides first, calling it second.

    The wrapper stays directly callable -- that is a bare callable's whole
    interface -- and carries the tool-shaped attributes alongside, so a caller
    that reads ``name`` / ``description`` / ``args_schema`` off a tool list sees
    the same values it saw before.

    **No handle to the target, frozen source or plan is published.** The returned
    function closes over only an opaque non-callable token; a private registry
    resolves that token to the source-free plan. The binding is published because
    the inventory stage reads it, and nothing is executed through it.
    """
    annotations = _attested_annotations(target)
    _attest_signature_defaults(target)
    forbidden_codes: list[types.CodeType] = []
    _collect_executable_codes(target, forbidden_codes, set())
    _collect_executable_codes(facts.body, forbidden_codes, set())
    published_schema = _snapshot_args_schema(
        facts.args_schema, (target, facts.body, *forbidden_codes)
    )
    source = facts.body
    _strip_frozen_callable_attributes(source)
    arguments = tuple(facts.material["arguments"])
    binding = _pin(facts, target, seams)
    plan = _CallablePlan(
        target=target,
        source=source,
        state_cells=facts.state_cells,
        metadata=_CallableMetadata(
            name=binding.identity.name,
            description=facts.description,
            arguments=arguments,
            schema=schema_digest(published_schema),
        ),
        facts=facts,
        observed_identity=binding.identity,
        binding=binding,
        seams=seams,
    )
    observed = _callable_facts(plan)
    plan = replace(
        plan,
        observed_identity=normalize_tool_identity(observed.name, observed.material),
    )
    token = object()
    _CALLABLE_PLANS[token] = plan
    try:
        governed = (
            _async_callable_wrapper(token)
            if inspect.iscoroutinefunction(target)
            else _sync_callable_wrapper(token)
        )
        governed.__name__ = _text(_peek(target, "__name__")) or plan.metadata.name
        governed.__qualname__ = _text(_peek(target, "__qualname__")) or governed.__name__
        governed.__module__ = _text(_peek(target, "__module__")) or governed.__module__
        governed.__doc__ = _text(_peek(target, "__doc__"))
        governed.__annotations__ = dict(annotations)
        # Preserve the existing fail-closed boundary for admitted callables whose
        # executable cannot describe a valid call (an over-bound partial, for
        # example): construction succeeds, and every attempted call is refused by
        # ``_effective_call`` before a policy is consulted.
        with contextlib.suppress(ToolGovernanceError):
            governed.__signature__ = _published_signature(source, annotations)
        governed.name = plan.binding.identity.name
        governed.description = facts.description
        governed.args_schema = published_schema
        governed.zeroth_binding = plan.binding
        weakref.finalize(governed, _drop_callable_plan, token)
    except Exception:
        _drop_callable_plan(token)
        raise
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
    if isinstance(target, GovernedTool):
        raise UnstableToolIdentityError("an already governed tool cannot be governed again")
    is_tool = isinstance(target, BaseTool)
    if not is_tool and not callable(target):
        raise ToolGovernanceError("govern_tools accepts BaseTool instances and plain callables")
    describe = _describe_base_tool if is_tool else _describe_callable
    facts = describe(target)
    if is_tool:
        binding = _pin(facts, target, seams)
        observed = describe(target)
        plan = _GovernedPlan(
            target=target,
            describe=describe,
            facts=facts,
            observed_identity=normalize_tool_identity(observed.name, observed.material),
            binding=binding,
            seams=seams,
        )
        return _govern_base_tool(target, facts, plan)
    return _govern_callable(target, facts, seams)


def govern_tools(
    tools: Iterable[Any],
    *,
    context: object = None,
    client: ToolDecisionClient | None = None,
    unknown_side_effect: UnknownSideEffectPolicy = UnknownSideEffectPolicy.DENY,
    audit: ToolAuditSubmitter | None = None,
    actor: ActorIdentity | None = None,
    interrupt: Callable[[Mapping[str, Any]], Any] | None = None,
    approval_lifecycle: SQLiteApprovalRepository | None = None,
    side_effect: Callable[[Any], Any] | None = None,
    contract_ref: Callable[[Any], Any] | None = None,
    capability_refs: Callable[[Any], Any] | None = None,
    requires_approval: Callable[[Any], Any] | None = None,
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
        approval_lifecycle: Durable approval storage used before an interrupt.
        side_effect: An optional per-tool classifier, reviewed when each wrapper
            is built and rechecked before every action. Only a
            real :class:`~zeroth.integrations.langgraph._tool_types.SideEffectClass`
            member classifies a tool; anything else leaves it unknown, and
            unknown is denied unless *unknown_side_effect* says otherwise.
        contract_ref: An optional per-tool contract resolver, reviewed when each
            wrapper is built and rechecked before every action.
        capability_refs: An optional per-tool required-capability resolver.
        requires_approval: An optional per-tool explicit-approval resolver.

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
        approval_lifecycle=approval_lifecycle,
        side_effect=side_effect,
        contract_ref=contract_ref,
        capability_refs=capability_refs,
        requires_approval=requires_approval,
    )
    return [_govern_one(target, seams) for target in supplied]


__all__ = [
    "GovernedTool",
    "GovernedToolBinding",
    "govern_tools",
]
