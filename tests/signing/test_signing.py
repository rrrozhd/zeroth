"""WS-D signing primitives: roundtrip, adversarial, honest-bound, rotation."""

from __future__ import annotations

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from zeroth.platform.config.settings import ProvenanceSigningSettings
from zeroth.core.secrets import EnvSecretProvider
from zeroth.core.signing import (
    Ed25519Signer,
    EnvHmacSigner,
    NullSigner,
    build_signing_provider,
    sign_digest,
    signable_bytes,
    verify_digest,
)

DIGEST = "a" * 64


def _ed25519_keypair() -> tuple[str, str]:
    private = Ed25519PrivateKey.generate()
    return (
        private.private_bytes_raw().hex(),
        private.public_key().public_bytes_raw().hex(),
    )


def test_env_hmac_roundtrip_via_shared_secret_provider() -> None:
    provider = EnvSecretProvider({"SIGNING_DEPLOYMENT": "shared-hmac-key"})
    signer = build_signing_provider(
        ProvenanceSigningSettings(mode="env", signing_key_id="k1"), provider
    )
    assert isinstance(signer, EnvHmacSigner)
    assert signer.algorithm() == "HS256"

    signature, key_id, algorithm = sign_digest(DIGEST, signer)
    assert signature and key_id == "k1" and algorithm == "HS256"
    assert verify_digest(DIGEST, signature, key_id, algorithm, signer) is True


def test_ed25519_verify_with_public_key_only() -> None:
    private_hex, public_hex = _ed25519_keypair()
    signer = Ed25519Signer.from_raw(
        key_id="ed1", public_keys_hex={"ed1": public_hex}, private_key_hex=private_hex
    )
    signature, key_id, algorithm = sign_digest(DIGEST, signer)
    assert algorithm == "Ed25519"

    # A verifier holding ONLY the public key can still verify.
    verify_only = Ed25519Signer.from_raw(key_id="ed1", public_keys_hex={"ed1": public_hex})
    assert verify_digest(DIGEST, signature, key_id, algorithm, verify_only) is True


def test_adversarial_key_id_swap_is_rejected() -> None:
    """key_id is inside the signed bytes: swapping it must not re-verify."""
    signer = EnvHmacSigner(key_id="k1", keys={"k1": b"secret"})
    signature, _, algorithm = sign_digest(DIGEST, signer)

    # Verifying the SAME signature under an attacker-chosen key_id fails, because
    # signable_bytes(digest, "attacker", alg) != the bytes that were signed.
    assert verify_digest(DIGEST, signature, "attacker-key", algorithm, signer) is False
    assert signable_bytes(DIGEST, "k1", algorithm) != signable_bytes(
        DIGEST, "attacker-key", algorithm
    )


def test_signature_bytes_bound_to_digest() -> None:
    signer = EnvHmacSigner(key_id="k1", keys={"k1": b"secret"})
    signature, key_id, algorithm = sign_digest(DIGEST, signer)
    # A different digest under the same key does not validate the old signature.
    assert verify_digest("b" * 64, signature, key_id, algorithm, signer) is False


def test_honest_bound_env_hmac_forgery_succeeds_for_key_holder() -> None:
    """The HMAC trust model is key-custody-bounded, and the test says so.

    A party who holds the shared key CAN forge an accepted signature — that is
    the honest limitation env-HMAC carries (NOT non-repudiation).
    """
    issuer = EnvHmacSigner(key_id="k1", keys={"k1": b"the-shared-key"})
    _, key_id, algorithm = sign_digest(DIGEST, issuer)

    forger = EnvHmacSigner(key_id="k1", keys={"k1": b"the-shared-key"})
    forged_signature, _, _ = sign_digest(DIGEST, forger)
    assert verify_digest(DIGEST, forged_signature, key_id, algorithm, issuer) is True


def test_honest_bound_ed25519_public_only_attacker_cannot_forge() -> None:
    """The asymmetric path resists forgery by a public-key-only attacker."""
    private_hex, public_hex = _ed25519_keypair()
    signer = Ed25519Signer.from_raw(
        key_id="ed1", public_keys_hex={"ed1": public_hex}, private_key_hex=private_hex
    )
    _, key_id, algorithm = sign_digest(DIGEST, signer)

    # Attacker has only the public key -> cannot produce a valid signature.
    attacker = Ed25519Signer.from_raw(key_id="ed1", public_keys_hex={"ed1": public_hex})
    forged = b"\x00" * 64
    assert verify_digest(DIGEST, forged.hex(), key_id, algorithm, attacker) is False
    try:
        attacker.sign(signable_bytes(DIGEST, key_id, algorithm))
        raise AssertionError("public-key-only signer must not be able to sign")
    except Exception as exc:  # SigningConfigError
        assert "private key" in str(exc)


def test_key_rotation_verifies_after_retire() -> None:
    """A retired key stays verify-only; the active key signs new material."""
    old = EnvHmacSigner(key_id="k1", keys={"k1": b"old-key"})
    old_signature, _, algorithm = sign_digest(DIGEST, old)

    # Rotate: active key is k2, but k1 is kept in the key set for verification.
    rotated = EnvHmacSigner(key_id="k2", keys={"k1": b"old-key", "k2": b"new-key"})
    assert rotated.key_id() == "k2"
    # Old material still verifies under its own key_id after the rotation.
    assert verify_digest(DIGEST, old_signature, "k1", algorithm, rotated) is True
    # New signing uses the active key.
    new_signature, new_key_id, _ = sign_digest(DIGEST, rotated)
    assert new_key_id == "k2"
    assert verify_digest(DIGEST, new_signature, "k2", algorithm, rotated) is True


def test_null_signer_fails_closed() -> None:
    """Misconfig fails CLOSED: no signature emitted, verify always False."""
    signer = NullSigner()
    signature, key_id, algorithm = sign_digest(DIGEST, signer)
    # NullSigner yields no signature -> the caller persists unsigned-legacy.
    assert (signature, key_id, algorithm) == (None, None, None)
    # And it can never greenlight a signature.
    assert signer.verify(b"msg", b"sig", "unsigned") is False


def test_none_signer_is_unsigned_legacy() -> None:
    assert sign_digest(DIGEST, None) == (None, None, None)
    assert verify_digest(DIGEST, "deadbeef", "k1", "HS256", None) is False


def test_build_env_mode_without_key_returns_none() -> None:
    """Dev 'env' with no key -> None (bootstrap warns, stays unsigned-legacy)."""
    signer = build_signing_provider(
        ProvenanceSigningSettings(mode="env"), EnvSecretProvider({})
    )
    assert signer is None


def test_build_off_mode_returns_null_signer() -> None:
    signer = build_signing_provider(
        ProvenanceSigningSettings(mode="off"), EnvSecretProvider({})
    )
    assert isinstance(signer, NullSigner)


def test_build_kms_mode_without_key_fails_closed() -> None:
    from zeroth.core.signing import SigningConfigError

    try:
        build_signing_provider(
            ProvenanceSigningSettings(mode="kms"), EnvSecretProvider({})
        )
        raise AssertionError("kms mode without a key must raise")
    except SigningConfigError as exc:
        assert "kms" in str(exc)


def test_build_kms_mode_end_to_end() -> None:
    private_hex, public_hex = _ed25519_keypair()
    import json

    provider = EnvSecretProvider({"SIGNING_DEPLOYMENT": private_hex})
    settings = ProvenanceSigningSettings(
        mode="kms",
        signing_key_id="ed1",
        public_keys_json=json.dumps({"ed1": public_hex}),
    )
    signer = build_signing_provider(settings, provider)
    assert isinstance(signer, Ed25519Signer)
    signature, key_id, algorithm = sign_digest(DIGEST, signer)
    assert verify_digest(DIGEST, signature, key_id, algorithm, signer) is True


class _AsyncOnlyKeyProvider:
    """Fails loudly if signing key material is resolved synchronously."""

    def resolve(self, secret_ref, *, tenant_id=None):  # noqa: ANN001
        raise AssertionError("sync resolve used on an async path")

    def resolve_many(self, refs, *, tenant_id=None):  # noqa: ANN001
        raise AssertionError("sync resolve_many used on an async path")

    def resolve_secret(self, logical_name, *, tenant_id=None, deployment_ref=None):  # noqa: ANN001
        raise AssertionError("sync resolve_secret used on an async path")

    async def resolve_secret_async(
        self, logical_name, *, tenant_id=None, deployment_ref=None
    ):  # noqa: ANN001
        return "shared-hmac-key"


import pytest  # noqa: E402


@pytest.mark.asyncio
async def test_build_signing_provider_async_resolves_without_sync_call() -> None:
    from zeroth.core.signing import build_signing_provider_async

    signer = await build_signing_provider_async(
        ProvenanceSigningSettings(mode="env", signing_key_id="k1"),
        _AsyncOnlyKeyProvider(),
    )
    assert isinstance(signer, EnvHmacSigner)
    signature = signer.sign(signable_bytes(DIGEST, "k1", "hmac-sha256"))
    assert signer.verify(signable_bytes(DIGEST, "k1", "hmac-sha256"), signature, "k1")
