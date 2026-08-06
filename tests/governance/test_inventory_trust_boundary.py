"""Trust-boundary acceptance tests for the registered tool inventory.

Audit round 1's finding, in one sentence: the client supplied *both* sides of
the comparison the enforcement ceiling rests on. A registration carried the
caller's own ``coverage``, ``tool_count`` and ``inventory_fingerprint``, and
``tools`` held bare names; the verifying provider then checked the registered
fingerprint against the attested one -- two strings the same client chose. The
reproduction was ``{coverage: "complete", tools: (), tool_count: 999,
fingerprint: "X"}`` plus an attestation of ``"X"``, which classified
``ENFORCED`` with no governed tools whatsoever.

* **R8** -- a registration records *server-recomputed* fingerprints and count,
  derived from structured identities the caller had to enumerate.
* **R12** -- and the provider's comparison stays a real one. Deriving the
  attested fingerprint from the registration too would compare a value with
  itself; the tests below pin that some input still fails to complete a
  manifest.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from tests.governance.test_capability_evidence_provider import (
    REGISTERED_TOOLS,
    make_payload,
    make_provider,
    make_signer,
)
from zeroth.contracts.langgraph_gateway.models import GovernanceLevel
from zeroth.governance.attestations.inventory import (
    RegisteredTool,
    recompute_inventory_fingerprint,
)
from zeroth.governance.attestations.signing import sign_attestation
from zeroth.governance.attestations.store import (
    InventoryRegistration,
    InventoryRegistrationRepository,
    RunAttestationRepository,
)
from zeroth.governance.enforcement_wire import InventorySubmission
from zeroth.integrations.langgraph._tool_inventory import (
    inventory_fingerprint as sdk_inventory_fingerprint,
)
from zeroth.integrations.langgraph._tool_types import (
    InventoryCoverage,
    ToolIdentity,
    ToolInventory,
    ToolInventoryEntry,
)

TOOLS = (
    RegisteredTool(name="search", fingerprint="fp-search"),
    RegisteredTool(name="send_email", fingerprint="fp-send-email"),
)


def make_registration(**overrides: Any) -> InventoryRegistration:
    """Build a complete-coverage registration over :data:`TOOLS`."""
    fields: dict[str, Any] = {
        "tenant_id": "tenant-alpha",
        "deployment_ref": "dep-alpha",
        "graph_version": "graph-v1",
        "adapter_version": "0.1.0",
        "coverage": "complete",
        "tools": TOOLS,
    }
    fields.update(overrides)
    return InventoryRegistration(**fields)


# --------------------------------------------------------------------------
# R8 -- the server computes the inventory facts, the client does not
# --------------------------------------------------------------------------


async def test_registration_records_server_recomputed_tool_fingerprints(
    sqlite_db: Any,
) -> None:
    """R8: declared digest and count are discarded for the computed ones.

    The audited registration is *constructed* here, with the exact values the
    auditor used. It is not rejected -- it is corrected: the model derives both
    fields from ``tools``, so ``999`` and ``"X"`` cannot survive into storage
    and the attack has nowhere to put its claim.
    """
    registration = make_registration(inventory_fingerprint="X", tool_count=999)

    assert registration.tool_count == len(TOOLS)
    assert registration.inventory_fingerprint == recompute_inventory_fingerprint(TOOLS)
    assert registration.inventory_fingerprint != "X"

    repository = InventoryRegistrationRepository(sqlite_db)
    await repository.register(registration)
    stored = await repository.latest_for_deployment("tenant-alpha", "dep-alpha")

    assert stored is not None
    assert stored.tools == TOOLS
    assert stored.tool_count == len(TOOLS)
    assert stored.inventory_fingerprint == recompute_inventory_fingerprint(TOOLS)


async def test_an_empty_inventory_cannot_certify_an_attested_fingerprint(
    sqlite_db: Any,
) -> None:
    """R8: the audit's end-to-end reproduction no longer reaches ENFORCED.

    Zero real tools, coverage self-declared ``complete``, and a run attesting
    the arbitrary fingerprint ``"X"``. The registration's digest is now
    computed from its (empty) identities, so it cannot equal ``"X"`` and the
    manifest never completes.
    """
    await InventoryRegistrationRepository(sqlite_db).register(
        make_registration(tools=(), inventory_fingerprint="X", tool_count=999)
    )
    await RunAttestationRepository(sqlite_db).record(
        sign_attestation(make_payload(inventory_fingerprint="X"), make_signer())
    )

    evidence = await make_provider(sqlite_db).evidence_for_run("corr-alpha")

    assert evidence is not None
    assert evidence.tool_manifest_complete is False
    assert evidence.governance_level is not GovernanceLevel.ENFORCED


async def test_an_empty_inventory_cannot_certify_its_own_computed_digest(
    sqlite_db: Any,
) -> None:
    """R8: recomputing the digest made the declaration consistent, not true.

    Audit round 2's reproduction, and the one the test above misses. It does
    not need a *forged* fingerprint at all: it registers zero identities under
    self-declared ``complete`` coverage and attests the digest the server
    itself computes for the empty set. Both sides then agree honestly, the
    comparison passes, and the run reached ``ENFORCED`` while governing
    nothing.

    The fix is that an empty identity set cannot complete a manifest whatever
    it agrees with. A registration enumerating no tools is an adapter's bare
    assertion that it governs everything by governing nothing -- there is no
    evidence in it, so it cannot support the level that claims evidence.
    """
    empty_digest = recompute_inventory_fingerprint(())
    await InventoryRegistrationRepository(sqlite_db).register(make_registration(tools=()))
    await RunAttestationRepository(sqlite_db).record(
        sign_attestation(
            make_payload(inventory_fingerprint=empty_digest),
            make_signer(),
        )
    )

    evidence = await make_provider(sqlite_db).evidence_for_run("corr-alpha")

    assert evidence is not None
    assert evidence.tool_manifest_complete is False
    assert evidence.governance_level is not GovernanceLevel.ENFORCED


async def test_a_truthful_adapter_still_reaches_enforced(sqlite_db: Any) -> None:
    """The positive control: a run attesting the real digest is ENFORCED.

    Without this, a regression that simply never completed a manifest would
    satisfy every negative test above. This is also what proves the comparison
    is still *live* -- one input completes the manifest and another does not,
    which is exactly what deriving the attested fingerprint from the
    registration would have destroyed.
    """
    registration = make_registration()
    await InventoryRegistrationRepository(sqlite_db).register(registration)
    await RunAttestationRepository(sqlite_db).record(
        sign_attestation(
            make_payload(inventory_fingerprint=registration.inventory_fingerprint),
            make_signer(),
        )
    )

    evidence = await make_provider(sqlite_db).evidence_for_run("corr-alpha")

    assert evidence is not None
    assert evidence.tool_manifest_complete is True
    assert evidence.governance_level is GovernanceLevel.ENFORCED


# --------------------------------------------------------------------------
# R12 -- identities, not labels: a substituted tool changes the digest
# --------------------------------------------------------------------------


def test_a_substituted_tool_changes_the_digest_under_the_same_name() -> None:
    """R12: swapping what answers to a name is visible in the fingerprint."""
    substituted = (
        RegisteredTool(name="search", fingerprint="fp-search"),
        RegisteredTool(name="send_email", fingerprint="fp-IMPOSTOR"),
    )

    assert recompute_inventory_fingerprint(substituted) != (recompute_inventory_fingerprint(TOOLS))


def test_the_digest_is_order_insensitive() -> None:
    """Two registrations of the same tool set agree whatever order they list."""
    assert recompute_inventory_fingerprint(tuple(reversed(TOOLS))) == (
        recompute_inventory_fingerprint(TOOLS)
    )


def test_the_recomputed_digest_matches_the_sdk_projection() -> None:
    """The server's digest must equal the one the adapter computes.

    Governance may not import the SDK's implementation (it may depend only on
    ``{contracts, platform}``), so the scheme is restated in
    ``zeroth.governance.attestations.inventory``. This is the test that keeps
    the restatement honest: a truthful adapter's attested fingerprint has to
    equal the digest the server recomputes for its registration, or no run
    could ever be ENFORCED and every "not enforced" test above would pass for
    the wrong reason.

    Built from the SDK's own dataclasses rather than a stand-in, because
    ``inventory_fingerprint`` runs its input through exact-type gates -- a
    duck-typed double is refused, which would turn this cross-check into a
    skip that proves nothing.
    """
    sdk_inventory = ToolInventory(
        entries=tuple(
            ToolInventoryEntry(identity=ToolIdentity(name=t.name, fingerprint=t.fingerprint))
            for t in TOOLS
        ),
        coverage=InventoryCoverage.COMPLETE,
    )

    assert recompute_inventory_fingerprint(TOOLS) == sdk_inventory_fingerprint(sdk_inventory)


# --------------------------------------------------------------------------
# R8/R12 -- one name cannot carry two identities
# --------------------------------------------------------------------------

IMPOSTOR = RegisteredTool(name="send_email", fingerprint="fp-IMPOSTOR")
"""A second callable answering to a name :data:`REGISTERED_TOOLS` already binds."""


def test_a_registration_cannot_name_the_same_tool_twice() -> None:
    """A repeated name is refused outright, identical fingerprints included.

    The boring half of the defect: two entries that agree on everything are
    still two entries, and the recomputed digest happily covers both. Refusing
    the shape rather than de-duplicating it keeps "what the adapter declared"
    and "what the server registered" the same object -- silently collapsing a
    duplicate would let a caller's declaration and the stored inventory differ
    without anything saying so.
    """
    with pytest.raises(ValidationError, match="same tool twice"):
        make_registration(tools=(*TOOLS, TOOLS[1]))


def test_a_registration_cannot_pair_two_fingerprints_with_one_name() -> None:
    """The attack: an impostor registered *beside* the tool it impersonates.

    Audit round 3 made admission match on ``(name, fingerprint)`` exactly, so
    ``fp-IMPOSTOR`` arriving as ``send_email`` is denied. Registering the
    impostor as a second entry under the same name defeats that -- both pairs
    land in the registered set, both are matched exactly, and coverage stays
    ``complete`` because the count still equals what was declared. Nothing
    about the digest catches it: it recomputes cleanly over both pairs.
    """
    with pytest.raises(ValidationError, match="same tool twice"):
        make_registration(tools=(*TOOLS, IMPOSTOR))


def test_the_wire_body_refuses_a_repeated_name_too() -> None:
    """The same rule at the transport boundary, where it is a 422 not a 500.

    ``InventoryRegistration`` is built inside the route handler, so its raise
    would surface as an unhandled 500. The submission model carries the rule as
    well; ``tests/service/test_enforcement_api.py`` asserts the status code
    that follows from it.
    """
    with pytest.raises(ValidationError, match="same tool twice"):
        InventorySubmission(
            deployment_ref="dep-alpha",
            graph_version="graph-v1",
            adapter_version="0.1.0",
            coverage="complete",
            tools=(*TOOLS, IMPOSTOR),
        )


async def _store_duplicated_registration(database: Any) -> str:
    """Write a registration row whose identities repeat a name, bypassing the model.

    The model now refuses this shape, so the only way such a row exists is to
    predate the rule. Storing a legitimate registration and then rewriting its
    identity column reproduces exactly that: a row on disk that no current code
    path could have written.

    Returns:
        The digest the duplicated identity set produces -- what an attacker
        holding this row would attest.
    """
    await InventoryRegistrationRepository(database).register(
        make_registration(tools=REGISTERED_TOOLS, coverage="complete")
    )
    duplicated = (*REGISTERED_TOOLS, IMPOSTOR)
    digest = recompute_inventory_fingerprint(duplicated)
    async with database.transaction(write_lock=True) as connection:
        await connection.execute(
            "UPDATE tool_inventory_registrations "
            "SET tool_identities_json = ?, inventory_fingerprint = ?, tool_count = ?",
            (
                json.dumps(
                    [{"name": t.name, "fingerprint": t.fingerprint} for t in duplicated]
                ),
                digest,
                len(duplicated),
            ),
        )
    return digest


async def test_a_stored_duplicate_row_is_unreadable_rather_than_authorizing(
    sqlite_db: Any,
) -> None:
    """A pre-existing duplicated row fails closed on read.

    The deliberate consequence of refusing the shape in the model: hydration
    raises instead of returning an inventory that authorizes two callables
    under one label. Asserted directly so the ENFORCED test below cannot pass
    because the row was quietly ignored.
    """
    await _store_duplicated_registration(sqlite_db)

    with pytest.raises(ValidationError, match="same tool twice"):
        await InventoryRegistrationRepository(sqlite_db).latest_for_deployment(
            "tenant-alpha",
            "dep-alpha",
        )


async def test_a_duplicated_inventory_cannot_reach_enforced(sqlite_db: Any) -> None:
    """The end-to-end reproduction: the impostor never buys ``ENFORCED``.

    The attestation binds the digest of the *duplicated* set -- the strongest
    form of the attack, in which the run's fingerprint and the stored
    registration agree honestly. The registration is unreadable, so the
    provider has no inventory to complete the manifest with and the level
    cannot be ``ENFORCED``.

    ``None`` rather than downgraded evidence, because
    :meth:`PersistedCapabilityEvidenceProvider.evidence_for_run` reads the
    registration inside its "unreadable state is no evidence" block. Asserted
    as ``None`` exactly: an ``is not ENFORCED`` assertion would also hold if the
    provider had returned evidence for entirely unrelated reasons.
    """
    digest = await _store_duplicated_registration(sqlite_db)
    await RunAttestationRepository(sqlite_db).record(
        sign_attestation(make_payload(inventory_fingerprint=digest), make_signer())
    )

    evidence = await make_provider(sqlite_db).evidence_for_run("corr-alpha")

    assert evidence is None
