"""What the governed tool surface actually holds, and what it may honestly claim.

Two questions, one module. *Which tools can this run reach* -- recorded from what
the pinning stage observed, matched against the list an operator declared -- and
*what governance level may be reported on the strength of that*. The second
answer is deliberately capped below
:attr:`~zeroth.contracts.langgraph_gateway.models.GovernanceLevel.ENFORCED`.

**Two recorders, because the two install surfaces hold their bindings
differently, and exactly one reporter.**
:func:`record_tool_inventory` reads the binding
:func:`~zeroth.integrations.langgraph._tool_wrappers.govern_tools` left on each
wrapper; :func:`record_binding_inventory` takes bindings a surface holds itself,
which is the only shape available to
:class:`~zeroth.integrations.langgraph._middleware.ZerothMiddleware` -- it wraps
no tool, so there is no object to hang a binding on. They differ in where a
binding comes from and in nothing else: the same gates, the same repeated-name
refusal, the same ``PARTIAL`` coverage, and the one
:func:`report_tool_enforcement` over the result. A second reporter is what would
let one surface over-claim.

**A match is on name *and* fingerprint, and a mismatch has three shapes.** An
inventory keyed on names alone calls a swapped implementation a match: same
``search`` tool, different body, same policy verdict. So
:func:`match_tool_inventory` reports a *missing* tool, an *unexpected* tool and a
*substituted* one -- same name, different fingerprint -- as three separate
findings, and :attr:`ToolInventoryMatch.matched` is true only when all three are
empty. Ordering never matters: the comparison is over names, and
:func:`inventory_fingerprint` digests the sorted pairs.

**A repeated tool name is refused on both sides.** A name is how a tool call is
dispatched, so two tools sharing one is already broken -- and it makes the diff
ambiguous, because a second entry under a known name is indistinguishable from a
substitution of the first. Refusing is both the fail-closed answer and the one
that keeps the three findings meaning exactly one thing each.

**Nothing here is trusted because it arrived typed.**
:class:`~zeroth.integrations.langgraph._tool_types.ToolInventory` and its entries
are plain frozen dataclasses that any caller can construct with anything, so
every identity is *rebuilt* from fields that passed
:func:`~zeroth.integrations.langgraph._tool_normalize.normalize_identifier` --
on the recorded side and on the declared side alike. That is what stops a
``str`` subclass smuggled into a name from forging a match. The inventory digest
covers the complete canonical entry, not only that matching key: changing policy
metadata or the declared identity configuration invalidates attestation evidence.
For the same reason
the binding is read structurally rather than through an exact-type gate on
:class:`~zeroth.integrations.langgraph._tool_wrappers.GovernedToolBinding`:
exact-typing a dataclass anybody can build buys nothing, while importing it would
pull ``langchain_core`` -- and with it ``langsmith`` and the OpenTelemetry SDK --
into a module that has no other need of them. What actually gates is the identity.

**A tool with no readable binding refuses the recording.** Skipping it would be
the worst outcome available: an ungoverned tool that cannot be read becomes an
ungoverned tool that cannot be *seen*, and the "no extras" direction of the match
stops working precisely where it matters.

**R13: this module never reports ``enforced``, and mints no evidence to make it
possible.** :class:`~zeroth.governance.langgraph_gateway.capabilities.CapabilityReporter`
promotes a run to
:attr:`~zeroth.contracts.langgraph_gateway.models.GovernanceLevel.ENFORCED` only for
signed, fresh, governance-run-matched
:class:`~zeroth.contracts.langgraph_gateway.models.RunCapabilityEvidence` that *also*
carries ``tool_manifest_complete``; absent that it fails closed to
:attr:`~zeroth.contracts.langgraph_gateway.models.GovernanceLevel.ADMISSION`. Nothing
in this package produces such evidence and nothing here starts:
:func:`report_tool_enforcement` has no branch that can return ``ENFORCED``, and
an inventory attested :attr:`~zeroth.integrations.langgraph._tool_types.InventoryCoverage.COMPLETE`
reports exactly what a partial one does. Coverage is a statement about the tool
*list*; the governance level is a statement about *attested evidence*, and the
two are not the same claim.

What the report does say is
:attr:`~zeroth.contracts.langgraph_gateway.models.GovernanceLevel.OBSERVED`, with the
names of the tools actually governed -- and
:attr:`~zeroth.contracts.langgraph_gateway.models.GovernanceLevel.ADMISSION` when
there are none, because "observed" is a claim about something and an empty
inventory observes nothing. This is a description of the tool surface, not a run
attestation: it is not fed to a ``CapabilityReporter``, and a consumer that asks
one about the run still gets ``ADMISSION``.

**Writing the level into audit metadata takes ``.value``.**
``execution_metadata["governance_level"]`` is a vocabulary-gated key and the
capture projection retains a term only when ``type(value) is str``
(``capture_projection.py``). A ``StrEnum`` member's type is the enum class, so it
passes the membership test and is then summarized away at write time -- audited
in appearance, content-free in fact. :attr:`ToolEnforcementReport.level_term` is
the plain string a writer must use.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from zeroth.contracts.langgraph_gateway.models import GovernanceLevel
from zeroth.integrations.langgraph._tool_errors import (
    ToolGovernanceError,
    UnstableToolIdentityError,
)
from zeroth.integrations.langgraph._tool_normalize import (
    argument_fingerprint,
    classify_side_effect,
    normalize_capability_refs,
    normalize_contract_ref,
    normalize_identifier,
    normalize_identity_configuration,
)
from zeroth.integrations.langgraph._tool_types import (
    InventoryCoverage,
    ToolIdentity,
    ToolInventory,
    ToolInventoryEntry,
)

_BINDING_ATTRIBUTE = "zeroth_binding"
"""Where ``govern_tools`` leaves what it pinned about a tool.

Read rather than re-derived: the wrapping stage already normalized the identity,
classified the side effect and resolved the contract, and re-deriving them here
would give a second implementation of the same three answers to drift from.
"""


def _peek(source: object, attribute: str) -> Any:
    """Read one attribute off a foreign object, treating any failure as absent.

    A local copy of the wrappers' helper rather than an import of it, because
    importing that module would pull ``langchain_core`` into a module that
    otherwise needs nothing from it.

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


def _gated_identity(value: object) -> ToolIdentity:
    """Rebuild a tool identity from exactly-typed, non-blank fields, or refuse it.

    Copy-then-validate: the returned identity is built from strings that passed
    :func:`~zeroth.integrations.langgraph._tool_normalize.normalize_identifier`,
    so a ``str`` subclass that arrived inside a caller-constructed
    :class:`~zeroth.integrations.langgraph._tool_types.ToolIdentity` never reaches
    a comparison or a fingerprint.

    Args:
        value: The candidate identity.

    Returns:
        An identity whose name and fingerprint are plain, bounded ``str``.

    Raises:
        UnstableToolIdentityError: If *value* is not exactly a ``ToolIdentity``,
            or either field is unusable.
    """
    if type(value) is not ToolIdentity:
        raise UnstableToolIdentityError("a tool inventory entry needs a ToolIdentity")
    name = normalize_identifier(value.name)
    fingerprint = normalize_identifier(value.fingerprint)
    if name is None or fingerprint is None:
        raise UnstableToolIdentityError("a tool identity needs a usable name and fingerprint")
    return ToolIdentity(name=name, fingerprint=fingerprint)


def _refuse_repeated_names(identities: Sequence[ToolIdentity], subject: str) -> None:
    """Refuse a tool list that names the same tool twice.

    Args:
        identities: The already-gated identities.
        subject: What is being checked, for the failure message.

    Raises:
        UnstableToolIdentityError: If a name occurs more than once. A name shared
            by two tools identifies neither.
    """
    seen: set[str] = set()
    for identity in identities:
        if identity.name in seen:
            raise UnstableToolIdentityError(f"{subject} names the same tool twice")
        seen.add(identity.name)


def _gated_entry(entry: object) -> ToolInventoryEntry:
    """Rebuild one inventory entry from gated fields, or refuse it."""
    if type(entry) is not ToolInventoryEntry:
        raise UnstableToolIdentityError("a tool inventory holds ToolInventoryEntry values")
    return _entry_from_binding(entry)


def _gated_entries(inventory: object) -> tuple[ToolInventoryEntry, ...]:
    """Re-read an inventory's entries through the gates that should have built them.

    Args:
        inventory: The candidate inventory.

    Returns:
        The entries, rebuilt from exactly-typed fields.

    Raises:
        ToolGovernanceError: If *inventory* is not exactly a ``ToolInventory``.
        UnstableToolIdentityError: If any entry or identity is unusable, or a
            tool name repeats.
    """
    if type(inventory) is not ToolInventory:
        raise ToolGovernanceError("a governed tool inventory is required here")
    entries = tuple(_gated_entry(entry) for entry in inventory.entries)
    _refuse_repeated_names([entry.identity for entry in entries], "the inventory")
    return entries


def _gated_coverage(value: object) -> InventoryCoverage:
    """Return a coverage claim only when it is a real member, else the weaker one."""
    if type(value) is not InventoryCoverage:
        return InventoryCoverage.PARTIAL
    return value


def _declared_identities(expected: object) -> tuple[ToolIdentity, ...]:
    """Gate the tool list an operator declared, the way the recorded one is gated.

    Args:
        expected: The declared identities.

    Returns:
        The identities, rebuilt from exactly-typed fields.

    Raises:
        ToolGovernanceError: If *expected* is not iterable.
        UnstableToolIdentityError: If an identity is unusable or a name repeats.
    """
    try:
        supplied = list(expected)
    except TypeError as error:
        raise ToolGovernanceError("an expected tool list must be iterable") from error
    identities = tuple(_gated_identity(item) for item in supplied)
    _refuse_repeated_names(identities, "the expected tool list")
    return identities


def _entry_from_binding(binding: object) -> ToolInventoryEntry:
    """Record one tool from what the pinning stage observed about it.

    Read structurally and re-gated field by field, for the reason the module
    docstring gives: the binding is a plain dataclass any caller can build, so
    what makes an entry trustworthy is the gates, not the type it arrived as.

    Args:
        binding: What
            :class:`~zeroth.integrations.langgraph._tool_wrappers.GovernedToolBinding`
            pinned about one tool.

    Returns:
        The tool, as the inventory saw it.

    Raises:
        UnstableToolIdentityError: If the binding carries no usable identity.
    """
    return ToolInventoryEntry(
        identity=_gated_identity(_peek(binding, "identity")),
        side_effect=classify_side_effect(_peek(binding, "side_effect")),
        contract_ref=normalize_contract_ref(_peek(binding, "contract_ref")),
        capability_refs=normalize_capability_refs(_peek(binding, "capability_refs")),
        requires_approval=_peek(binding, "requires_approval") is True,
        identity_configuration=normalize_identity_configuration(
            _peek(binding, "identity_configuration")
        ),
    )


def _entry_for(tool: object) -> ToolInventoryEntry:
    """Record one governed tool from the binding ``govern_tools`` left on it.

    Args:
        tool: A governed wrapper.

    Returns:
        The tool, as the inventory saw it.

    Raises:
        UnstableToolIdentityError: If the tool carries no readable binding -- it
            was never governed, and an ungoverned tool must not be silently
            omitted from an inventory another stage will call complete.
    """
    binding = _peek(tool, _BINDING_ATTRIBUTE)
    if binding is None:
        raise UnstableToolIdentityError("this tool carries no governed binding to record")
    return _entry_from_binding(binding)


def _partial_inventory(entries: tuple[ToolInventoryEntry, ...], subject: str) -> ToolInventory:
    """Return the entries as a ``PARTIAL`` inventory, refusing a repeated tool name.

    The one place either recorder mints an inventory, so the name refusal and the
    coverage claim cannot come out differently depending on which surface
    recorded it.
    """
    _refuse_repeated_names([entry.identity for entry in entries], subject)
    return ToolInventory(entries=entries, coverage=InventoryCoverage.PARTIAL)


@dataclass(frozen=True, slots=True)
class ToolInventoryMatch:
    """How a recorded inventory differs from the tool list an operator declared.

    The three fields are the three ways a tool surface can be wrong, kept apart
    on purpose: a substitution is not a missing tool plus an extra one, it is a
    *different implementation answering to a known name*, and collapsing it into
    the other two is how a name-keyed comparison loses it.

    Attributes:
        missing: Declared tools the inventory does not hold.
        unexpected: Recorded tools nobody declared.
        substituted: Recorded tools whose name was declared but whose fingerprint
            was not. Reported as the inventory recorded them -- what is actually
            installed, not what was expected.
    """

    missing: Sequence[ToolIdentity]
    unexpected: Sequence[ToolIdentity]
    substituted: Sequence[ToolIdentity]

    def __post_init__(self) -> None:
        """Snapshot each finding so the match cannot change after it is reported."""
        object.__setattr__(self, "missing", tuple(self.missing))
        object.__setattr__(self, "unexpected", tuple(self.unexpected))
        object.__setattr__(self, "substituted", tuple(self.substituted))

    @property
    def matched(self) -> bool:
        """Whether the inventory is exactly the declared list: no extras, no omissions."""
        return not (self.missing or self.unexpected or self.substituted)


@dataclass(frozen=True, slots=True)
class ToolEnforcementReport:
    """What the governed tool surface enforces, and the level that honestly supports.

    Attributes:
        level: The governance level this surface supports. Never
            :attr:`~zeroth.contracts.langgraph_gateway.models.GovernanceLevel.ENFORCED`
            -- see the module docstring for the evidence that would be required
            and is never minted here.
        coverage: Whether the inventory expects it saw every reachable tool.
        enforced_tools: The names actually governed, in the recorded order.
    """

    level: GovernanceLevel
    coverage: InventoryCoverage
    enforced_tools: Sequence[str]

    def __post_init__(self) -> None:
        """Snapshot the tool names so the report cannot grow after it is made."""
        object.__setattr__(self, "enforced_tools", tuple(self.enforced_tools))

    @property
    def level_term(self) -> str:
        """The level as the plain ``str`` audit metadata has to carry.

        ``execution_metadata["governance_level"]`` is vocabulary-gated with
        ``type(value) is str``, and a ``StrEnum`` member's type is the enum
        class. Writing :attr:`level` itself would pass the membership test and
        then be replaced by a digest summary at capture time; this is the
        difference between a retained term and a content-free hole.
        """
        return self.level.value


def record_tool_inventory(tools: Iterable[object]) -> ToolInventory:
    """Record every governed tool in *tools*, exactly as it was pinned.

    Reports :attr:`~zeroth.integrations.langgraph._tool_types.InventoryCoverage.PARTIAL`
    always. Completeness is a claim about tools nobody passed in, and this
    function has seen only the ones that were: :func:`attest_complete_inventory`
    is the one place the stronger claim is made, and it makes it only against a
    declared list that matched exactly.

    Args:
        tools: The governed wrappers to record. The iterable is consumed once,
            into a private list.

    Returns:
        The inventory, in the order the tools were supplied.

    Raises:
        ToolGovernanceError: If *tools* is not iterable.
        UnstableToolIdentityError: If a tool carries no governed binding, its
            identity is unusable, or two tools share a name.
    """
    try:
        supplied = list(tools)
    except TypeError as error:
        raise ToolGovernanceError("recording an inventory needs an iterable of tools") from error
    return _partial_inventory(
        tuple(_entry_for(tool) for tool in supplied), "the governed tool list"
    )


def record_binding_inventory(bindings: Iterable[object]) -> ToolInventory:
    """Record an inventory from bindings held directly, rather than read off wrappers.

    The recorder for a surface that governs tools it does not wrap.
    :class:`~zeroth.integrations.langgraph._middleware.ZerothMiddleware` never
    replaces a tool object -- the agent holds the originals and hands each one in
    per call -- so there is no wrapper to carry a ``zeroth_binding``, and the
    bindings it pinned are passed here instead. Everything downstream is the same
    code: the same field gates, the same repeated-name refusal, the same
    ``PARTIAL`` coverage, and the same
    :func:`report_tool_enforcement` over the result.

    Reports :attr:`~zeroth.integrations.langgraph._tool_types.InventoryCoverage.PARTIAL`
    for the same reason :func:`record_tool_inventory` does, and one more: these
    bindings are what an operator *declared*, and a declaration is not a
    discovery of what the agent actually holds.

    Args:
        bindings: What the pinning stage observed about each tool. The iterable
            is consumed once, into a private list.

    Returns:
        The inventory, in the order the bindings were supplied.

    Raises:
        ToolGovernanceError: If *bindings* is not iterable.
        UnstableToolIdentityError: If a binding carries no usable identity, or
            two of them share a tool name.
    """
    try:
        supplied = list(bindings)
    except TypeError as error:
        raise ToolGovernanceError("recording an inventory needs an iterable of bindings") from error
    return _partial_inventory(
        tuple(_entry_from_binding(binding) for binding in supplied), "the declared tool list"
    )


def inventory_fingerprint(inventory: object) -> str:
    """Return the order-insensitive digest of the complete governed inventory.

    Computed over sorted whole-entry objects through
    :func:`~zeroth.integrations.langgraph._tool_normalize.argument_fingerprint`,
    so it inherits that projection's exact-type refusals rather than hashing
    whatever it was handed. Two inventories holding the same entries in different
    orders digest alike; changing identity, policy metadata, or declared identity
    configuration changes the digest.

    Args:
        inventory: The recorded inventory.

    Returns:
        The hex SHA-256 digest of the canonical tool set.

    Raises:
        ToolGovernanceError: If *inventory* is not a ``ToolInventory``.
        UnstableToolIdentityError: If an entry or identity is unusable.
    """
    entries = _gated_entries(inventory)
    payload = sorted((entry.wire_fields() for entry in entries), key=lambda item: item["name"])
    return argument_fingerprint({"tools": payload})


def match_tool_inventory(inventory: object, expected: Iterable[object]) -> ToolInventoryMatch:
    """Compare a recorded inventory against the tool list an operator declared.

    Order-insensitive, and keyed on the fingerprint as well as the name: a tool
    swapped for another under the same name is reported as a substitution rather
    than passing as a match.

    Args:
        inventory: The recorded inventory.
        expected: The declared
            :class:`~zeroth.integrations.langgraph._tool_types.ToolIdentity`
            values.

    Returns:
        The three-way difference between them.

    Raises:
        ToolGovernanceError: If either side is not the shape it must be.
        UnstableToolIdentityError: If an identity is unusable, or either side
            names the same tool twice.
    """
    recorded = [entry.identity for entry in _gated_entries(inventory)]
    declared = _declared_identities(expected)
    recorded_names = {identity.name for identity in recorded}
    declared_by_name = {identity.name: identity.fingerprint for identity in declared}
    missing = [identity for identity in declared if identity.name not in recorded_names]
    unexpected = [identity for identity in recorded if identity.name not in declared_by_name]
    substituted = [
        identity
        for identity in recorded
        if identity.name in declared_by_name
        and declared_by_name[identity.name] != identity.fingerprint
    ]
    return ToolInventoryMatch(missing=missing, unexpected=unexpected, substituted=substituted)


def attest_complete_inventory(inventory: object, expected: Iterable[object]) -> ToolInventory:
    """Return the inventory as complete, or refuse to call it that.

    The only place
    :attr:`~zeroth.integrations.langgraph._tool_types.InventoryCoverage.COMPLETE`
    is ever minted, and it is minted only against a declared tool list that
    matched exactly -- every declared tool present, no extra tool, no
    fingerprint substituted.

    Coverage is not a governance level: an attested inventory reports exactly
    what a partial one reports through :func:`report_tool_enforcement`, because
    promotion needs signed run evidence this package does not produce.

    Args:
        inventory: The recorded inventory.
        expected: The declared identities the inventory must equal.

    Returns:
        A ``COMPLETE``-coverage inventory over the same, re-gated entries.

    Raises:
        ToolGovernanceError: If the inventory does not match the declared list.
        UnstableToolIdentityError: If an identity is unusable or a name repeats.
    """
    match = match_tool_inventory(inventory, expected)
    if not match.matched:
        raise ToolGovernanceError("the governed tool inventory is not the declared tool list")
    return ToolInventory(entries=_gated_entries(inventory), coverage=InventoryCoverage.COMPLETE)


def report_tool_enforcement(inventory: object) -> ToolEnforcementReport:
    """Report the level a governed tool surface supports, and on which tools.

    The level is
    :attr:`~zeroth.contracts.langgraph_gateway.models.GovernanceLevel.OBSERVED` when
    the inventory holds at least one governed tool and
    :attr:`~zeroth.contracts.langgraph_gateway.models.GovernanceLevel.ADMISSION` when
    it holds none. There is no third branch:
    :attr:`~zeroth.contracts.langgraph_gateway.models.GovernanceLevel.ENFORCED` needs
    signed, fresh, ``tool_manifest_complete`` run evidence, nothing in this
    package mints any, and an inventory's coverage is not a substitute for it.

    Args:
        inventory: The recorded inventory.

    Returns:
        What this surface enforces, and the level that supports.

    Raises:
        ToolGovernanceError: If *inventory* is not a ``ToolInventory``.
        UnstableToolIdentityError: If an entry or identity is unusable.
    """
    entries = _gated_entries(inventory)
    names = tuple(entry.identity.name for entry in entries)
    level = GovernanceLevel.OBSERVED if names else GovernanceLevel.ADMISSION
    return ToolEnforcementReport(
        level=level,
        coverage=_gated_coverage(inventory.coverage),
        enforced_tools=names,
    )


__all__ = [
    "ToolEnforcementReport",
    "ToolInventoryMatch",
    "attest_complete_inventory",
    "inventory_fingerprint",
    "match_tool_inventory",
    "record_binding_inventory",
    "record_tool_inventory",
    "report_tool_enforcement",
]
