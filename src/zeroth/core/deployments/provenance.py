"""Digest and attestation helpers for deployment snapshots.

Two independent axes protect a deployment attestation:

* the **digest** (:func:`verify_attestation`) — recomputed from the live
  snapshot, so any drift between the persisted digest and the current bytes is
  caught (tamper-evident);
* the **signature** (:func:`verify_attestation_signature`) — a keyed signature
  over the digest, so a malicious writer who recomputes the digest still cannot
  forge acceptance without the key (tamper-resistant).

:func:`verify_attestation_full` runs both; neither masks the other.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from zeroth.core.signing import sign_digest, verify_digest
from zeroth.platform.storage.json import to_json_value

if TYPE_CHECKING:
    from zeroth.core.signing import SigningKeyProvider


def compute_graph_snapshot_digest(serialized_graph: str) -> str:
    """Hash the serialized graph snapshot."""
    return hashlib.sha256(serialized_graph.encode("utf-8")).hexdigest()


def compute_contract_snapshot_digest(
    *,
    entry_input_contract_ref: str | None,
    entry_input_contract_version: int | None,
    entry_output_contract_ref: str | None,
    entry_output_contract_version: int | None,
) -> str:
    """Hash the pinned input/output contract snapshot."""
    return _digest_json(
        {
            "entry_input_contract_ref": entry_input_contract_ref,
            "entry_input_contract_version": entry_input_contract_version,
            "entry_output_contract_ref": entry_output_contract_ref,
            "entry_output_contract_version": entry_output_contract_version,
        }
    )


def compute_settings_snapshot_digest(settings_snapshot: dict[str, object]) -> str:
    """Hash the persisted deployment settings snapshot."""
    return _digest_json(settings_snapshot)


def build_attestation_payload(deployment: object) -> dict[str, object]:
    """Build a stable attestation payload from a deployment snapshot."""
    payload = {
        "deployment_ref": deployment.deployment_ref,
        "deployment_version": deployment.version,
        "graph_id": deployment.graph_id,
        "graph_version": deployment.graph_version,
        "graph_version_ref": deployment.graph_version_ref,
        "entry_input_contract_ref": deployment.entry_input_contract_ref,
        "entry_input_contract_version": deployment.entry_input_contract_version,
        "entry_output_contract_ref": deployment.entry_output_contract_ref,
        "entry_output_contract_version": deployment.entry_output_contract_version,
        "graph_snapshot_digest": deployment.graph_snapshot_digest,
        "contract_snapshot_digest": deployment.contract_snapshot_digest,
        "settings_snapshot_digest": deployment.settings_snapshot_digest,
        "created_at": deployment.created_at.isoformat(),
    }
    return {
        **payload,
        "attestation_digest": compute_attestation_digest(payload),
    }


def compute_attestation_digest(payload: dict[str, object]) -> str:
    """Hash an attestation payload."""
    return _digest_json(payload)


def verify_attestation(deployment: object, attestation: dict[str, object]) -> list[str]:
    """Compare an attestation payload with the current persisted deployment snapshot."""
    mismatches: list[str] = []
    current = {
        "deployment_ref": deployment.deployment_ref,
        "deployment_version": deployment.version,
        "graph_id": deployment.graph_id,
        "graph_version": deployment.graph_version,
        "graph_version_ref": deployment.graph_version_ref,
        "entry_input_contract_ref": deployment.entry_input_contract_ref,
        "entry_input_contract_version": deployment.entry_input_contract_version,
        "entry_output_contract_ref": deployment.entry_output_contract_ref,
        "entry_output_contract_version": deployment.entry_output_contract_version,
        "graph_snapshot_digest": compute_graph_snapshot_digest(deployment.serialized_graph),
        "contract_snapshot_digest": compute_contract_snapshot_digest(
            entry_input_contract_ref=deployment.entry_input_contract_ref,
            entry_input_contract_version=deployment.entry_input_contract_version,
            entry_output_contract_ref=deployment.entry_output_contract_ref,
            entry_output_contract_version=deployment.entry_output_contract_version,
        ),
        "settings_snapshot_digest": compute_settings_snapshot_digest(
            dict(deployment.deployment_settings_snapshot)
        ),
        "created_at": deployment.created_at.isoformat(),
    }
    current["attestation_digest"] = compute_attestation_digest(current)
    for field in (
        "deployment_ref",
        "deployment_version",
        "graph_version_ref",
        "graph_snapshot_digest",
        "contract_snapshot_digest",
        "settings_snapshot_digest",
        "attestation_digest",
    ):
        if attestation.get(field) != current.get(field):
            mismatches.append(field)
    return mismatches


def sign_attestation(
    digest: str, signer: SigningKeyProvider | None
) -> tuple[str | None, str | None, str | None]:
    """Sign an attestation digest → ``(signature_hex, key_id, algorithm)``.

    Pure: it does not touch the deployment payload. Returns all-``None`` when
    there is no signer or signing is disabled, so the caller persists an
    unsigned-legacy row rather than a signed-but-invalid one.
    """
    return sign_digest(digest, signer)


def verify_attestation_signature(
    digest: str,
    signature: str | None,
    key_id: str | None,
    algorithm: str | None,
    signer: SigningKeyProvider | None,
) -> bool:
    """Return True iff ``signature`` is a valid keyed signature over ``digest``."""
    return verify_digest(digest, signature, key_id, algorithm, signer)


def verify_attestation_full(
    deployment: object,
    attestation: dict[str, object],
    signer: SigningKeyProvider | None,
) -> tuple[list[str], bool | None]:
    """Dual-check an attestation: digest recompute AND persisted signature.

    Returns ``(digest_mismatches, signature_ok)`` where ``signature_ok`` is
    three-state: ``None`` when the persisted row is unsigned-legacy (no key id),
    ``True``/``False`` for a signed row. The signature is verified over the
    *persisted* digest (what was signed at deploy time), independent of the
    supplied ``attestation`` payload — so a payload tamper trips the digest axis
    even while an untouched signature still validates, and a signature-byte flip
    trips the signature axis while the digest recompute still matches.
    """
    digest_mismatches = verify_attestation(deployment, attestation)
    key_id = getattr(deployment, "attestation_signing_key_id", None)
    if key_id is None:
        return digest_mismatches, None
    signature_ok = verify_attestation_signature(
        str(getattr(deployment, "attestation_digest", "")),
        getattr(deployment, "attestation_signature", None),
        key_id,
        getattr(deployment, "attestation_algorithm", None),
        signer,
    )
    return digest_mismatches, signature_ok


def _digest_json(payload: dict[str, object]) -> str:
    return hashlib.sha256(to_json_value(payload).encode("utf-8")).hexdigest()
