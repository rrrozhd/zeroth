"""Proof that the tool inventory is exact (R11) and never over-claims (R13).

**R11 is asserted in all three directions, and the third is the one with teeth.**
A missing tool and an extra tool are what any comparison catches. A tool *swapped
for another under the same name* is what a name-keyed comparison calls a match --
so the substitution case below asserts not only that the match fails, but that
the two tools' fingerprints differ while their names are identical, which is the
only reason it can fail.

**R13 is asserted as an absence.** ``enforced`` needs signed, fresh,
``tool_manifest_complete`` run evidence that nothing in this package mints, so the
tests do not look for a level that is withheld under some condition: they assert
that *no* inventory -- empty, partial, or attested ``COMPLETE`` -- ever reports
``ENFORCED``, and that the module's namespace holds no evidence type to mint one
from. The attested case is the sharp one: it proves inventory completeness does
not leak into the governance level.

``langchain-core`` is a core dependency and is already imported eagerly by
``_wrapper.py``, so real ``BaseTool`` objects are used here. Nothing imports
``langchain.agents`` or needs ``langgraph``, and so nothing carries the
``langgraph_conformance`` marker: ``addopts`` deselects it and a marked test would
never run.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest
from langchain_core.tools import StructuredTool

from tests.integrations.langgraph.tools._hostile import HostileList, HostileStr
from zeroth.core.langgraph_gateway.models import GovernanceLevel
from zeroth.governance.audit.capture_projection import ContentFreeProjection
from zeroth.governance.audit.capture_scrub import RedactionChain
from zeroth.integrations.langgraph._tool_errors import (
    ToolGovernanceError,
    UnstableToolIdentityError,
)
from zeroth.integrations.langgraph._tool_inventory import (
    ToolEnforcementReport,
    attest_complete_inventory,
    inventory_fingerprint,
    match_tool_inventory,
    record_tool_inventory,
    report_tool_enforcement,
)
from zeroth.integrations.langgraph._tool_types import (
    InventoryCoverage,
    ToolIdentity,
    ToolInventory,
    ToolInventoryEntry,
)
from zeroth.integrations.langgraph._tool_wrappers import govern_tools

FINGERPRINT_SHAPE = 64
"""How long a hex SHA-256 digest is, so a fabricated one is recognisably shaped."""


def _body(text: str) -> str:
    """Stand in for whatever a real tool does."""
    return text


def _tool(name: str, description: str = "the declared body") -> StructuredTool:
    """Build a real ``BaseTool`` whose fingerprint follows its description."""
    return StructuredTool.from_function(func=_body, name=name, description=description)


def _governed(*tools: Any) -> list[Any]:
    """Wrap tools the way an installer would, without deciding anything."""
    return govern_tools(tools)


def _identities(tools: list[Any]) -> list[ToolIdentity]:
    """Read back what ``govern_tools`` pinned, which is what an operator declares."""
    return [tool.zeroth_binding.identity for tool in tools]


def _names(identities: Any) -> list[str]:
    """Project a finding onto the names it holds."""
    return [identity.name for identity in identities]


# -- R11: the inventory is exactly the governed tool list ---------------------


def test_the_inventory_records_exactly_what_govern_tools_pinned() -> None:
    tools = _governed(_tool("search"), _tool("write"))
    inventory = record_tool_inventory(tools)

    assert inventory.coverage is InventoryCoverage.PARTIAL
    assert _names([entry.identity for entry in inventory.entries]) == ["search", "write"]
    assert [entry.identity for entry in inventory.entries] == _identities(tools)
    assert match_tool_inventory(inventory, _identities(tools)).matched


def test_a_missing_tool_fails_the_match() -> None:
    tools = _governed(_tool("search"), _tool("write"))
    declared = _identities(tools)
    inventory = record_tool_inventory(tools[:1])

    match = match_tool_inventory(inventory, declared)

    assert not match.matched
    assert _names(match.missing) == ["write"]
    assert match.unexpected == ()
    assert match.substituted == ()


def test_an_extra_tool_fails_the_match() -> None:
    tools = _governed(_tool("search"), _tool("write"))
    declared = _identities(tools)[:1]
    inventory = record_tool_inventory(tools)

    match = match_tool_inventory(inventory, declared)

    assert not match.matched
    assert _names(match.unexpected) == ["write"]
    assert match.missing == ()
    assert match.substituted == ()


def test_a_same_name_different_fingerprint_substitution_fails_the_match() -> None:
    """The direction a name-keyed inventory would wave through."""
    declared = _governed(_tool("search", "the declared body"))
    installed = _governed(_tool("search", "a substituted body"))
    declared_identity = _identities(declared)[0]
    installed_identity = _identities(installed)[0]

    # The two are indistinguishable by name and distinguishable only by fingerprint.
    assert declared_identity.name == installed_identity.name == "search"
    assert declared_identity.fingerprint != installed_identity.fingerprint

    inventory = record_tool_inventory(installed)
    match = match_tool_inventory(inventory, [declared_identity])

    assert not match.matched
    assert _names(match.substituted) == ["search"]
    # Reported as what is installed, not as what was expected.
    assert match.substituted[0].fingerprint == installed_identity.fingerprint
    # Not folded into the other two directions.
    assert match.missing == ()
    assert match.unexpected == ()
    # And the whole-inventory digest moves with it.
    assert inventory_fingerprint(inventory) != inventory_fingerprint(
        record_tool_inventory(declared)
    )


def test_the_inventory_fingerprint_ignores_order_and_nothing_else() -> None:
    search, write = _tool("search"), _tool("write")
    forward = record_tool_inventory(_governed(search, write))
    reverse = record_tool_inventory(_governed(write, search))

    assert len(inventory_fingerprint(forward)) == FINGERPRINT_SHAPE
    assert inventory_fingerprint(forward) == inventory_fingerprint(reverse)
    assert match_tool_inventory(reverse, _identities(_governed(search, write))).matched

    dropped = record_tool_inventory(_governed(search))
    assert inventory_fingerprint(dropped) != inventory_fingerprint(forward)


def test_an_ungoverned_tool_refuses_the_recording() -> None:
    """A tool nobody governed must not be silently absent from the inventory."""
    with pytest.raises(UnstableToolIdentityError):
        record_tool_inventory([_tool("search")])


def test_a_hostile_container_cannot_smuggle_an_unreadable_tool_in() -> None:
    """``HostileList.__iter__`` injects a value the list does not hold; it must raise."""
    governed = _governed(_tool("search"))

    with pytest.raises(UnstableToolIdentityError):
        record_tool_inventory(HostileList(governed))


def test_a_hostile_name_cannot_forge_a_match() -> None:
    real = _identities(_governed(_tool("search")))[0]
    poisoned = ToolInventory(
        entries=[ToolInventoryEntry(identity=ToolIdentity(HostileStr("search"), real.fingerprint))]
    )

    with pytest.raises(UnstableToolIdentityError):
        match_tool_inventory(poisoned, [real])
    with pytest.raises(UnstableToolIdentityError):
        inventory_fingerprint(poisoned)
    with pytest.raises(UnstableToolIdentityError):
        report_tool_enforcement(poisoned)

    # Nor from the declared side.
    inventory = record_tool_inventory(_governed(_tool("search")))
    with pytest.raises(UnstableToolIdentityError):
        match_tool_inventory(
            inventory, [ToolIdentity(HostileStr("search"), HostileStr(real.fingerprint))]
        )


def test_a_foreign_entry_or_container_is_refused() -> None:
    inventory = record_tool_inventory(_governed(_tool("search")))

    with pytest.raises(ToolGovernanceError):
        report_tool_enforcement(object())
    with pytest.raises(UnstableToolIdentityError):
        match_tool_inventory(ToolInventory(entries=[object()]), [])
    with pytest.raises(UnstableToolIdentityError):
        match_tool_inventory(inventory, [object()])


def test_a_repeated_tool_name_is_refused_on_both_sides() -> None:
    """A name two tools answer to identifies neither, and makes the diff ambiguous."""
    with pytest.raises(UnstableToolIdentityError):
        record_tool_inventory(_governed(_tool("search", "one"), _tool("search", "two")))

    inventory = record_tool_inventory(_governed(_tool("search")))
    declared = _identities(_governed(_tool("search")))
    with pytest.raises(UnstableToolIdentityError):
        match_tool_inventory(inventory, [declared[0], declared[0]])


# -- R11: completeness is claimed only against a list that matched exactly ----


def test_completeness_is_minted_only_by_an_exact_match() -> None:
    tools = _governed(_tool("search"), _tool("write"))
    declared = _identities(tools)
    inventory = record_tool_inventory(tools)

    attested = attest_complete_inventory(inventory, declared)
    assert attested.coverage is InventoryCoverage.COMPLETE
    assert [entry.identity for entry in attested.entries] == declared

    with pytest.raises(ToolGovernanceError):
        attest_complete_inventory(inventory, declared[:1])
    with pytest.raises(ToolGovernanceError):
        attest_complete_inventory(
            record_tool_inventory(_governed(_tool("search", "a substituted body"))),
            [declared[0]],
        )


# -- R13: never `enforced`, and nothing here could make it reachable ----------


def test_a_tool_only_run_never_reports_enforced() -> None:
    inventory = record_tool_inventory(_governed(_tool("search"), _tool("write")))

    report = report_tool_enforcement(inventory)

    assert report.level is GovernanceLevel.OBSERVED
    assert report.level is not GovernanceLevel.ENFORCED
    assert report.coverage is InventoryCoverage.PARTIAL
    assert report.enforced_tools == ("search", "write")


def test_a_complete_attested_inventory_still_reports_observed() -> None:
    """Coverage is a claim about the tool list; the level is a claim about evidence."""
    tools = _governed(_tool("search"))
    attested = attest_complete_inventory(record_tool_inventory(tools), _identities(tools))

    report = report_tool_enforcement(attested)

    assert report.coverage is InventoryCoverage.COMPLETE
    assert report.level is GovernanceLevel.OBSERVED
    assert report.level is not GovernanceLevel.ENFORCED


def test_an_empty_inventory_reports_admission() -> None:
    """An empty inventory observed nothing, so it may not claim to have observed."""
    report = report_tool_enforcement(record_tool_inventory([]))

    assert report.level is GovernanceLevel.ADMISSION
    assert report.enforced_tools == ()


def test_no_inventory_shape_reports_enforced() -> None:
    tools = _governed(_tool("search"), _tool("write"))
    partial = record_tool_inventory(tools)
    shapes = [
        record_tool_inventory([]),
        partial,
        attest_complete_inventory(partial, _identities(tools)),
        ToolInventory(entries=partial.entries, coverage=InventoryCoverage.COMPLETE),
        # A coverage value nobody minted falls back to the weaker claim.
        ToolInventory(entries=partial.entries, coverage="complete"),
    ]

    for inventory in shapes:
        assert report_tool_enforcement(inventory).level is not GovernanceLevel.ENFORCED

    assert report_tool_enforcement(shapes[-1]).coverage is InventoryCoverage.PARTIAL


def test_the_module_mints_no_capability_evidence() -> None:
    """R13's negative: no evidence type is even in scope to construct."""
    import zeroth.integrations.langgraph._tool_inventory as module

    namespace = vars(module)
    assert "RunCapabilityEvidence" not in namespace
    assert "CapabilityReporter" not in namespace
    # Checked over the namespace's *values*: ``tool_manifest_complete`` is the
    # evidence claim's wire name, so the way this module could mint one is as a
    # module-level string constant, never as a binding named after it. (Asserting
    # it is not a *key* passed for free and therefore proved nothing.)
    assert not any(value == "tool_manifest_complete" for value in namespace.values())
    for value in namespace.values():
        assert not (not isinstance(value, type) and hasattr(value, "evidence_for_run"))


def test_a_report_carries_no_attestation_surface() -> None:
    report = report_tool_enforcement(record_tool_inventory(_governed(_tool("search"))))

    assert not hasattr(report, "evidence")
    assert not hasattr(report, "signature_valid")
    assert set(ToolEnforcementReport.__dataclass_fields__) == {
        "level",
        "coverage",
        "enforced_tools",
    }
    with pytest.raises(AttributeError):
        report.level = GovernanceLevel.ENFORCED  # type: ignore[misc]


# -- The level survives audit metadata only as a plain string -----------------


def test_only_the_level_term_survives_the_capture_projection() -> None:
    """A ``StrEnum`` member is summarized away by the vocabulary gate; ``.value`` is not."""
    report = report_tool_enforcement(record_tool_inventory(_governed(_tool("search"))))
    projection = ContentFreeProjection(RedactionChain().scrub)

    kept, _ = projection.metadata({"governance_level": report.level_term})
    assert kept["governance_level"] == "observed"

    summarized, _ = projection.metadata({"governance_level": report.level})
    assert type(summarized["governance_level"]) is dict
    assert "sha256" in summarized["governance_level"]


def test_importing_the_inventory_module_pulls_in_no_optional_dependency() -> None:
    """It reads bindings structurally, so it needs neither OpenTelemetry nor langgraph."""
    code = (
        "import sys, zeroth.integrations.langgraph._tool_inventory; "
        "leaked = sorted(k for k in sys.modules "
        "if k.split('.')[0] in {'opentelemetry', 'langgraph'}); "
        "assert not leaked, leaked"
    )
    completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)

    assert completed.returncode == 0, completed.stderr
