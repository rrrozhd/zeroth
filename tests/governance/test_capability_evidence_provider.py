"""Trust-boundary tests for persisted capability evidence (ZER-8 S5).

``CapabilityReporter._validated_level`` grants ``ENFORCED`` only when the
evidence it reads carries ``governance_level is ENFORCED`` *and*
``tool_manifest_complete``. Nothing in the SDK can mint ``ENFORCED``
(``report_tool_enforcement`` returns OBSERVED or ADMISSION), so that claim can
only ever originate client-side -- which makes the verifying provider the one
place where the claim is turned back into a server-side fact.

Every test here exists to pin one half of that: the provider recomputes
``tool_manifest_complete`` from the inventory *it* has stored for the
deployment, and treats the client's ``claimed_level`` strictly as a ceiling
that may lower the outcome. The R15 positive control is load-bearing -- without
a case that genuinely reaches ``ENFORCED``, every negative test below could
pass for the trivial reason that the provider never grants it at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from zeroth.contracts.langgraph_gateway.models import GovernanceLevel
from zeroth.governance.attestations import (
    InventoryRegistration,
    InventoryRegistrationRepository,
    PersistedCapabilityEvidenceProvider,
    RunAttestationPayload,
    RunAttestationRepository,
    attestation_digest,
    sign_attestation,
)
from zeroth.governance.attestations.inventory import (
    RegisteredTool,
    recompute_inventory_fingerprint,
)
from zeroth.governance.attestations.versions import ADAPTER_VERSION
from zeroth.governance.langgraph_gateway.capabilities import CapabilityReporter
from zeroth.platform.signing import EnvHmacSigner

TENANT = "tenant-alpha"
DEPLOYMENT = "dep-alpha"
CORRELATION = "corr-alpha"
GRAPH_VERSION = "graph-v1"
REGISTERED_TOOLS = (
    RegisteredTool(name="search", fingerprint="fp-search"),
    RegisteredTool(name="send_email", fingerprint="fp-send-email"),
    RegisteredTool(name="delete_record", fingerprint="fp-delete-record"),
)
FINGERPRINT = recompute_inventory_fingerprint(REGISTERED_TOOLS)
"""The digest the server recomputes for :data:`REGISTERED_TOOLS`.

Derived rather than a literal: since audit round 1 a registration's
fingerprint is computed from its tool identities and cannot be declared,
so a hand-written constant here would simply never match and would reduce
the ENFORCED positive control to another negative test.
"""

ISSUED_AT = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)


def make_signer() -> EnvHmacSigner:
    """Return an HMAC signer holding two keys so a key-id swap is a real test."""
    return EnvHmacSigner(
        key_id="k1",
        keys={"k1": b"attestation-secret", "k2": b"rotated-secret"},
    )


def make_payload(**overrides: Any) -> RunAttestationPayload:
    """Build a payload that, unmodified, is eligible for ``ENFORCED``.

    ``adapter_version`` is the real ``ADAPTER_VERSION`` on purpose: a
    placeholder string would classify as ``ADAPTER_MISMATCH`` and silently
    reduce the positive control to yet another negative test.
    """
    fields: dict[str, Any] = {
        "correlation_id": CORRELATION,
        "tenant_id": TENANT,
        "deployment_ref": DEPLOYMENT,
        "graph_version": GRAPH_VERSION,
        "adapter_version": ADAPTER_VERSION,
        "inventory_fingerprint": FINGERPRINT,
        "inventory_coverage": "complete",
        "tool_count": 3,
        "claimed_level": "enforced",
        "issued_at": ISSUED_AT,
        "expires_at": ISSUED_AT + timedelta(minutes=5),
    }
    fields.update(overrides)
    return RunAttestationPayload(**fields)


def make_registration(**overrides: Any) -> InventoryRegistration:
    """Build a COMPLETE-coverage registration matching the default payload."""
    fields: dict[str, Any] = {
        "tenant_id": TENANT,
        "deployment_ref": DEPLOYMENT,
        "graph_version": GRAPH_VERSION,
        "adapter_version": ADAPTER_VERSION,
        "coverage": "complete",
        "tools": REGISTERED_TOOLS,
        "registered_at": ISSUED_AT - timedelta(minutes=1),
    }
    fields.update(overrides)
    return InventoryRegistration(**fields)


def make_provider(database: Any, **overrides: Any) -> PersistedCapabilityEvidenceProvider:
    """Build a provider bound to the default tenant and deployment."""
    fields: dict[str, Any] = {
        "attestations": RunAttestationRepository(database),
        "registrations": InventoryRegistrationRepository(database),
        "signer": make_signer(),
        "tenant_id": TENANT,
        "deployment_ref": DEPLOYMENT,
        "expected_graph_version": GRAPH_VERSION,
        "now": lambda: ISSUED_AT + timedelta(seconds=5),
    }
    fields.update(overrides)
    return PersistedCapabilityEvidenceProvider(**fields)


async def seed(
    database: Any,
    *,
    payload: RunAttestationPayload | None = None,
    registration: InventoryRegistration | None = None,
) -> None:
    """Store one signed attestation and, unless suppressed, one registration."""
    signed = sign_attestation(payload or make_payload(), make_signer())
    await RunAttestationRepository(database).record(signed)
    if registration is not None:
        await InventoryRegistrationRepository(database).register(registration)


# --------------------------------------------------------------------------
# R15 -- the positive control.
# --------------------------------------------------------------------------


async def test_fresh_signed_attestation_over_complete_inventory_is_enforced(sqlite_db) -> None:
    """R15: signature + matching COMPLETE inventory + matching versions -> ENFORCED."""
    await seed(sqlite_db, registration=make_registration())

    evidence = await make_provider(sqlite_db).evidence_for_run(CORRELATION)

    assert evidence is not None
    assert evidence.governance_level is GovernanceLevel.ENFORCED
    assert evidence.tool_manifest_complete is True
    assert evidence.signature_valid is True
    assert evidence.correlation_id == CORRELATION
    assert evidence.graph_version == GRAPH_VERSION
    assert evidence.observed_at == ISSUED_AT
    assert evidence.observed_at.tzinfo is not None


async def test_reporter_reports_enforced_for_the_positive_control(sqlite_db) -> None:
    """R15 at the seam that decides: the reporter itself must say ENFORCED."""
    await seed(sqlite_db, registration=make_registration())
    reporter = CapabilityReporter(
        make_provider(sqlite_db),
        expected_graph_version=GRAPH_VERSION,
        now=lambda: ISSUED_AT + timedelta(seconds=5),
    )

    level = await reporter.level_for_run(CORRELATION)

    assert level is GovernanceLevel.ENFORCED


# --------------------------------------------------------------------------
# R10/R11/R12 -- the manifest must come from stored state, not the payload.
# --------------------------------------------------------------------------


async def test_no_registered_inventory_is_never_enforced(sqlite_db) -> None:
    """R10: a valid attestation with nothing registered cannot be ENFORCED."""
    await seed(sqlite_db, registration=None)

    evidence = await make_provider(sqlite_db).evidence_for_run(CORRELATION)

    assert evidence is not None
    assert evidence.signature_valid is True
    assert evidence.tool_manifest_complete is False
    assert evidence.governance_level is GovernanceLevel.OBSERVED


async def test_partial_registered_coverage_is_never_enforced(sqlite_db) -> None:
    """R11: stored PARTIAL coverage wins over a payload claiming complete."""
    await seed(sqlite_db, registration=make_registration(coverage="partial"))

    evidence = await make_provider(sqlite_db).evidence_for_run(CORRELATION)

    assert evidence is not None
    assert evidence.tool_manifest_complete is False
    assert evidence.governance_level is not GovernanceLevel.ENFORCED


async def test_reporter_refuses_enforced_for_partial_coverage(sqlite_db) -> None:
    """R11 at the seam that decides: the reporter must not report ENFORCED."""
    await seed(sqlite_db, registration=make_registration(coverage="partial"))
    reporter = CapabilityReporter(
        make_provider(sqlite_db),
        expected_graph_version=GRAPH_VERSION,
        now=lambda: ISSUED_AT + timedelta(seconds=5),
    )

    level = await reporter.level_for_run(CORRELATION)

    assert level is not GovernanceLevel.ENFORCED


async def test_fingerprint_drift_is_never_enforced(sqlite_db) -> None:
    """R12: a registration for a different inventory cannot back this run.

    The drift is expressed as a different *tool set* rather than a declared
    digest string, because a registration's fingerprint is recomputed from its
    identities and can no longer be asserted. That is the point of R12: one
    tool swapped for another under the same name moves the digest.
    """
    drifted = (
        RegisteredTool(name="search", fingerprint="fp-search"),
        RegisteredTool(name="send_email", fingerprint="fp-send-email"),
        RegisteredTool(name="delete_record", fingerprint="fp-SUBSTITUTED"),
    )
    await seed(sqlite_db, registration=make_registration(tools=drifted))

    evidence = await make_provider(sqlite_db).evidence_for_run(CORRELATION)

    assert evidence is not None
    assert evidence.tool_manifest_complete is False
    assert evidence.governance_level is not GovernanceLevel.ENFORCED


async def test_payload_coverage_alone_cannot_complete_the_manifest(sqlite_db) -> None:
    """The manifest flag is derived from stored state, never copied from the wire."""
    await seed(
        sqlite_db,
        payload=make_payload(inventory_coverage="complete"),
        registration=make_registration(coverage="partial"),
    )

    evidence = await make_provider(sqlite_db).evidence_for_run(CORRELATION)

    assert evidence is not None
    assert evidence.tool_manifest_complete is False


# --------------------------------------------------------------------------
# R9/R5 -- signature failures.
# --------------------------------------------------------------------------


async def test_unsigned_attestation_is_admission(sqlite_db) -> None:
    """R5: an attestation stored without a signature never verifies."""
    await RunAttestationRepository(sqlite_db).record(sign_attestation(make_payload(), None))
    await InventoryRegistrationRepository(sqlite_db).register(make_registration())

    evidence = await make_provider(sqlite_db).evidence_for_run(CORRELATION)

    assert evidence is not None
    assert evidence.signature_valid is False
    assert evidence.governance_level is GovernanceLevel.ADMISSION


async def test_tampered_payload_is_admission(sqlite_db) -> None:
    """R9: editing the payload and recomputing the digest still fails the key check."""
    signed = sign_attestation(make_payload(tool_count=3), make_signer())
    edited = signed.payload.model_copy(update={"tool_count": 99})
    tampered = signed.model_copy(update={"payload": edited, "digest": attestation_digest(edited)})
    await RunAttestationRepository(sqlite_db).record(tampered)
    await InventoryRegistrationRepository(sqlite_db).register(make_registration())

    evidence = await make_provider(sqlite_db).evidence_for_run(CORRELATION)

    assert evidence is not None
    assert evidence.signature_valid is False
    assert evidence.governance_level is GovernanceLevel.ADMISSION


# --------------------------------------------------------------------------
# Tenancy -- the reason the provider is bound at construction.
# --------------------------------------------------------------------------


async def test_attestation_is_invisible_across_tenants(sqlite_db) -> None:
    """A correlation id is attacker-suppliable, so lookup must be tenant-scoped."""
    await seed(sqlite_db, registration=make_registration())

    other = make_provider(sqlite_db, tenant_id="tenant-beta")

    assert await other.evidence_for_run(CORRELATION) is None


async def test_unknown_correlation_returns_none(sqlite_db) -> None:
    """No attestation for the run means no evidence, not weak evidence."""
    await seed(sqlite_db, registration=make_registration())

    assert await make_provider(sqlite_db).evidence_for_run("corr-unknown") is None


# --------------------------------------------------------------------------
# claimed_level is a ceiling, never a lift.
# --------------------------------------------------------------------------


async def test_claimed_enforced_cannot_raise_an_observed_result(sqlite_db) -> None:
    """A claim of ``enforced`` over facts that only support OBSERVED stays OBSERVED."""
    await seed(sqlite_db, payload=make_payload(claimed_level="enforced"), registration=None)

    evidence = await make_provider(sqlite_db).evidence_for_run(CORRELATION)

    assert evidence is not None
    assert evidence.governance_level is GovernanceLevel.OBSERVED


async def test_claimed_observed_lowers_an_otherwise_enforced_result(sqlite_db) -> None:
    """A client may ask for less governance than the server can prove."""
    await seed(
        sqlite_db,
        payload=make_payload(claimed_level="observed"),
        registration=make_registration(),
    )

    evidence = await make_provider(sqlite_db).evidence_for_run(CORRELATION)

    assert evidence is not None
    assert evidence.governance_level is GovernanceLevel.OBSERVED
    assert evidence.tool_manifest_complete is True


async def test_unparsable_claimed_level_falls_to_admission(sqlite_db) -> None:
    """An unrecognised claim is not a reason to grant anything."""
    await seed(
        sqlite_db,
        payload=make_payload(claimed_level="super-enforced"),
        registration=make_registration(),
    )

    evidence = await make_provider(sqlite_db).evidence_for_run(CORRELATION)

    assert evidence is not None
    assert evidence.governance_level is GovernanceLevel.ADMISSION


# --------------------------------------------------------------------------
# R17 -- version agreement.
# --------------------------------------------------------------------------


async def test_adapter_version_mismatch_is_never_enforced(sqlite_db) -> None:
    """R17: a drifted adapter contract cannot back a full-enforcement claim."""
    await seed(
        sqlite_db,
        payload=make_payload(adapter_version="9.9"),
        registration=make_registration(),
    )

    evidence = await make_provider(sqlite_db).evidence_for_run(CORRELATION)

    assert evidence is not None
    assert evidence.tool_manifest_complete is True
    assert evidence.governance_level is not GovernanceLevel.ENFORCED


async def test_graph_version_mismatch_is_never_enforced(sqlite_db) -> None:
    """R17: a graph the gateway did not expect cannot back full enforcement."""
    await seed(
        sqlite_db,
        payload=make_payload(graph_version="graph-v2"),
        registration=make_registration(graph_version="graph-v2"),
    )

    evidence = await make_provider(sqlite_db).evidence_for_run(CORRELATION)

    assert evidence is not None
    assert evidence.tool_manifest_complete is True
    assert evidence.governance_level is not GovernanceLevel.ENFORCED


async def test_unknown_expected_graph_version_is_never_enforced(sqlite_db) -> None:
    """An absent expectation is the absence of evidence, not agreement."""
    await seed(sqlite_db, registration=make_registration())

    provider = make_provider(sqlite_db, expected_graph_version=None)
    evidence = await provider.evidence_for_run(CORRELATION)

    assert evidence is not None
    assert evidence.tool_manifest_complete is True
    assert evidence.governance_level is not GovernanceLevel.ENFORCED


async def test_expired_attestation_is_not_enforced(sqlite_db) -> None:
    """``expires_at`` is signed; the provider is where it is enforced against a clock."""
    await seed(
        sqlite_db,
        payload=make_payload(expires_at=ISSUED_AT + timedelta(seconds=1)),
        registration=make_registration(),
    )

    evidence = await make_provider(sqlite_db).evidence_for_run(CORRELATION)

    assert evidence is not None
    assert evidence.signature_valid is True
    assert evidence.governance_level is GovernanceLevel.OBSERVED


# --------------------------------------------------------------------------
# Malformed stored state.
# --------------------------------------------------------------------------


async def test_malformed_stored_payload_returns_none(sqlite_db) -> None:
    """A corrupt row is a storage fault; the provider still never raises."""
    await seed(sqlite_db, registration=make_registration())
    async with sqlite_db.transaction(write_lock=True) as connection:
        await connection.execute(
            "UPDATE run_attestations SET payload_json = ? WHERE correlation_id = ?",
            ("{not-json", CORRELATION),
        )

    assert await make_provider(sqlite_db).evidence_for_run(CORRELATION) is None


# --------------------------------------------------------------------------
# Store round-trips.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("microsecond", [0, 123456])
async def test_attestation_round_trips_byte_identically(sqlite_db, microsecond: int) -> None:
    """Reload must reproduce the signed bytes, or verification fails for no reason.

    The microsecond case is not decoration: production attestations come from
    ``utc_now()``, so a round trip that silently truncated sub-second precision
    would break every real ``ENFORCED`` classification while a whole-second
    fixture kept passing.
    """
    issued_at = ISSUED_AT.replace(microsecond=microsecond)
    signed = sign_attestation(
        make_payload(issued_at=issued_at, expires_at=issued_at + timedelta(minutes=5)),
        make_signer(),
    )
    repository = RunAttestationRepository(sqlite_db)
    await repository.record(signed)

    loaded = await repository.find_by_correlation(TENANT, CORRELATION)

    assert loaded is not None
    assert loaded.payload == signed.payload
    assert attestation_digest(loaded.payload) == signed.digest
    assert loaded.signature == signed.signature


async def test_recording_the_same_correlation_twice_keeps_the_first(sqlite_db) -> None:
    """First write wins, so a later attestation cannot overwrite a run's evidence."""
    repository = RunAttestationRepository(sqlite_db)
    await repository.record(sign_attestation(make_payload(tool_count=3), make_signer()))
    await repository.record(sign_attestation(make_payload(tool_count=99), make_signer()))

    loaded = await repository.find_by_correlation(TENANT, CORRELATION)

    assert loaded is not None
    assert loaded.payload.tool_count == 3


async def test_latest_registration_wins(sqlite_db) -> None:
    """Registrations are append-only; the newest row is the deployment's inventory."""
    repository = InventoryRegistrationRepository(sqlite_db)
    superseded = (RegisteredTool(name="search", fingerprint="fp-old"),)
    await repository.register(make_registration(tools=superseded))
    await repository.register(
        make_registration(
            tools=REGISTERED_TOOLS,
            registered_at=ISSUED_AT,
        )
    )

    latest = await repository.latest_for_deployment(TENANT, DEPLOYMENT)

    assert latest is not None
    assert latest.tools == REGISTERED_TOOLS
    assert latest.inventory_fingerprint == FINGERPRINT
    assert latest.inventory_fingerprint != recompute_inventory_fingerprint(superseded)


async def test_a_registration_round_trips_every_recorded_field(sqlite_db) -> None:
    """R8: every column a registration is stored in comes back as it went in.

    The values are deliberately distinct from each other and from the module
    fixtures: with the defaults, an assertion could be satisfied by a
    neighbouring column, so a ``graph_version``/``adapter_version`` swap in
    ``_row_to_registration`` would round-trip undetected. ``coverage`` is a
    plain string in the model, so a non-``complete`` token stores and reloads
    fine -- what the token *means* is pinned separately below.
    """
    repository = InventoryRegistrationRepository(sqlite_db)
    roundtrip_tools = (
        RegisteredTool(name="alpha", fingerprint="fp-alpha"),
        RegisteredTool(name="beta", fingerprint="fp-beta"),
    )
    registration = make_registration(
        graph_version="graph-roundtrip",
        adapter_version="adapter-roundtrip",
        coverage="partial",
        tools=roundtrip_tools,
    )

    stored = await repository.register(registration)
    latest = await repository.latest_for_deployment(TENANT, DEPLOYMENT)

    assert latest is not None
    assert latest.registration_id == stored.registration_id
    assert latest.tenant_id == TENANT
    assert latest.deployment_ref == DEPLOYMENT
    assert latest.graph_version == "graph-roundtrip"
    assert latest.adapter_version == "adapter-roundtrip"
    assert latest.inventory_fingerprint == recompute_inventory_fingerprint(roundtrip_tools)
    assert latest.coverage == "partial"
    assert latest.tool_count == 2
    assert latest.tools == roundtrip_tools
    assert latest.registered_at == registration.registered_at


async def test_registration_lookup_is_tenant_scoped(sqlite_db) -> None:
    """One tenant's registration must not satisfy another tenant's deployment."""
    repository = InventoryRegistrationRepository(sqlite_db)
    await repository.register(make_registration())

    assert await repository.latest_for_deployment("tenant-beta", DEPLOYMENT) is None


@pytest.mark.parametrize("coverage", ["COMPLETE", "Complete", " complete", "full"])
async def test_only_the_exact_complete_token_completes_the_manifest(
    sqlite_db,
    coverage: str,
) -> None:
    """Widening the accepted coverage vocabulary would be a fail-open change."""
    await seed(sqlite_db, registration=make_registration(coverage=coverage))

    evidence = await make_provider(sqlite_db).evidence_for_run(CORRELATION)

    assert evidence is not None
    assert evidence.tool_manifest_complete is False


# --------------------------------------------------------------------------
# R9 -- an attestation is evidence only for the deployment it was signed for.
# --------------------------------------------------------------------------


class ForeignAttestationRepository:
    """Repository that ignores scoping and hands back one fixed attestation.

    Stands in for the layer below the provider having been widened, or
    answering from a stale index. The provider must not rely on its collaborator
    to enforce the binding that the provider itself is defined by.

    It implements ``find_for_deployment`` -- the method the provider actually
    calls -- and counts the calls. An earlier revision of this double still
    carried the obsolete ``find_by_correlation``, so the provider's lookup
    raised ``AttributeError``, which ``evidence_for_run`` catches as "no
    readable state" and answers ``None``. All three tests below passed without
    ``_binds_to_this_provider`` ever running: audit round 2 caught it, and
    removing the binding re-check left them green. ``calls`` is what makes that
    failure mode impossible to repeat -- a double the provider never reaches
    cannot report having been reached.
    """

    def __init__(self, signed: Any) -> None:
        self._signed = signed
        self.calls = 0

    async def find_for_deployment(self, *args: Any, **kwargs: Any) -> Any:
        """Return the attestation regardless of what was asked for."""
        del args, kwargs
        self.calls += 1
        return self._signed


async def test_an_attestation_for_another_deployment_is_never_paired_with_this_one(
    sqlite_db,
) -> None:
    """R9: a deployment-B attestation must not be judged against deployment A.

    The audited defect: ``find_by_correlation`` was scoped by tenant and
    correlation only, and its result was paired with
    ``latest_for_deployment(self._deployment_ref)``. ``payload.deployment_ref``
    is bound *inside* the signed bytes, so the attestation honestly says it
    belongs to ``dep-beta`` -- nothing was forged. It was simply never compared
    against the provider's own binding, so B's signed run inherited A's
    complete inventory and reached ENFORCED.
    """
    await seed(
        sqlite_db,
        payload=make_payload(deployment_ref="dep-beta"),
        registration=make_registration(),
    )

    evidence = await make_provider(sqlite_db).evidence_for_run(CORRELATION)

    assert evidence is None


async def test_a_foreign_payload_is_refused_even_if_the_store_returns_it(
    sqlite_db,
) -> None:
    """R9: the provider re-checks the binding rather than trusting the lookup.

    Defence in depth for the test above. With the deployment in the SQL
    predicate the foreign row is unreachable through the real repository, so
    this substitutes a repository that returns it anyway and asserts the
    provider still refuses.
    """
    signed = sign_attestation(make_payload(deployment_ref="dep-beta"), make_signer())
    await InventoryRegistrationRepository(sqlite_db).register(make_registration())
    attestations = ForeignAttestationRepository(signed)
    provider = make_provider(sqlite_db, attestations=attestations)

    assert await provider.evidence_for_run(CORRELATION) is None
    # Without this, an unreachable double reads as a pass: the provider would
    # be refusing because it never got an attestation, not because it rejected
    # the one it was handed.
    assert attestations.calls == 1


async def test_a_foreign_tenant_payload_is_refused_even_if_the_store_returns_it(
    sqlite_db,
) -> None:
    """R9: the same re-check covers the tenant half of the binding."""
    signed = sign_attestation(make_payload(tenant_id="tenant-beta"), make_signer())
    await InventoryRegistrationRepository(sqlite_db).register(make_registration())
    attestations = ForeignAttestationRepository(signed)
    provider = make_provider(sqlite_db, attestations=attestations)

    assert await provider.evidence_for_run(CORRELATION) is None
    assert attestations.calls == 1


async def test_a_replayed_correlation_is_refused_even_if_the_store_returns_it(
    sqlite_db,
) -> None:
    """R9: evidence answered for one correlation must be *that* correlation's."""
    signed = sign_attestation(make_payload(correlation_id="corr-other"), make_signer())
    await InventoryRegistrationRepository(sqlite_db).register(make_registration())
    attestations = ForeignAttestationRepository(signed)
    provider = make_provider(sqlite_db, attestations=attestations)

    assert await provider.evidence_for_run(CORRELATION) is None
    assert attestations.calls == 1
