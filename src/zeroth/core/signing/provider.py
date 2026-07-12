"""Signing-key providers for tamper-*resistant* provenance (WS-D).

Unkeyed SHA-256 digests are tamper-*evident*: they catch accidental corruption
but not a malicious writer who recomputes the digest after editing. WS-D adds a
keyed signature over the digest so a forgery requires the signing key, not just
the hashing algorithm.

Trust model (see ``docs/provenance-trust-model.md``):

* :class:`EnvHmacSigner` — symmetric HMAC-SHA256. A valid signature asserts only
  that *a holder of the shared key* produced it. It is **key-custody-bounded**,
  NOT PKI / non-repudiation: anyone who can read the key can forge.
* :class:`Ed25519Signer` — asymmetric. Verification needs only the public key,
  so a verifier who never holds the private key cannot forge. This is the path
  that backs the strong "signed & verifiable" claim; pairing it with an external
  KMS that signs without exposing the private key is the non-repudiation story.
* :class:`NullSigner` — fail-closed placeholder. ``sign`` yields no signature
  (records stay unsigned-legacy) and ``verify`` always returns ``False``, so a
  misconfiguration can never mint a row that reads as verified.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from zeroth.core.signing.canonical import signable_bytes

if TYPE_CHECKING:
    from zeroth.core.config.settings import ProvenanceSigningSettings
    from zeroth.core.secrets import SecretProvider


class SigningConfigError(RuntimeError):
    """Provenance-signing configuration is incomplete or invalid.

    Raised at bootstrap for the *strong* signing path (``mode='kms'``) when the
    key material cannot be resolved — fail-closed at startup, mirroring how the
    Vault secret backend refuses to start on incomplete config.
    """


@runtime_checkable
class SigningKeyProvider(Protocol):
    """Interface for signing a message and verifying a signature by key id.

    ``verify`` takes the ``key_id`` the signature claims so a provider that
    retains retired keys (verify-only, for rotation) can select the right key.
    """

    def key_id(self) -> str:  # pragma: no cover - protocol
        """Return the id of the key this provider signs with."""

    def algorithm(self) -> str:  # pragma: no cover - protocol
        """Return the signature algorithm token ("HS256" | "Ed25519")."""

    def sign(self, message: bytes) -> bytes:  # pragma: no cover - protocol
        """Sign ``message``; return empty bytes when signing is disabled."""

    def verify(
        self, message: bytes, signature: bytes, key_id: str
    ) -> bool:  # pragma: no cover - protocol
        """Return True iff ``signature`` is valid for ``message`` under ``key_id``."""


class NullSigner:
    """Signer that never signs and never verifies (fail-closed default).

    Used for ``mode='off'`` and as a safety net: ``sign`` returns empty bytes so
    the signing wrapper leaves the record unsigned-legacy, and ``verify`` is
    unconditionally ``False`` so nothing signed under this provider can read as
    verified.
    """

    _KEY_ID = "unsigned"
    _ALGORITHM = "none"

    def key_id(self) -> str:
        return self._KEY_ID

    def algorithm(self) -> str:
        return self._ALGORITHM

    def sign(self, message: bytes) -> bytes:
        return b""

    def verify(self, message: bytes, signature: bytes, key_id: str) -> bool:
        return False


class EnvHmacSigner:
    """Symmetric HMAC-SHA256 signer keyed from the shared secret provider.

    Holds a ``key_id -> raw-key-bytes`` map. The active ``key_id`` is used for
    signing; ``verify`` selects the key named by the signature's ``key_id`` so a
    retired key can stay verify-only across a rotation. Key-custody-bounded: a
    holder of the key can forge, so this is NOT non-repudiation.
    """

    ALGORITHM = "HS256"

    def __init__(self, *, key_id: str, keys: Mapping[str, bytes]) -> None:
        if key_id not in keys:
            raise SigningConfigError(f"active signing key_id {key_id!r} missing from provided keys")
        self._key_id = key_id
        self._keys = dict(keys)

    def key_id(self) -> str:
        return self._key_id

    def algorithm(self) -> str:
        return self.ALGORITHM

    def sign(self, message: bytes) -> bytes:
        return hmac.new(self._keys[self._key_id], message, hashlib.sha256).digest()

    def verify(self, message: bytes, signature: bytes, key_id: str) -> bool:
        key = self._keys.get(key_id)
        if key is None:
            return False
        expected = hmac.new(key, message, hashlib.sha256).digest()
        return hmac.compare_digest(expected, signature)


class Ed25519Signer:
    """Asymmetric Ed25519 signer (``cryptography``, no new dependency).

    ``private_key`` is optional: a verify-only provider (public keys only, e.g. a
    third-party auditor) can still check signatures but cannot forge, which is
    the property that separates this from the HMAC path. ``public_keys`` maps
    every acceptable ``key_id`` to its public key, so rotation keeps retired keys
    verifiable.
    """

    ALGORITHM = "Ed25519"

    def __init__(
        self,
        *,
        key_id: str,
        public_keys: Mapping[str, object],
        private_key: object | None = None,
    ) -> None:
        if key_id not in public_keys:
            raise SigningConfigError(f"active signing key_id {key_id!r} missing from public keys")
        self._key_id = key_id
        self._public_keys = dict(public_keys)
        self._private_key = private_key

    @classmethod
    def from_raw(
        cls,
        *,
        key_id: str,
        public_keys_hex: Mapping[str, str],
        private_key_hex: str | None = None,
    ) -> Ed25519Signer:
        """Build from hex-encoded raw 32-byte Ed25519 key material."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )

        public_keys = {
            kid: Ed25519PublicKey.from_public_bytes(bytes.fromhex(value))
            for kid, value in public_keys_hex.items()
        }
        private_key = (
            Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
            if private_key_hex
            else None
        )
        return cls(key_id=key_id, public_keys=public_keys, private_key=private_key)

    def key_id(self) -> str:
        return self._key_id

    def algorithm(self) -> str:
        return self.ALGORITHM

    def sign(self, message: bytes) -> bytes:
        if self._private_key is None:
            raise SigningConfigError("Ed25519Signer has no private key (verify-only); cannot sign")
        return self._private_key.sign(message)

    def verify(self, message: bytes, signature: bytes, key_id: str) -> bool:
        from cryptography.exceptions import InvalidSignature

        public_key = self._public_keys.get(key_id)
        if public_key is None:
            return False
        try:
            public_key.verify(signature, message)
        except InvalidSignature:
            return False
        return True


def sign_digest(
    digest: str, signer: SigningKeyProvider | None
) -> tuple[str | None, str | None, str | None]:
    """Sign ``digest`` and return ``(signature_hex, key_id, algorithm)``.

    Returns ``(None, None, None)`` when there is no signer or the signer yields
    no signature (:class:`NullSigner`), which the callers persist as
    unsigned-legacy — never as a signed-but-invalid row.
    """
    if signer is None:
        return None, None, None
    key_id = signer.key_id()
    algorithm = signer.algorithm()
    signature = signer.sign(signable_bytes(digest, key_id, algorithm))
    if not signature:
        return None, None, None
    return signature.hex(), key_id, algorithm


def verify_digest(
    digest: str,
    signature_hex: str | None,
    key_id: str | None,
    algorithm: str | None,
    signer: SigningKeyProvider | None,
) -> bool:
    """Return True iff ``signature_hex`` is a valid signature over ``digest``.

    A missing signer/signature/metadata yields ``False`` (fail-closed). Callers
    distinguish *unsigned-legacy* (no ``key_id`` persisted) from *signed-invalid*
    before mapping this bool onto the three-state result.
    """
    if signer is None or not signature_hex or not key_id or not algorithm:
        return False
    try:
        signature = bytes.fromhex(signature_hex)
    except ValueError:
        return False
    return signer.verify(signable_bytes(digest, key_id, algorithm), signature, key_id)


def build_signing_provider(
    settings: ProvenanceSigningSettings,
    secret_provider: SecretProvider,
    *,
    tenant_id: str | None = None,
) -> SigningKeyProvider | None:
    """Select the signer from ``settings.mode`` using the shared secret provider.

    * ``off`` — :class:`NullSigner` (explicit opt-out; rows stay unsigned-legacy).
    * ``env`` — :class:`EnvHmacSigner` when the key resolves; ``None`` (caller
      warns, rows stay unsigned-legacy) when it does not. Dev boxes with no key
      configured keep working without minting misleadingly-signed rows.
    * ``kms`` — :class:`Ed25519Signer`; raises :class:`SigningConfigError` when
      the private key cannot be resolved (fail-closed for the strong path).
    """
    mode = (settings.mode or "env").lower()
    logical_name = settings.signing_key_ref or "signing.deployment"

    if mode == "off":
        return NullSigner()

    if mode == "env":
        key = secret_provider.resolve_secret(logical_name, tenant_id=tenant_id)
        if not key:
            return None
        return EnvHmacSigner(
            key_id=settings.signing_key_id,
            keys={settings.signing_key_id: key.encode("utf-8")},
        )

    if mode == "kms":
        private_key_hex = secret_provider.resolve_secret(logical_name, tenant_id=tenant_id)
        if not private_key_hex:
            raise SigningConfigError(
                "provenance.mode='kms' requires a resolvable signing key "
                f"({logical_name!r}) but the secret provider returned nothing"
            )
        public_keys_hex = _parse_public_keys(settings)
        if settings.signing_key_id not in public_keys_hex:
            raise SigningConfigError(
                "provenance.mode='kms' requires provenance.public_keys_json to "
                f"contain the active signing_key_id {settings.signing_key_id!r}"
            )
        return Ed25519Signer.from_raw(
            key_id=settings.signing_key_id,
            public_keys_hex=public_keys_hex,
            private_key_hex=private_key_hex,
        )

    raise SigningConfigError(
        f"unknown provenance.mode {settings.mode!r} (expected 'env', 'kms', or 'off')"
    )


def _parse_public_keys(settings: ProvenanceSigningSettings) -> dict[str, str]:
    raw = settings.public_keys_json
    if raw is None:
        return {}
    value = raw.get_secret_value() if hasattr(raw, "get_secret_value") else str(raw)
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise SigningConfigError("provenance.public_keys_json is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise SigningConfigError(
            "provenance.public_keys_json must be a JSON object of key_id -> public-key-hex"
        )
    return {str(k): str(v) for k, v in parsed.items()}
