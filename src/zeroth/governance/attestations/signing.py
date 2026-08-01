"""Digest, sign and verify run-start attestations.

Two layers stack here. The unkeyed SHA-256 digest over the canonical JSON of the
payload is tamper-*evident* only -- an attacker who edits a field can recompute
it. The keyed layer underneath (``zeroth.platform.signing``) signs
``signable_bytes(digest, key_id, algorithm)``, which folds the key id and
algorithm into the signed material and closes downgrade/substitution. This
module sits on top of that and must not undo it: it never signs a bare payload
and never verifies a digest without also re-deriving it from the payload.
"""

from __future__ import annotations

import hashlib

from zeroth.governance.attestations.payload import (
    RunAttestationPayload,
    SignedRunAttestation,
    stable_claims,
)
from zeroth.platform.signing import SigningKeyProvider, sign_digest, verify_digest
from zeroth.platform.storage.json import to_json_value


def attestation_digest(payload: RunAttestationPayload) -> str:
    """Return the SHA-256 hex digest over the canonical JSON of ``payload``.

    The model itself is handed to ``to_json_value`` (sorted keys, no
    whitespace) so *every* declared field is covered by construction and stays
    covered when fields are added later.

    Args:
        payload: The attestation claims to hash.

    Returns:
        Lowercase hex SHA-256 digest of the canonical payload encoding.
    """
    return hashlib.sha256(to_json_value(payload).encode("utf-8")).hexdigest()


def sign_attestation(
    payload: RunAttestationPayload,
    signer: SigningKeyProvider | None,
) -> SignedRunAttestation:
    """Digest ``payload`` and sign the digest with ``signer``.

    A missing signer, or a signer that yields no signature (``NullSigner``),
    produces an unsigned-legacy record: the digest is still recorded, but the
    signature triple stays ``None`` so the result can never read as verified.

    Args:
        payload: The attestation claims to bind.
        signer: Provider used to sign the digest, or ``None`` for unsigned.

    Returns:
        The payload wrapped with its digest and, when available, its signature.
    """
    digest = attestation_digest(payload)
    signature, key_id, algorithm = sign_digest(digest, signer)
    return SignedRunAttestation(
        payload=payload,
        digest=digest,
        signature=signature,
        signing_key_id=key_id,
        signing_algorithm=algorithm,
    )


def is_identical_resubmission(
    submitted: SignedRunAttestation,
    stored: SignedRunAttestation | None,
    signer: SigningKeyProvider | None,
) -> bool:
    """Return True iff ``submitted`` is a retry of the attestation ``stored``.

    First-write-wins makes a second attestation for one run lose, and that is
    correct for a *different* attestation. It is wrong for the same one: an
    adapter whose response was lost has no way to resubmit the bytes it sent,
    because the server stamps a fresh ``issued_at`` and ``expires_at`` on
    arrival. Comparing envelopes alone therefore tells an honest retry that
    some other attestation holds its run, which is false.

    Two independent checks must both pass, and neither is sufficient alone:

    * :func:`~zeroth.governance.attestations.payload.stable_claims` equality
      proves the submitted claims -- client-supplied and server-derived alike
      -- are the stored claims, differing only in the issuance window.
    * Re-signing the *stored* payload and requiring the whole envelope to come
      back identical proves the stored row is intact (its digest still derives
      from its payload) and is exactly what the signer holding the key *now*
      would produce. A rotated key, a retired key, a signed row under an
      unsigned deployment, or an unsigned row under a signed one all fail here,
      so none of them is mistaken for a retry.

    Fail-closed on every other axis: no stored row, a signer that raises, or a
    payload that will not project all answer False, which leaves the caller on
    its conflict path. False costs an honest client a 409 it could have been
    spared; True on the wrong pair would tell a client its claims govern a run
    they do not.

    Args:
        submitted: The attestation just built for this request.
        stored: The attestation already in force for the run, or ``None``.
        signer: Provider holding the key material, or ``None`` when the
            deployment signs nothing.

    Returns:
        True only when ``submitted`` restates ``stored`` under the current
        signer. Never raises.
    """
    try:
        if stored is None:
            return False
        if stable_claims(submitted.payload) != stable_claims(stored.payload):
            return False
        return sign_attestation(stored.payload, signer) == stored
    except Exception:  # noqa: BLE001 - trust boundary: any failure is "not a retry"
        return False


def verify_attestation(
    signed: SignedRunAttestation,
    signer: SigningKeyProvider | None,
) -> bool:
    """Return True iff ``signed`` is intact and validly signed under ``signer``.

    Fail-closed on both axes. The digest is recomputed from the payload and must
    match the stored one, so a payload edited after signing fails even when the
    stored digest and signature are left untouched; the keyed check then runs
    over the recomputed digest with the stored key id and algorithm, so an
    attacker who *does* recompute the digest still fails, and rewriting
    ``signing_key_id`` or ``signing_algorithm`` invalidates the signature
    because both are folded into the signed bytes.

    Freshness is deliberately **not** checked here: ``issued_at`` / ``expires_at``
    are bound into the signature, but enforcing a TTL against a clock belongs to
    the verifying provider, together with the tenant, deployment and correlation
    match. A ``True`` here means "these exact bytes were signed by this key", not
    "this attestation is currently acceptable".

    Args:
        signed: The signed attestation to check.
        signer: Provider holding the key material, or ``None`` (always False).

    Returns:
        True only for an intact, validly signed attestation; False for anything
        unsigned, tampered with, or malformed. Never raises.
    """
    try:
        if not isinstance(signed, SignedRunAttestation) or signer is None:
            return False
        recomputed = attestation_digest(signed.payload)
        if recomputed != signed.digest:
            return False
        return verify_digest(
            recomputed,
            signed.signature,
            signed.signing_key_id,
            signed.signing_algorithm,
            signer,
        )
    except Exception:  # noqa: BLE001 - trust boundary: any failure is a verification failure
        return False
