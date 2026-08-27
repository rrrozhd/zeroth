"""Digest, signature, and verification for checkout attestations (ZER-37).

Copies the deployment provenance triple exactly
(:mod:`zeroth.service.deployments.provenance`): a canonical-JSON SHA-256
**digest** recomputable from the payload (tamper-evident), and a keyed
**signature** over that digest through the platform signing provider
(tamper-resistant). Signing with no signer -- or a :class:`NullSigner` --
returns all-``None`` so callers persist an unsigned-legacy row rather than a
signed-but-invalid one; verification reports the signature axis as ``None``
for those rows, mirroring the deployment three-state semantics.

The payload embeds *digests* (tree, config, manifest), never the documents
they were computed over.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from zeroth.platform.signing import sign_digest, verify_digest
from zeroth.platform.storage.json import to_json_value

if TYPE_CHECKING:
    from zeroth.platform.signing import SigningKeyProvider

_SCHEMA_VERSION = 1


class CheckoutAttestationPayload(BaseModel):
    """The identities one staged checkout is attested under.

    Frozen and ``extra="forbid"``: the signed material is exactly these
    fields, so an unrecognised key can never ride along unsigned and a field
    cannot be mutated after the digest is taken (the run-attestation payload
    precedent in :mod:`zeroth.governance.attestations.payload`).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = _SCHEMA_VERSION
    tenant_id: str
    workspace_id: str | None
    checkout_id: str
    installation_id: int
    repository_id: int
    repository_full_name: str
    requested_ref: str
    commit_sha: str
    git_tree_id: str
    tree_digest: str
    config_digest: str | None
    manifest_digest: str | None
    script_name: str | None
    issued_at: datetime


def build_checkout_attestation(payload: CheckoutAttestationPayload) -> str:
    """Hash the attestation payload's canonical JSON form -> hex digest."""
    canonical = to_json_value(payload.model_dump(mode="json"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def sign_checkout_attestation(
    digest: str, signer: SigningKeyProvider | None
) -> tuple[str | None, str | None, str | None]:
    """Sign a checkout attestation digest -> ``(signature_hex, key_id, algorithm)``.

    Pure: it does not touch the checkout row. Returns all-``None`` when there
    is no signer or signing is disabled, so the caller persists an
    unsigned-legacy row rather than a signed-but-invalid one.
    """
    return sign_digest(digest, signer)


def verify_checkout_attestation(
    payload: CheckoutAttestationPayload,
    *,
    digest: str,
    signature: str | None,
    key_id: str | None,
    algorithm: str | None,
    signer: SigningKeyProvider | None,
) -> tuple[bool, bool | None]:
    """Dual-check a checkout attestation: digest recompute AND keyed signature.

    Returns ``(digest_ok, signature_ok)`` where ``signature_ok`` is
    three-state per the deployment semantics: ``None`` when the persisted row
    is unsigned-legacy (no key id), ``True``/``False`` for a signed row. The
    signature is verified over the *persisted* digest -- what was signed at
    staging time -- so a payload tamper trips the digest axis even while an
    untouched signature still validates, and a signature-byte flip trips the
    signature axis while the digest recompute still matches.
    """
    digest_ok = build_checkout_attestation(payload) == digest
    if key_id is None:
        return digest_ok, None
    return digest_ok, verify_digest(digest, signature, key_id, algorithm, signer)


__all__ = [
    "CheckoutAttestationPayload",
    "build_checkout_attestation",
    "sign_checkout_attestation",
    "verify_checkout_attestation",
]
