"""Legacy import path for :mod:`zeroth.platform.signing.provider`."""

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
    "verify_digest",
]
