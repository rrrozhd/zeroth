"""Keyed provenance signing (WS-D): sign digests, verify by key id.

The digest layer stays unkeyed SHA-256 (tamper-evident); this package adds a
keyed signature *over* the digest (tamper-resistant). ``signable_bytes`` binds
the key id and algorithm into the signed material so they cannot be swapped
after signing. See ``docs/provenance-trust-model.md`` for what a valid signature
asserts per mode.
"""

from zeroth.platform.signing.canonical import signable_bytes
from zeroth.platform.signing.provider import (
    Ed25519Signer,
    EnvHmacSigner,
    NullSigner,
    SigningConfigError,
    SigningKeyProvider,
    build_signing_provider,
    build_signing_provider_async,
    sign_digest,
    verify_digest,
)

__all__ = [
    "Ed25519Signer",
    "EnvHmacSigner",
    "NullSigner",
    "SigningConfigError",
    "SigningKeyProvider",
    "build_signing_provider",
    "build_signing_provider_async",
    "sign_digest",
    "signable_bytes",
    "verify_digest",
]
