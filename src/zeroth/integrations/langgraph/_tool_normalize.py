"""Build the descriptor a tool-governance decision is made about, before it is made.

Normalization runs to completion *first*: identity, then the canonical argument
projection, then the contract binding, then the principal, then the side-effect
classification. Only then does a
:class:`~zeroth.integrations.langgraph._tool_types.ToolAction` exist, and only a
``ToolAction`` is ever handed to a decision client. Nothing here evaluates a
policy, and nothing here reaches a network.

**The principal is injected, never discovered.** ``_correlation.py`` publishes
exactly one string on a ``ContextVar`` -- the gateway correlation id -- and that
string is base64url-decoded from a token segment *without any signature check*;
its own module docstring says enforced mode must never inherit trust from it.
Tenant, principal, roles and audience are discarded at extraction and never
reconstructed. So this module imports nothing from ``_correlation`` and reads no
ambient state: the caller supplies a
:class:`~zeroth.integrations.langgraph._tool_types.ToolGovernanceContext` or the
call is refused with
:class:`~zeroth.integrations.langgraph._tool_errors.GovernanceContextError`.

**Every gate here is an exact-type gate, and every container is copied before it
is validated.** ``isinstance`` admits subclasses, and a subclass is where the
attack lives: a ``str`` subclass whose ``__str__`` substitutes content, a
``tuple`` subclass whose ``__iter__`` injects values, a ``dict`` subclass that
answers one thing to ``items()`` and another to ``__getitem__``. The repository's
own precedent is
:func:`~zeroth.governance.audit.capture_vocabulary.normalize_reason_code`, which
opens with ``if type(name) is not str: return None``; this module follows that
shape everywhere. Mappings are read by iterating ``items()`` and never by keying
back into them, so a stored key's ``__hash__`` / ``__eq__`` is never given a
chance to run.

**An unrepresentable argument is refused, never elided or replaced.** Dropping a
value, or standing a placeholder in for it, would hand the policy a *different*
call from the one about to run -- a policy that denies ``path="/etc/shadow"``
would see a placeholder and allow. Refusing is the only projection that cannot
turn a hostile value into an allow.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any

from zeroth.integrations.langgraph._tool_errors import (
    GovernanceContextError,
    ToolGovernanceError,
    UnstableToolIdentityError,
)
from zeroth.integrations.langgraph._tool_types import (
    SideEffectClass,
    ToolAction,
    ToolGovernanceContext,
    ToolIdentity,
)

_MAX_NAME_CHARS = 256
"""Longest identifier -- tool name, contract ref, principal -- normalization accepts.

A name is a label, not a payload. The bound is what stops a caller from routing
an argument's worth of content through an identifier field and into the
fingerprint. Anything longer is refused rather than truncated: two distinct
over-long names must not collapse onto one identity.
"""

_MAX_ARGUMENT_DEPTH = 16
"""Deepest nesting the canonical projection will walk before refusing.

Recursion over attacker-shaped input is a denial-of-service vector, the same one
``_correlation.py`` caps by token length. Bounding the walk keeps the work an
untrusted argument mapping can provoke finite.
"""

_MAX_ARGUMENT_NODES = 4096
"""Most values the canonical projection will copy for one call.

Depth alone does not bound the work: a wide, shallow structure is unbounded too.
The node budget is the second half of the same bound.
"""


class _CopyBudget:
    """The finite work one canonical projection is allowed to perform."""

    __slots__ = ("_remaining",)

    def __init__(self, nodes: int = _MAX_ARGUMENT_NODES) -> None:
        self._remaining = nodes

    def spend(self) -> None:
        """Charge one copied value, refusing the projection once the budget is gone."""
        self._remaining -= 1
        if self._remaining < 0:
            raise ToolGovernanceError("tool arguments exceed the canonical projection budget")


def _canonical_scalar(value: Any, _depth: int, _budget: _CopyBudget) -> Any:
    """Return an exact-typed scalar unchanged -- it is already canonical."""
    return value


def _canonical_sequence(value: Any, depth: int, budget: _CopyBudget) -> list[Any]:
    """Copy an exact ``list`` or ``tuple`` into a canonical list of canonical values."""
    return [_canonical_value(item, depth + 1, budget) for item in value]


def _canonical_dict(value: Any, depth: int, budget: _CopyBudget) -> dict[str, Any]:
    """Copy an exact mapping into a canonical ``dict`` of canonical values."""
    canonical: dict[str, Any] = {}
    for key, item in value.items():
        if type(key) is not str:
            raise ToolGovernanceError("tool argument keys must be exactly str")
        canonical[key] = _canonical_value(item, depth + 1, budget)
    return canonical


_CANONICAL_HANDLERS: tuple[tuple[type, Callable[[Any, int, _CopyBudget], Any]], ...] = (
    (str, _canonical_scalar),
    (bool, _canonical_scalar),
    (int, _canonical_scalar),
    (float, _canonical_scalar),
    (type(None), _canonical_scalar),
    (dict, _canonical_dict),
    (MappingProxyType, _canonical_dict),
    (list, _canonical_sequence),
    (tuple, _canonical_sequence),
)
"""The exact types the canonical projection accepts, paired with how to copy each.

Scanned with ``is`` rather than looked up in a dict on purpose. A dict lookup
hashes and compares the key, and the key here is ``type(value)`` -- a metaclass
defining ``__hash__`` / ``__eq__`` could make a hostile class answer as ``dict``.
Identity comparison reaches no user-defined hook at all. A subclass is simply
absent from this table, which *is* the hostile-subtype gate.
"""


def _canonical_value(value: Any, depth: int, budget: _CopyBudget) -> Any:
    """Project one argument value onto its canonical form, or refuse it.

    Args:
        value: The value to project. Only the exact types in
            :data:`_CANONICAL_HANDLERS` are representable.
        depth: How deep in the argument structure this value sits.
        budget: The remaining copy budget for this projection.

    Returns:
        The canonical form of *value*.

    Raises:
        ToolGovernanceError: If *value* is of an unrepresentable type, or the
            structure exceeds the depth or node bound.
    """
    budget.spend()
    if depth > _MAX_ARGUMENT_DEPTH:
        raise ToolGovernanceError("tool arguments are nested past the canonical projection depth")
    for candidate, handler in _CANONICAL_HANDLERS:
        if type(value) is candidate:
            return handler(value, depth, budget)
    raise ToolGovernanceError("tool argument value is not representable as canonical JSON")


def _canonical_plain(source: object) -> dict[str, Any]:
    """Copy an argument mapping into a plain canonical ``dict``, or refuse it."""
    if type(source) is not dict and type(source) is not MappingProxyType:
        raise ToolGovernanceError("tool arguments must be an exact mapping")
    return _canonical_dict(source, 0, _CopyBudget())


def canonical_arguments(source: object) -> Mapping[str, Any]:
    """Project a call's arguments onto an immutable, exactly-typed snapshot.

    The mapping is copied by iterating ``items()``; the copy, not the caller's
    object, is what gets validated and returned. Every key must be exactly
    ``str`` and every value exactly one of the JSON-shaped types, so a hostile
    subclass cannot ride into the descriptor a decision is made against.

    Args:
        source: The raw arguments a tool was invoked with.

    Returns:
        A read-only view over a private canonical copy of *source*.

    Raises:
        ToolGovernanceError: If *source* is not an exact mapping, or holds a key
            or value the canonical projection cannot represent.
    """
    return MappingProxyType(_canonical_plain(source))


def argument_fingerprint(arguments: Mapping[str, Any]) -> str:
    """Return the stable digest of a call's canonical arguments.

    Computed over the canonical projection, not over the caller's object, so the
    digest describes exactly what a policy will see. Serialization sorts keys, so
    two mappings with equal content fingerprint alike whatever order they were
    built in.

    Args:
        arguments: The call arguments, canonical or raw.

    Returns:
        The hex SHA-256 digest of the canonical serialization.

    Raises:
        ToolGovernanceError: If the arguments are not canonically representable.
    """
    canonical = _canonical_plain(arguments)
    serialized = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def normalize_identifier(value: object) -> str | None:
    """Return *value* as a bounded, non-blank identifier, or ``None``.

    The single exact-type gate every name in this module passes through. A
    ``str`` subclass fails it, as does a blank or over-long string: an
    identifier that carries no identity is absent, not empty.

    Args:
        value: The candidate identifier.

    Returns:
        The stripped identifier, or ``None`` when nothing usable survives.
    """
    if type(value) is not str:
        return None
    stripped = value.strip()
    if not stripped or len(stripped) > _MAX_NAME_CHARS:
        return None
    return stripped


def normalize_tool_identity(name: object, material: object = None) -> ToolIdentity:
    """Pin a tool to the identity a policy can be written against.

    The name alone is caller-chosen and reusable, so the identity also carries a
    fingerprint over the tool's identifying *material* -- its declared schema,
    description, whatever the binding stage can see. Both go through the same
    exact-type gates as everything else.

    Args:
        name: The tool's bound name.
        material: The tool's identifying material, as a mapping, or ``None``
            when the binding stage could observe none.

    Returns:
        The tool's identity.

    Raises:
        UnstableToolIdentityError: If the name is unusable or the material is
            not canonically representable.
    """
    tool_name = normalize_identifier(name)
    if tool_name is None:
        raise UnstableToolIdentityError("tool name is not a usable identifier")
    try:
        canonical = canonical_arguments({} if material is None else material)
    except ToolGovernanceError as error:
        raise UnstableToolIdentityError("tool identity material is not representable") from error
    fingerprint = argument_fingerprint({"name": tool_name, "material": dict(canonical)})
    return ToolIdentity(name=tool_name, fingerprint=fingerprint)


def normalize_contract_ref(value: object) -> str | None:
    """Return the contract a tool is bound to, or ``None`` when none is declared.

    Absent is a real answer here -- a tool may legitimately carry no contract --
    so an unusable value normalizes to ``None`` rather than raising. It is the
    policy's job to decide what an unbound tool may do.

    Args:
        value: The declared contract reference.

    Returns:
        The normalized reference, or ``None``.
    """
    return normalize_identifier(value)


def _required_identifier(value: object, field: str) -> str:
    """Return a governance-context field as an identifier, or refuse the context."""
    normalized = normalize_identifier(value)
    if normalized is None:
        raise GovernanceContextError(f"governance context field is unusable: {field}")
    return normalized


def _optional_identifier(value: object) -> str | None:
    """Return an optional governance-context field, treating unusable as absent."""
    return None if value is None else normalize_identifier(value)


def require_governance_context(context: object) -> ToolGovernanceContext:
    """Return a normalized copy of *context*, or refuse to attribute the call.

    Copy-then-validate: the returned context is rebuilt from gated fields, so a
    hostile ``str`` subclass smuggled into a principal or tenant cannot survive
    into audit metadata even though the original object was frozen and looked
    fine. A missing or unusable context raises rather than defaulting, because
    a call whose principal cannot be established is not a call that can be
    decided.

    Args:
        context: The caller-supplied governance context.

    Returns:
        A context whose every field is a plain, non-blank ``str`` or ``None``.

    Raises:
        GovernanceContextError: If *context* is not exactly a
            :class:`~zeroth.integrations.langgraph._tool_types.ToolGovernanceContext`,
            or any required field is unusable.
    """
    if type(context) is not ToolGovernanceContext:
        raise GovernanceContextError("a tool call needs an injected ToolGovernanceContext")
    return ToolGovernanceContext(
        tenant_id=_required_identifier(context.tenant_id, "tenant_id"),
        principal_id=_required_identifier(context.principal_id, "principal_id"),
        run_id=_required_identifier(context.run_id, "run_id"),
        thread_id=_optional_identifier(context.thread_id),
        correlation_id=_optional_identifier(context.correlation_id),
    )


def classify_side_effect(value: object) -> SideEffectClass:
    """Return the side-effect class a tool was classified as, defaulting to unknown.

    Only an actual :class:`~zeroth.integrations.langgraph._tool_types.SideEffectClass`
    member classifies a tool. The enum is a ``StrEnum``, so the bare string
    ``"read_only"`` compares equal to
    :attr:`~zeroth.integrations.langgraph._tool_types.SideEffectClass.READ_ONLY`
    -- and is still rejected here, because a classification nobody made must not
    look like one somebody did.

    Args:
        value: The classifier's verdict, if it produced one.

    Returns:
        The classification, or
        :attr:`~zeroth.integrations.langgraph._tool_types.SideEffectClass.UNKNOWN`.
    """
    if type(value) is not SideEffectClass:
        return SideEffectClass.UNKNOWN
    return value


def normalize_tool_action(
    *,
    name: object,
    arguments: object,
    context: object,
    identity_material: object = None,
    contract_ref: object = None,
    side_effect: object = None,
) -> ToolAction:
    """Build the complete, normalized descriptor of one attempted tool call.

    Runs the five normalization stages in order -- identity, canonical
    arguments, contract binding, principal, side-effect classification -- and
    returns only when all five succeeded. Callers must not construct a
    ``ToolAction`` any other way: a partially normalized descriptor is one a
    policy could be evaluated against.

    Args:
        name: The tool's bound name.
        arguments: The raw arguments the tool was invoked with.
        context: The injected governance context the call is attributed to.
        identity_material: The tool's identifying material, when observable.
        contract_ref: The contract the tool is bound to, when declared.
        side_effect: The classifier's verdict, when it produced one.

    Returns:
        The action a decision may now be made about.

    Raises:
        UnstableToolIdentityError: If the tool cannot be pinned to an identity.
        ToolGovernanceError: If the arguments are not canonically representable.
        GovernanceContextError: If the call cannot be attributed.
    """
    identity = normalize_tool_identity(name, identity_material)
    canonical = canonical_arguments(arguments)
    contract = normalize_contract_ref(contract_ref)
    governance = require_governance_context(context)
    return ToolAction(
        identity=identity,
        arguments=canonical,
        contract_ref=contract,
        principal_id=governance.principal_id,
        side_effect=classify_side_effect(side_effect),
    )


__all__ = [
    "argument_fingerprint",
    "canonical_arguments",
    "classify_side_effect",
    "normalize_contract_ref",
    "normalize_identifier",
    "normalize_tool_action",
    "normalize_tool_identity",
    "require_governance_context",
]
