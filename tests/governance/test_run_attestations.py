"""Trust-boundary tests for the signed run-start attestation (ZER-8 S1).

The gateway grants ``ENFORCED`` only from evidence it can attribute, and
evidence lookup is keyed on ``correlation_id`` -- a value the client supplies
and which nothing verifies (``zeroth.integrations.langgraph._correlation``).
These tests therefore assert that *every* identity field is bound inside the
signed material, so an attestation minted for one run cannot be replayed under
another correlation, tenant, deployment, graph or tool inventory.

Every tamper test simulates the strong attacker: it edits the payload **and**
recomputes the unkeyed digest, because the unkeyed digest is trivially
recomputable. The signature must still fail.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from zeroth.governance.attestations import (
    RunAttestationPayload,
    SignedRunAttestation,
    attestation_digest,
    sign_attestation,
    verify_attestation,
)
from zeroth.platform.signing import EnvHmacSigner, NullSigner

ISSUED_AT = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)


def make_signer() -> EnvHmacSigner:
    """Return an HMAC signer holding two keys so key-id swaps are a real test."""
    return EnvHmacSigner(
        key_id="k1",
        keys={"k1": b"attestation-secret", "k2": b"rotated-secret"},
    )


def make_payload(**overrides: Any) -> RunAttestationPayload:
    """Build a complete, valid run attestation payload."""
    fields: dict[str, Any] = {
        "correlation_id": "corr-alpha",
        "tenant_id": "tenant-alpha",
        "deployment_ref": "dep-alpha",
        "graph_version": "graph-v1",
        "adapter_version": "adapter-v1",
        "inventory_fingerprint": "fingerprint-alpha",
        "inventory_coverage": "complete",
        "tool_count": 3,
        "claimed_level": "enforced",
        "issued_at": ISSUED_AT,
        "expires_at": ISSUED_AT + timedelta(minutes=5),
    }
    fields.update(overrides)
    return RunAttestationPayload(**fields)


def retamper(signed: SignedRunAttestation, **overrides: Any) -> SignedRunAttestation:
    """Edit payload fields and recompute the unkeyed digest, as an attacker would."""
    tampered = signed.payload.model_copy(update=overrides)
    return signed.model_copy(
        update={"payload": tampered, "digest": attestation_digest(tampered)}
    )


# --------------------------------------------------------------------------
# round trip / positive control
# --------------------------------------------------------------------------


def test_sign_then_verify_round_trip() -> None:
    signer = make_signer()
    signed = sign_attestation(make_payload(), signer)

    # Positive control: without a real signature every "-> False" below is vacuous.
    assert signed.signature is not None
    assert signed.signing_key_id == "k1"
    assert signed.signing_algorithm == "HS256"
    assert signed.digest == attestation_digest(signed.payload)
    assert verify_attestation(signed, signer) is True


def test_wire_round_trip_still_verifies() -> None:
    """A consumer that parses the attestation off the wire must still verify it.

    The digest is taken over ``model_dump(mode="json")``, so a datetime that does
    not survive serialize -> parse -> re-dump byte-identically would make
    *legitimate* attestations fail closed. Uses a microsecond-bearing timestamp
    because that is what production carries.
    """
    issued = datetime.now(UTC)
    signed = sign_attestation(
        make_payload(issued_at=issued, expires_at=issued + timedelta(minutes=5)),
        make_signer(),
    )
    rehydrated = SignedRunAttestation.model_validate_json(signed.model_dump_json())

    assert rehydrated.digest == attestation_digest(rehydrated.payload)
    assert verify_attestation(rehydrated, make_signer()) is True


def test_digest_is_deterministic_for_equal_payloads() -> None:
    assert attestation_digest(make_payload()) == attestation_digest(make_payload())


def test_digest_covers_every_declared_field() -> None:
    baseline = attestation_digest(make_payload())
    for field in RunAttestationPayload.model_fields:
        if field == "schema_version":
            continue
        mutated = make_payload(**{field: _other_value(field)})
        assert attestation_digest(mutated) != baseline, f"{field} not covered by digest"


def _other_value(field: str) -> Any:
    """Return a different-but-valid value for ``field``."""
    if field == "tool_count":
        return 99
    if field == "inventory_coverage":
        return "partial"
    if field in {"issued_at", "expires_at"}:
        return ISSUED_AT + timedelta(days=1)
    return f"attacker-{field}"


# --------------------------------------------------------------------------
# replay / substitution: each identity field independently
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("correlation_id", "corr-victim"),
        ("tenant_id", "tenant-victim"),
        ("deployment_ref", "dep-victim"),
        ("graph_version", "graph-v2"),
        ("adapter_version", "adapter-v2"),
        ("inventory_fingerprint", "fingerprint-victim"),
        ("inventory_coverage", "partial"),
        ("tool_count", 99),
    ],
)
def test_field_tamper_with_recomputed_digest_fails(field: str, value: Any) -> None:
    signer = make_signer()
    signed = sign_attestation(make_payload(), signer)
    forged = retamper(signed, **{field: value})

    assert forged.digest != signed.digest
    assert verify_attestation(forged, signer) is False


def test_claimed_level_escalation_fails() -> None:
    signer = make_signer()
    signed = sign_attestation(make_payload(claimed_level="observed"), signer)
    forged = retamper(signed, claimed_level="enforced")

    assert verify_attestation(forged, signer) is False


# --------------------------------------------------------------------------
# downgrade / key substitution
# --------------------------------------------------------------------------


def test_signing_key_id_swap_fails() -> None:
    signer = make_signer()
    signed = sign_attestation(make_payload(), signer)
    forged = signed.model_copy(update={"signing_key_id": "k2"})

    assert verify_attestation(forged, signer) is False


def test_signing_algorithm_swap_fails() -> None:
    signer = make_signer()
    signed = sign_attestation(make_payload(), signer)
    forged = signed.model_copy(update={"signing_algorithm": "none"})

    assert verify_attestation(forged, signer) is False


def test_signature_from_another_key_fails() -> None:
    issuer = EnvHmacSigner(key_id="k1", keys={"k1": b"attacker-secret"})
    signed = sign_attestation(make_payload(), issuer)

    assert verify_attestation(signed, make_signer()) is False


# --------------------------------------------------------------------------
# digest / payload divergence
# --------------------------------------------------------------------------


def test_payload_mutated_but_digest_kept_fails() -> None:
    signer = make_signer()
    signed = sign_attestation(make_payload(), signer)
    forged = signed.model_copy(
        update={"payload": signed.payload.model_copy(update={"correlation_id": "corr-victim"})}
    )

    assert forged.digest == signed.digest
    assert verify_attestation(forged, signer) is False


def test_digest_mutated_but_payload_kept_fails() -> None:
    signer = make_signer()
    signed = sign_attestation(make_payload(), signer)
    forged = signed.model_copy(update={"digest": "0" * 64})

    assert verify_attestation(forged, signer) is False


# --------------------------------------------------------------------------
# unsigned / malformed: fail closed, never raise
# --------------------------------------------------------------------------


def test_null_signer_yields_unsigned_and_never_verifies() -> None:
    null = NullSigner()
    signed = sign_attestation(make_payload(), null)

    assert signed.signature is None
    assert signed.signing_key_id is None
    assert signed.signing_algorithm is None
    assert signed.digest == attestation_digest(signed.payload)
    assert verify_attestation(signed, null) is False
    assert verify_attestation(signed, make_signer()) is False


def test_missing_signer_fails_closed() -> None:
    signed = sign_attestation(make_payload(), make_signer())

    assert verify_attestation(signed, None) is False


def test_unsigned_attestation_without_signer_fails_closed() -> None:
    signed = sign_attestation(make_payload(), None)

    assert signed.signature is None
    assert verify_attestation(signed, None) is False


@pytest.mark.parametrize(
    "bogus",
    [None, "not-an-attestation", 42, {"payload": None}, object()],
)
def test_malformed_input_returns_false_without_raising(bogus: Any) -> None:
    assert verify_attestation(bogus, make_signer()) is False


def test_payload_frozen_and_rejects_unknown_fields() -> None:
    payload = make_payload()
    with pytest.raises(ValidationError):
        payload.correlation_id = "mutated"
    with pytest.raises(ValidationError):
        RunAttestationPayload(**{**payload.model_dump(), "extra_field": "x"})


# --------------------------------------------------------------------------
# Recording outcome (ZER-8 S4): first write wins, and the loser is told.
# --------------------------------------------------------------------------


async def test_record_reports_whether_this_attestation_is_the_one_in_force(
    sqlite_db,
) -> None:
    """A caller must be able to tell "recorded" from "an earlier one is in force".

    ``ON CONFLICT DO NOTHING`` makes both cases look identical at the SQL
    level. A repository that swallowed the difference would force its callers
    to report success either way, telling a client its attestation is the
    evidence its run will be judged on when some other attestation is.
    """
    from zeroth.governance.attestations import RunAttestationRepository

    repository = RunAttestationRepository(sqlite_db)

    first = await repository.record(sign_attestation(make_payload(tool_count=3), make_signer()))
    second = await repository.record(sign_attestation(make_payload(tool_count=99), make_signer()))

    assert first.authoritative is True
    assert first.inserted is True
    assert second.authoritative is False
    assert second.inserted is False
    stored = await repository.find_by_correlation("tenant-alpha", "corr-alpha")
    assert stored is not None
    assert stored.payload.tool_count == 3


# --------------------------------------------------------------------------
# Authority is decided on the signed envelope, not on the payload digest
# --------------------------------------------------------------------------


async def test_a_rotated_key_resubmission_is_not_reported_as_authoritative(
    sqlite_db: Any,
) -> None:
    """The same claims signed under a rotated key are not this caller's evidence.

    ``attestation_digest`` covers the payload and nothing else, so an
    attestation re-signed under ``k2`` after ``k1`` was retired carries an
    identical digest and a different signature. Deciding authority on the
    digest answered True here -- the caller was told its attestation is what
    the run will be judged on, while the evidence actually in force was signed
    with a key that no longer verifies.

    The stored envelope is asserted alongside the return value: without it a
    repository that had simply stopped writing anything would satisfy the
    ``False``.
    """
    from zeroth.governance.attestations import RunAttestationRepository

    repository = RunAttestationRepository(sqlite_db)
    payload = make_payload()
    retired = sign_attestation(payload, EnvHmacSigner(key_id="k1", keys={"k1": b"old-secret"}))
    rotated = sign_attestation(payload, EnvHmacSigner(key_id="k2", keys={"k2": b"new-secret"}))

    assert retired.digest == rotated.digest, "the payload digest cannot tell these apart"
    assert retired.signature != rotated.signature

    assert (await repository.record(retired)).authoritative is True
    assert (await repository.record(rotated)).authoritative is False

    stored = await repository.find_by_correlation("tenant-alpha", "corr-alpha")
    assert stored is not None
    assert stored.signing_key_id == "k1", "first write still wins; only the answer changed"


async def test_an_unsigned_attestation_in_force_does_not_certify_a_signed_one(
    sqlite_db: Any,
) -> None:
    """A signed submission losing to an unsigned one is told it lost.

    ``NullSigner`` persists an attestation as unsigned-legacy, which never
    verifies. Its payload digest equals that of the same claims signed
    properly, so the digest comparison reported the signed submission as
    authoritative while the run's evidence remained an envelope with no
    signature at all -- the strongest form of the defect, because the caller
    has every reason to believe its own signed attestation is in force.
    """
    from zeroth.governance.attestations import RunAttestationRepository

    repository = RunAttestationRepository(sqlite_db)
    payload = make_payload()
    unsigned = sign_attestation(payload, NullSigner())
    signed = sign_attestation(payload, make_signer())

    assert unsigned.signature is None
    assert unsigned.digest == signed.digest, "the payload digest cannot tell these apart"

    assert (await repository.record(unsigned)).authoritative is True
    assert (await repository.record(signed)).authoritative is False

    stored = await repository.find_by_correlation("tenant-alpha", "corr-alpha")
    assert stored is not None
    assert stored.signature is None


async def test_an_identical_resubmission_is_still_authoritative(sqlite_db: Any) -> None:
    """The positive control: a plain retry does not become a conflict.

    Envelope equality is only safe because the shipped ``EnvHmacSigner`` is
    deterministic -- re-signing one payload with one key yields byte-identical
    bytes -- so a client whose response was lost in transit and who retries
    still learns its attestation is in force. Without this test the two above
    would be satisfied by a repository that answered ``False`` to everything,
    which would turn every network retry into a 409.

    ``inserted`` is asserted alongside, and it is the half that differs: the
    second call is authoritative on a row it did *not* write. That is the state
    two concurrent identical requests reach, and the route has to tell it apart
    from a fresh write or it counts one stored attestation twice.
    """
    from zeroth.governance.attestations import RunAttestationRepository

    repository = RunAttestationRepository(sqlite_db)
    payload = make_payload()

    first = await repository.record(sign_attestation(payload, make_signer()))
    second = await repository.record(sign_attestation(payload, make_signer()))

    assert (first.authoritative, first.inserted) == (True, True)
    assert (second.authoritative, second.inserted) == (True, False)


async def test_an_unsigned_retry_of_an_unsigned_attestation_is_authoritative(
    sqlite_db: Any,
) -> None:
    """A ``NULL`` signature compares as absent, not as the string ``"None"``.

    The unsigned-to-unsigned retry is where a naive ``str(row[...])`` coercion
    would silently compare ``"None"`` against ``None`` and turn a legitimate
    retry into a conflict.
    """
    from zeroth.governance.attestations import RunAttestationRepository

    repository = RunAttestationRepository(sqlite_db)
    payload = make_payload()

    first = await repository.record(sign_attestation(payload, NullSigner()))
    second = await repository.record(sign_attestation(payload, NullSigner()))

    assert (first.authoritative, first.inserted) == (True, True)
    assert (second.authoritative, second.inserted) == (True, False)
