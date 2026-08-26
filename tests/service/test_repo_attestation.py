"""Checkout attestations: deterministic digest, keyed signature, dual-check verify."""

from __future__ import annotations

from datetime import UTC, datetime

from zeroth.integrations.github.models import RepositoryGrant
from zeroth.platform.signing import EnvHmacSigner
from zeroth.platform.storage.json import to_json_value
from zeroth.service.github.repository import SQLiteGitHubRepository
from zeroth.service.repositories.attestation import (
    CheckoutAttestationPayload,
    build_checkout_attestation,
    sign_checkout_attestation,
    verify_checkout_attestation,
)
from zeroth.service.repositories.repo_models import RepoCheckout
from zeroth.service.repositories.repository import SQLiteRepoCheckoutRepository

_ISSUED_AT = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def _payload(**overrides) -> CheckoutAttestationPayload:
    defaults = dict(
        tenant_id="tenant-a",
        workspace_id=None,
        checkout_id="checkout-1",
        installation_id=501,
        repository_id=9001,
        repository_full_name="acme/repo-one",
        requested_ref="main",
        commit_sha="a" * 40,
        git_tree_id="b" * 40,
        tree_digest="sha256:tree",
        config_digest="sha256:config",
        manifest_digest="sha256:manifest",
        script_name="ingest",
        issued_at=_ISSUED_AT,
    )
    defaults.update(overrides)
    return CheckoutAttestationPayload(**defaults)


def _signer() -> EnvHmacSigner:
    return EnvHmacSigner(key_id="k1", keys={"k1": b"checkout-key"})


def _flip_hex(value: str) -> str:
    head = "1" if value[0] == "0" else "0"
    return head + value[1:]


# -- digest ---------------------------------------------------------------------


def test_digest_is_deterministic_over_equal_payloads() -> None:
    assert build_checkout_attestation(_payload()) == build_checkout_attestation(_payload())


def test_digest_changes_when_any_identity_field_changes() -> None:
    baseline = build_checkout_attestation(_payload())
    assert build_checkout_attestation(_payload(commit_sha="c" * 40)) != baseline
    assert build_checkout_attestation(_payload(tenant_id="tenant-b")) != baseline
    assert build_checkout_attestation(_payload(checkout_id="checkout-2")) != baseline
    assert build_checkout_attestation(_payload(script_name=None)) != baseline
    assert build_checkout_attestation(_payload(workspace_id="workspace-a")) != baseline


# -- sign + verify --------------------------------------------------------------


def test_signed_attestation_round_trip_verifies() -> None:
    signer = _signer()
    payload = _payload()
    digest = build_checkout_attestation(payload)
    signature, key_id, algorithm = sign_checkout_attestation(digest, signer)
    assert signature
    assert key_id == "k1"
    assert algorithm == "HS256"

    digest_ok, signature_ok = verify_checkout_attestation(
        payload,
        digest=digest,
        signature=signature,
        key_id=key_id,
        algorithm=algorithm,
        signer=signer,
    )
    assert digest_ok is True
    assert signature_ok is True


def test_tampered_payload_trips_the_digest_axis_not_the_signature_axis() -> None:
    signer = _signer()
    digest = build_checkout_attestation(_payload())
    signature, key_id, algorithm = sign_checkout_attestation(digest, signer)

    # The persisted digest+signature are intact; the payload drifted.
    digest_ok, signature_ok = verify_checkout_attestation(
        _payload(commit_sha="c" * 40),
        digest=digest,
        signature=signature,
        key_id=key_id,
        algorithm=algorithm,
        signer=signer,
    )
    assert digest_ok is False
    assert signature_ok is True  # the old signature still covers the old digest


def test_flipped_signature_trips_the_signature_axis_not_the_digest_axis() -> None:
    signer = _signer()
    payload = _payload()
    digest = build_checkout_attestation(payload)
    signature, key_id, algorithm = sign_checkout_attestation(digest, signer)
    assert signature is not None

    digest_ok, signature_ok = verify_checkout_attestation(
        payload,
        digest=digest,
        signature=_flip_hex(signature),
        key_id=key_id,
        algorithm=algorithm,
        signer=signer,
    )
    assert digest_ok is True
    assert signature_ok is False


def test_unsigned_legacy_when_the_signer_is_absent() -> None:
    payload = _payload()
    digest = build_checkout_attestation(payload)
    assert sign_checkout_attestation(digest, None) == (None, None, None)

    digest_ok, signature_ok = verify_checkout_attestation(
        payload,
        digest=digest,
        signature=None,
        key_id=None,
        algorithm=None,
        signer=None,
    )
    assert digest_ok is True
    assert signature_ok is None  # unsigned-legacy: neither verified nor tampered


# -- persistence onto the checkout row ------------------------------------------


async def _seeded_checkout(sqlite_db) -> RepoCheckout:
    github = SQLiteGitHubRepository(sqlite_db)
    installation = await github.upsert_installation(
        "tenant-a",
        installation_id=501,
        account_login="acme",
        account_type="Organization",
        repository_selection="selected",
    )
    grant = await github.upsert_repository(
        "tenant-a",
        installation_pk=installation.id,
        grant=RepositoryGrant(
            repo_id=9001,
            owner="acme",
            name="repo-one",
            full_name="acme/repo-one",
            private=True,
            default_branch="main",
        ),
    )
    return await SQLiteRepoCheckoutRepository(sqlite_db).create(
        RepoCheckout(
            tenant_id="tenant-a",
            repository_pk=grant.id,
            installation_id=501,
            repository_id=9001,
            repository_full_name="acme/repo-one",
            requested_ref="main",
        )
    )


async def test_record_attestation_persists_and_hydrates_the_envelope(sqlite_db) -> None:
    repo = SQLiteRepoCheckoutRepository(sqlite_db)
    checkout = await _seeded_checkout(sqlite_db)
    signer = _signer()
    payload = _payload(checkout_id=checkout.id)
    digest = build_checkout_attestation(payload)
    signature, key_id, algorithm = sign_checkout_attestation(digest, signer)
    payload_json = to_json_value(payload.model_dump(mode="json"))

    assert await repo.record_attestation(
        "tenant-a",
        checkout.id,
        digest=digest,
        signature=signature,
        key_id=key_id,
        algorithm=algorithm,
        payload_json=payload_json,
    )
    reloaded = await repo.get("tenant-a", checkout.id)
    assert reloaded is not None
    assert reloaded.attestation_digest == digest
    assert reloaded.attestation_signature == signature
    assert reloaded.attestation_key_id == "k1"
    assert reloaded.attestation_algorithm == "HS256"
    assert reloaded.attestation_payload_json == payload_json

    # The persisted envelope verifies on both axes after hydration.
    hydrated = CheckoutAttestationPayload.model_validate_json(
        reloaded.attestation_payload_json
    )
    digest_ok, signature_ok = verify_checkout_attestation(
        hydrated,
        digest=reloaded.attestation_digest,
        signature=reloaded.attestation_signature,
        key_id=reloaded.attestation_key_id,
        algorithm=reloaded.attestation_algorithm,
        signer=signer,
    )
    assert digest_ok is True
    assert signature_ok is True
    assert not await repo.record_attestation(
        "tenant-b",
        checkout.id,
        digest=digest,
        signature=signature,
        key_id=key_id,
        algorithm=algorithm,
        payload_json=payload_json,
    )


async def test_unsigned_legacy_envelope_persists_as_all_none(sqlite_db) -> None:
    repo = SQLiteRepoCheckoutRepository(sqlite_db)
    checkout = await _seeded_checkout(sqlite_db)
    payload = _payload(checkout_id=checkout.id)
    digest = build_checkout_attestation(payload)
    signature, key_id, algorithm = sign_checkout_attestation(digest, None)

    assert await repo.record_attestation(
        "tenant-a",
        checkout.id,
        digest=digest,
        signature=signature,
        key_id=key_id,
        algorithm=algorithm,
        payload_json=to_json_value(payload.model_dump(mode="json")),
    )
    reloaded = await repo.get("tenant-a", checkout.id)
    assert reloaded is not None
    assert reloaded.attestation_digest == digest
    assert reloaded.attestation_signature is None
    assert reloaded.attestation_key_id is None
    assert reloaded.attestation_algorithm is None

    digest_ok, signature_ok = verify_checkout_attestation(
        payload,
        digest=reloaded.attestation_digest,
        signature=reloaded.attestation_signature,
        key_id=reloaded.attestation_key_id,
        algorithm=reloaded.attestation_algorithm,
        signer=None,
    )
    assert digest_ok is True
    assert signature_ok is None
