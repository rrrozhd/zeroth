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
call is therefore executed through
:func:`~zeroth.integrations.langgraph._tool_execution.executing_tool`, whose
validation stage is a pass-through, and the values the body receives are exactly
the values the decision was made about. See :func:`_delegate_input` for why that
object is still driven through ``invoke``. The plain-callable surface reaches the
same property by a different route: :func:`_effective_call` binds the call against
the callable's own signature *once* and that one binding is both what the policy
is shown and what the body is invoked with, so a value the signature materializes
-- a parameter default -- cannot exist on only one of the two sides.

**Identity is the tool's body, not the label on it.** Name, description and
argument names are metadata a substituted tool reproduces exactly, so identity
also carries a digest of the code the tool will actually run and a digest of its
complete declared schema -- types and constraints, not field names. A tool whose
implementation cannot be fingerprinted stably is refused rather than pinned to
the weaker surface it presents; see
:mod:`~zeroth.integrations.langgraph._tool_fingerprint` for what is derived, what
it deliberately leaves out, and why deriving beats a fingerprint a caller asserts.

**The authorized tool is the executed tool, and nothing can rebind it.** The
wrapper holds exactly one reference to the tool it governs -- the ``target`` on
its sealed, private :class:`_GovernedPlan` -- and executes *that*. A second,
publicly assignable handle to the delegate would be a confused deputy: identity
is re-derived from the plan, so policy would authorize the plan's target's
fingerprint while a delegate somebody assigned afterwards ran instead. The plan
is therefore written once, into pydantic's private store, and
:meth:`GovernedTool.__setattr__` refuses every later assignment to it, to the
binding, and to the names the mutable handles used to carry.

**The body that runs is the body whose identity was authorized.** Identity used
to be derived from the live tool and the body fetched from it again at execution
time, with the caller's classifier, contract resolver and decision client running
in between -- so anything that moved the tool in that window executed under the
fingerprint pinned before it moved, and any read governance made could be
answered by the delegate itself. Both halves are now one fact:
:mod:`~zeroth.integrations.langgraph._tool_execution` takes a per-call snapshot
by static reads before any caller-supplied code runs, identity is digested from
that snapshot, and execution runs a framework-owned adapter built from it rather
than a copy the delegate produced. A delegate that overrides a pre-body entry
point, its copy machinery or its attribute dispatch -- or that shadows one on the
instance -- is refused as well, at wrapping and again per call; that refusal is
defense in depth rather than the guarantee, because a list of banned attributes
is a list the next probe walks around.

**Identity is pinned at wrap time and re-derived at every call.** A tool whose
name, body or declared schema moves between the wrapping and the call cannot
carry a reproducible decision, so the mismatch raises
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

import builtins
import contextlib
import enum
import functools
import inspect
import types
import typing
import weakref
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, get_args, get_origin

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from pydantic import BaseModel, PrivateAttr

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
from zeroth.integrations.langgraph._tool_execution import (
    ToolSnapshot,
    executing_tool,
    refuse_delegate_dispatch,
    snapshot_callable,
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
    authorize_tool_call,
    guard_tool_call,
)
from zeroth.integrations.langgraph._tool_normalize import (
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

    **Only ``identity`` is an authorization fact.** It is pinned here and
    re-derived on every call, and a call whose identity no longer matches is
    refused. ``side_effect`` and ``contract_ref`` are inventory description and
    nothing decides against them: :func:`_governed_action` runs the resolvers
    live on every call, so a tool that becomes side-effecting after it was
    wrapped is decided as what it is now, not as what it was.

    **The wrapping never fills them in.** ``govern_tools`` leaves both at their
    defaults rather than asking the caller's resolvers, because a live resolver
    is *consumed* by being asked and every later call would then be decided under
    the following answer -- see :func:`_pin`. A caller with its own classification
    to report constructs these values itself and hands them to
    :func:`~zeroth.integrations.langgraph._tool_inventory.record_binding_inventory`.

    Attributes:
        identity: The name and fingerprint the tool is decided under.
        side_effect: How this tool is described in an inventory, defaulting to
            unknown -- which the default policy denies.
        contract_ref: The contract this tool is described as bound to, when a
            caller declared one.
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


@dataclass(frozen=True, slots=True)
class _CallableMetadata:
    """Immutable identifying metadata for one frozen callable source."""

    name: str
    description: str
    arguments: tuple[str, ...]
    schema: str | None


@dataclass(frozen=True, slots=True)
class _CallablePlan:
    """A plain callable's source-free execution plan, held behind an opaque token."""

    source: Any
    metadata: _CallableMetadata
    binding: GovernedToolBinding
    seams: _Seams


_CALLABLE_PLANS: dict[object, _CallablePlan] = {}
"""Callable plans keyed by fresh, non-callable tokens closed over by wrappers."""

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

_MAX_STATIC_ATTESTATION_DEPTH = 32

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
    """Derive exact trusted types from pristine framework and standard-library state."""
    trusted = set(_BUILTIN_CARRIER_BASES)
    trusted.update(
        {
            enum.property,
            type(enum._not_given),
            inspect.Parameter,
            inspect.Signature,
            types.BuiltinFunctionType,
            types.CellType,
            types.ClassMethodDescriptorType,
            types.CodeType,
            types.FunctionType,
            types.GetSetDescriptorType,
            types.MappingProxyType,
            types.MemberDescriptorType,
            types.MethodDescriptorType,
            types.MethodType,
            types.MethodWrapperType,
            types.WrapperDescriptorType,
        }
    )
    trusted.update(type(value) for value in vars(inspect).values())
    trusted.update(type(value) for value in vars(typing).values())
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
) -> None:
    """Refuse values whose recursively published graph is executable or opaque."""
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
) -> bool:
    """Traverse owned static dictionaries and exact containers without dispatch."""
    if depth > _MAX_STATIC_ATTESTATION_DEPTH:
        raise ToolGovernanceError("callable argument schema attestation exceeded its depth bound")
    if opaque_is_safe and type(value) not in _TRUSTED_GENERATED_CARRIER_TYPES:
        opaque_is_safe = False
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
) -> bool:
    """Traverse one value already retained in the active recursion path."""

    def descend(item: Any) -> bool:
        return _reaches_forbidden_static_value(
            item,
            forbidden,
            seen,
            depth + 1,
            opaque_is_safe=opaque_is_safe,
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
    if _is_implementation(value):
        raise ToolGovernanceError("callable argument schema carries unknown executable state")
    if isinstance(value, type):
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
    namespace: Mapping[str, Any], forbidden: tuple[Any, ...], seen: dict[int, Any]
) -> None:
    """Attest caller-owned schema entries while exempting exact framework products."""
    for name, value in namespace.items():
        if name in _PYDANTIC_GENERATED_ATTRIBUTES:
            if _reaches_forbidden_static_value(value, forbidden, seen, opaque_is_safe=True):
                raise ToolGovernanceError(
                    "callable argument schemas cannot retain executable sources"
                )
            continue
        if _reaches_forbidden_static_value(name, forbidden, seen) or (
            _reaches_forbidden_static_value(value, forbidden, seen)
        ):
            raise ToolGovernanceError("callable argument schemas cannot retain executable sources")


def _attest_args_schema(schema: Any, forbidden: tuple[Any, ...]) -> None:
    """Attest schemas that will be published by identity on a callable wrapper."""
    if schema is None:
        return
    if type(schema) is dict:
        _attest_public_value(schema)
        return
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        seen: dict[int, Any] = {}
        for namespace in _class_namespaces(schema):
            _attest_schema_namespace(namespace, forbidden, seen)
        return
    raise ToolGovernanceError("callable argument schemas must be recursively attestable")


def _attested_annotations(target: Any) -> dict[str, Any]:
    """Copy only an exact, recursively attestable annotation mapping."""
    annotations = getattr(target, "__annotations__", None)
    if annotations is None:
        return {}
    if type(annotations) is not dict:
        raise ToolGovernanceError("callable annotations must be an exact dictionary")
    copied: dict[str, Any] = {}
    for name, value in annotations.items():
        if type(name) is not str:
            raise ToolGovernanceError("callable annotation names must be exactly str")
        _attest_public_value(value, annotation=True)
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
    for parameter in signature.parameters.values():
        _attest_public_value(parameter.default)


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
    *not* carried by the outer wrapper. The framework-owned executing twin runs
    the frozen body and receives the output-shaping fields captured in its
    snapshot. Carrying the first once meant firing every handler twice; it would
    now fire them exactly **once**, because the executing twin stopped carrying
    the source tool's ``callbacks`` at all --
    :data:`~zeroth.integrations.langgraph._tool_execution._CARRIED_FIELDS` drops
    the field and says why. Once is still the wrong number: these are the
    *delegate's* handlers, and running them around the wrapper runs
    caller-supplied code inside the governance boundary to observe a call the
    audit trail already records. It is not the same hole that field list closes,
    and the difference is worth stating so the two are not read as one argument:
    handlers on *this* object fire before the verdict rather than after it --
    ``on_tool_start`` runs ahead of ``_to_args_and_kwargs`` and the decision is
    made inside ``_run`` -- so what one of them rewrote, the policy would still
    be shown.

    The second would hide a failure the executing twin already handled, and the
    third would make the wrapper re-format output the twin already formatted --
    which, for ``content_and_artifact``, means rejecting the twin's own
    ``ToolMessage`` for not being a two-tuple. The twin is handed the whole tool
    call, artifact and all, and its result travels back untouched.

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
    name = _peek(target, "name")
    if normalize_identifier(name) is None:
        name = _peek(target, "__name__")
    arguments = _schema_argument_names(args_schema) or _signature_argument_names(target)
    body = snapshot_callable(target)
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


def _pin(facts: _ToolFacts) -> GovernedToolBinding:
    """Fix the identity this tool is decided under, and consult nothing to do it.

    The identity is the pin: every call re-derives it and refuses if it moved.

    **No authorization resolver is invoked here.** Recording an inventory used to
    ask the caller's classifier and contract resolver for their reading of the
    tool, and a resolver is allowed to be *live*: one that answers from a queue,
    a feature flag or a counter is consumed by the recording, so every later call
    is decided under the answer *after* the one it should have had. That is not a
    reporting defect -- it shifts the classification a policy denies on. The
    binding therefore carries the unknown classification and no contract, and
    :func:`_governed_action` resolves both, live, on every call. A caller who
    wants a classified inventory builds
    :class:`GovernedToolBinding` values from observations it made itself and
    records those.

    Args:
        facts: The tool's already-gated identifying surface.

    Returns:
        The binding whose identity every call through the wrapper is checked
        against.

    Raises:
        UnstableToolIdentityError: If the tool carries no usable name, or its
            identifying material is not canonically representable.
    """
    return GovernedToolBinding(identity=normalize_tool_identity(facts.name, facts.material))


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
    body = snapshot_callable(plan.source)
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
    )


def _governed_action(
    plan: _GovernedPlan | _CallablePlan, arguments: Mapping[str, Any]
) -> tuple[ToolAction, object, _ToolFacts]:
    """Snapshot the tool, decide about the snapshot, and hand it back to be executed.

    **The snapshot is taken first, and it is what runs.** ``plan.describe`` reads
    the tool's body and surface by value, statically, on the line below -- before
    the contract resolver, the side-effect classifier and the decision client,
    every one of which is *caller-supplied code this function calls*. That
    ordering used to be a hole rather than a detail: a classifier that replaced
    the tool's ``func`` when it was consulted moved the body after its identity
    had been pinned and before execution read it again, and the new body then ran
    under the old fingerprint. Nothing downstream re-reads the tool, so there is
    no longer a second read to disagree with the first.

    **Every authorization fact is resolved now, not at wrap time.** The
    classification and the contract binding are re-read from the live resolvers
    on each call, exactly as
    :meth:`~zeroth.integrations.langgraph._middleware.ZerothMiddleware._governed`
    installs them, for two reasons that point the same way. The first is R8: two
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
        The normalized action, the governance context it was attributed to, and
        the snapshot the action describes -- which is the object execution runs.

    Raises:
        UnstableToolIdentityError: If the tool's identity is not the one it was
            wrapped under, or it dispatches through machinery governance cannot
            read past.
        GovernanceContextError: If the call cannot be attributed.
        ToolGovernanceError: If the arguments are not canonically representable.
    """
    context = _resolve_context(plan.seams.context)
    if type(plan) is _CallablePlan:
        facts = _callable_facts(plan)
        resolver_target = plan.source
    else:
        facts = plan.describe(plan.target)
        resolver_target = plan.target
    action = normalize_tool_action(
        name=facts.name,
        arguments=arguments,
        context=context,
        identity_material=facts.material,
        contract_ref=_resolved(plan.seams.contract_ref, resolver_target),
        side_effect=_resolved(plan.seams.side_effect, resolver_target),
    )
    if action.identity != plan.binding.identity:
        raise UnstableToolIdentityError("the tool's identity changed after it was governed")
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
    }


def _callback_free_config() -> RunnableConfig:
    """Return the config the post-authorization executor is invoked with.

    **An invocation with no config is not an invocation with no callbacks.**
    ``ensure_config`` fills a missing config from ``var_child_runnable_config`` --
    the variable ``BaseTool.run`` republishes its own child config into while the
    body runs -- so the internal executor, invoked bare, inherited every handler
    attached to the outer run and fired ``on_tool_start`` a *second* time, after
    the verdict and before ``_to_args_and_kwargs``. The mapping that hook is handed
    is a shallow filtered copy, so every container one level down is the one the
    body is about to receive: a policy that inspected ``["safe", "evil"]`` had the
    body run on ``["safe", "evil", "evil"]``, the extra entry appended by a handler
    that had already, legitimately, run once before the decision.

    That is the same second read of the *arguments* that dropping ``callbacks``
    from :data:`~zeroth.integrations.langgraph._tool_execution._CARRIED_FIELDS`
    deleted, reached through the run instead of through the delegate. Closing the
    delegate's carrier did nothing to this one, which is why the two are recorded
    as separate facts rather than one.

    **``{"callbacks": []}``, and specifically not ``{"callbacks": None}``.**
    ``ensure_config`` merges the ``ContextVar`` first and then overlays the
    explicit config only for keys whose value ``is not None``, so a ``None`` is
    filtered straight back out and the ambient handlers win -- measurably: the
    ambient handler fires once with ``None`` and not at all with ``[]``. The empty
    list is a *value*, and ``callbacks`` is in ``CONFIG_KEYS``, so it overlays.

    **Only ``callbacks``, and the ``ContextVar`` is deliberately left alone.** The
    other keys ``ensure_config`` inherits -- ``tags``, ``metadata``,
    ``configurable``, ``run_name``, ``recursion_limit``, ``max_concurrency`` --
    carry no handler the framework invokes, and none of them reaches the body:
    ``configurable`` is injected only into a parameter annotated ``RunnableConfig``
    and the execution adapter presents ``(*args, **kwargs)`` with no annotations at
    all. Emptying the variable instead would additionally strip ``configurable``
    from what a body reads back through ``get_config()`` -- a governed tool would
    stop seeing its own ``thread_id`` -- which is the observability this fix is
    careful not to break, one layer further in.

    A fresh mapping per call rather than a module constant: this value is handed to
    framework code, and a shared mutable default is one ``setdefault`` away from
    being a carrier of exactly the kind this function exists to close.

    Returns:
        A config that suppresses inherited callbacks and overrides nothing else.
    """
    return {"callbacks": []}


def _delegate_input(
    args: tuple[Any, ...], kwargs: Mapping[str, Any], tool_call_id: str | None, name: str
) -> Any:
    """Rebuild the input the framework-owned executing twin's ``invoke`` expects.

    When the governed call arrived as a tool call, the twin is handed a tool call
    too, id and all. That is what keeps a ``content_and_artifact`` tool's artifact
    alive through the wrapping: the twin builds the whole ``ToolMessage`` itself
    from the frozen body and captured output settings, and that message travels out of the
    wrapper's own formatting stage untouched. Rebuilding a bare argument dict
    instead would leave the twin with no call id, and a twin with no call id
    returns content and drops its artifact on the floor. The source tool is never
    invoked here.

    Args:
        args: The parsed positional arguments.
        kwargs: The parsed named arguments.
        tool_call_id: The id of the tool call being governed, when there is one.
        name: The tool's name, as it goes into a rebuilt tool call.

    Returns:
        The input to hand the executing twin.

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


class GovernedTool(BaseTool):
    """A ``BaseTool`` that decides before it runs a frozen twin, and mutates nothing.

    Preserves ``name``, ``description`` and ``args_schema`` from the tool it
    wraps, and reports the wrapped tool's own input schema rather than one
    inferred from this class's ``_run`` signature -- so a schema-less tool stays
    schema-less through the wrapping instead of acquiring ``*args`` / ``**kwargs``.
    It carries the other fields a *caller* reads off a tool object rather than off
    the body behind it -- see :func:`_carried_fields` for which, and why the
    error-handling ones are not among them.

    **One source reference, sealed.** The ``target`` on the plan is the source a
    fresh static snapshot is captured from before caller code runs; execution
    uses the framework-owned twin built from that snapshot, never the target's
    dispatch. The target is held in pydantic's private store and unreachable as a
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
                through, including the one tool this wrapper executes.
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

    def _to_args_and_kwargs(
        self, tool_input: Any, tool_call_id: str | None
    ) -> tuple[tuple[Any, ...], dict[str, Any]]:
        """Parse the input as ``BaseTool`` does, carrying the call id on to ``_run``.

        ``BaseTool`` hands the call id to this method and to nothing further, but
        the executing twin needs it to format its own output. Threading it through the
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
        """Govern this call, then invoke its frozen executing twin once on an allow.

        Every ``BaseTool`` entry point -- ``invoke``, ``run`` and the inherited
        ``_arun`` fallback -- funnels through here, so there is no way to reach
        the snapshotted body without passing the guard.

        The executor is invoked with :func:`_callback_free_config` rather than
        bare. Only *this* invocation is silenced: the wrapper's own ``run`` has
        already fired the caller's handlers for the governed tool, before the
        verdict, and goes on doing so.
        """
        plan = self._plan()
        call_id = kwargs.pop(_TOOL_CALL_ID_KEY, None)
        action, context, facts = _governed_action(plan, _call_arguments(args, kwargs))
        payload = _delegate_input(args, kwargs, call_id, self.name)
        runnable = executing_tool(facts.snapshot)
        return guard_tool_call(
            action,
            context,
            lambda: runnable.invoke(payload, config=_callback_free_config()),
            **_enforcement_seams(plan),
        )

    async def _arun(self, *args: Any, **kwargs: Any) -> Any:
        """Govern this call, then await the wrapped tool exactly once on an allow.

        Authorization is the same synchronous core the sync path runs; only the
        downstream invocation is awaited. There is no async enforcement branch to
        drift out of step with the sync one.

        It carries :func:`_callback_free_config` for the same reason ``_run`` does,
        and the two lines say it identically on purpose: ``ainvoke`` inherits the
        ambient run config by exactly the same ``ensure_config`` path, so a fix
        applied to one of them would leave the other surface running handlers after
        the verdict.
        """
        plan = self._plan()
        call_id = kwargs.pop(_TOOL_CALL_ID_KEY, None)
        action, context, facts = _governed_action(plan, _call_arguments(args, kwargs))
        payload = _delegate_input(args, kwargs, call_id, self.name)
        runnable = executing_tool(facts.snapshot)
        authorize_tool_call(action, context, **_enforcement_seams(plan))
        return await runnable.ainvoke(payload, config=_callback_free_config())


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
    return guard_tool_call(
        action, context, lambda: body(*call.args, **call.kwargs), **_enforcement_seams(plan)
    )


async def _async_callable_call(
    token: object, args: tuple[Any, ...], kwargs: Mapping[str, Any]
) -> Any:
    """Resolve one async wrapper's private plan, govern the call, and execute it."""
    plan = _callable_plan(token)
    call = _effective_call(plan.source, args, kwargs)
    action, context, facts = _governed_action(plan, call.arguments)
    authorize_tool_call(action, context, **_enforcement_seams(plan))
    return await facts.body(*call.args, **call.kwargs)


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
    _attest_args_schema(facts.args_schema, (target, facts.body, *forbidden_codes))
    source = facts.body
    _strip_frozen_callable_attributes(source)
    arguments = tuple(facts.material["arguments"])
    binding = _pin(facts)
    plan = _CallablePlan(
        source=source,
        metadata=_CallableMetadata(
            name=binding.identity.name,
            description=facts.description,
            arguments=arguments,
            schema=schema_digest(facts.args_schema),
        ),
        binding=binding,
        seams=seams,
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
        governed.args_schema = facts.args_schema
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
    is_tool = isinstance(target, BaseTool)
    if not is_tool and not callable(target):
        raise ToolGovernanceError("govern_tools accepts BaseTool instances and plain callables")
    describe = _describe_base_tool if is_tool else _describe_callable
    facts = describe(target)
    if is_tool:
        plan = _GovernedPlan(target=target, describe=describe, binding=_pin(facts), seams=seams)
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
