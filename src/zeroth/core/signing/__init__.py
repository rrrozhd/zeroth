"""Legacy import path for the platform signing package.

Keyed provenance signing lives in :mod:`zeroth.platform.signing`; this package
republishes the same objects for compatibility. Import from the canonical
location instead (see docs/backend-import-migration.md).
"""

from zeroth.platform.signing import (
    Ed25519Signer,
    EnvHmacSigner,
    NullSigner,
    SigningConfigError,
    SigningKeyProvider,
    build_signing_provider,
    build_signing_provider_async,
    sign_digest,
    signable_bytes,
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
