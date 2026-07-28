"""What the tool-governance vocabulary promises the later enforcement stages.

Two promises are load-bearing and the rest is naming. First, the tri-state
verdict is *local*: :class:`ToolDecisionKind` carries ``REQUIRE_APPROVAL`` while
:class:`~zeroth.governance.policy.models.PolicyDecision` stays ALLOW/DENY, so
adding human-approval to tool calls cannot turn every ``is PolicyDecision.ALLOW``
branch elsewhere in the runtime into a hard failure. Second, the containers are
snapshotted rather than referenced, so a caller that keeps its dict cannot
change the action a decision was already made about.

No ``langgraph`` or ``langchain`` import here, hence no
``langgraph_conformance`` marker: ``addopts`` deselects that marker, so a marked
test would silently never run.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator, Mapping

import pytest

from zeroth.governance.policy.models import PolicyDecision
from zeroth.integrations.langgraph._tool_types import (
    InventoryCoverage,
    SideEffectClass,
    ToolAction,
    ToolDecision,
    ToolDecisionKind,
    ToolGovernanceContext,
    ToolIdentity,
    ToolInventory,
    ToolInventoryEntry,
)

VALUE_SENTINEL = "MUTATED_AFTER_THE_DECISION"
"""Written into a caller-held container after construction, to prove isolation."""

IDENTITY = ToolIdentity(name="search", fingerprint="a" * 64)

DECLARED_TYPES = (
    ToolAction,
    ToolDecision,
    ToolGovernanceContext,
    ToolIdentity,
    ToolInventory,
    ToolInventoryEntry,
)


def test_the_enums_spell_their_members_as_lower_case_label_terms() -> None:
    # These values reach audit metadata, where only label-shaped terms survive
    # the capture projection.
    values = [
        *SideEffectClass,
        *ToolDecisionKind,
        *InventoryCoverage,
    ]

    for member in values:
        assert member.value == member.value.strip().lower()
        assert member.value != ""
        assert len(member.value) <= 64


def test_the_tool_verdict_is_tri_state_while_the_platform_decision_stays_binary() -> None:
    # TRAP 2, pinned from this side: the third verdict lives here, not there.
    assert set(ToolDecisionKind) == {
        ToolDecisionKind.ALLOW,
        ToolDecisionKind.DENY,
        ToolDecisionKind.REQUIRE_APPROVAL,
    }
    assert {member.value for member in PolicyDecision} == {"allow", "deny"}


def test_an_unclassified_tool_is_not_reported_as_read_only() -> None:
    # UNKNOWN is a verdict of its own; defaulting to READ_ONLY would wave through
    # everything nobody classified.
    assert ToolAction(identity=IDENTITY, arguments={}).side_effect is SideEffectClass.UNKNOWN
    assert ToolInventoryEntry(identity=IDENTITY).side_effect is SideEffectClass.UNKNOWN


def test_an_inventory_defaults_to_partial_coverage() -> None:
    # "Nothing was visible" must not read as "nothing is there".
    assert ToolInventory(entries=()).coverage is InventoryCoverage.PARTIAL


@pytest.mark.parametrize("declared", DECLARED_TYPES, ids=lambda cls: cls.__name__)
def test_every_declared_type_is_a_frozen_slotted_dataclass(declared: type) -> None:
    # frozen+slots is the repo's style for these descriptors, and frozen is what
    # makes the snapshots below meaningful.
    assert dataclasses.is_dataclass(declared)
    assert declared.__dataclass_params__.frozen is True
    assert "__slots__" in vars(declared)


def test_a_decision_cannot_be_edited_after_it_is_returned() -> None:
    decision = ToolDecision(kind=ToolDecisionKind.DENY, reason_code="policy_violation")

    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.kind = ToolDecisionKind.ALLOW  # type: ignore[misc]


def test_the_arguments_a_decision_was_made_against_cannot_change_afterwards() -> None:
    # The container hazard: the caller keeps the dict it passed in.
    submitted = {"query": "weather"}
    action = ToolAction(identity=IDENTITY, arguments=submitted)

    submitted["query"] = VALUE_SENTINEL
    submitted["injected"] = VALUE_SENTINEL

    assert dict(action.arguments) == {"query": "weather"}


def test_the_argument_snapshot_is_read_only_from_the_holder_side_too() -> None:
    action = ToolAction(identity=IDENTITY, arguments={"query": "weather"})

    with pytest.raises(TypeError):
        action.arguments["query"] = VALUE_SENTINEL  # type: ignore[index]


def test_the_arguments_are_taken_from_items_not_from_a_second_lookup() -> None:
    # A hostile mapping can answer one thing to iteration and another to
    # __getitem__; snapshotting via items() means only the first is ever read.
    action = ToolAction(identity=IDENTITY, arguments=_TwoFacedMapping())

    assert dict(action.arguments) == {"query": "weather"}


def test_an_inventory_cannot_grow_after_it_is_reported() -> None:
    seen = [ToolInventoryEntry(identity=IDENTITY)]
    inventory = ToolInventory(entries=seen, coverage=InventoryCoverage.COMPLETE)

    seen.append(ToolInventoryEntry(identity=ToolIdentity(name="delete", fingerprint="b" * 64)))

    assert isinstance(inventory.entries, tuple)
    assert len(inventory.entries) == 1


def test_a_threadless_governance_context_is_representable_rather_than_invalid() -> None:
    # The approval stage needs to *detect* the missing thread and raise its own
    # typed failure; the context type refusing to exist would hide that.
    context = ToolGovernanceContext(tenant_id="tenant-a", principal_id="user-1", run_id="run-1")

    assert context.thread_id is None
    assert context.correlation_id is None


class _TwoFacedMapping(Mapping[str, object]):
    """A mapping whose ``__getitem__`` disagrees with the pairs its ``items()`` yields.

    ``dict(m)`` and ``{k: m[k] for k in m}`` both key back in and pick up the
    sentinel; only reading ``items()`` once gets the real pairs. That is the
    whole difference between the two ways of copying a mapping.
    """

    _TRUE_PAIRS = (("query", "weather"),)

    def __iter__(self) -> Iterator[str]:
        return iter([key for key, _ in self._TRUE_PAIRS])

    def __len__(self) -> int:
        return len(self._TRUE_PAIRS)

    def __getitem__(self, key: str) -> object:
        del key
        return VALUE_SENTINEL

    def items(self) -> tuple[tuple[str, object], ...]:  # type: ignore[override]
        """Yield the pairs this mapping really holds."""
        return self._TRUE_PAIRS
