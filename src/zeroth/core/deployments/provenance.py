"""Legacy import path for :mod:`zeroth.service.deployments.provenance`."""

from zeroth.service.deployments.provenance import (
    build_attestation_payload,
    compute_attestation_digest,
    compute_contract_snapshot_digest,
    compute_graph_snapshot_digest,
    compute_settings_snapshot_digest,
    sign_attestation,
    verify_attestation,
    verify_attestation_full,
    verify_attestation_signature,
)

__all__ = [
    "build_attestation_payload",
    "compute_attestation_digest",
    "compute_contract_snapshot_digest",
    "compute_graph_snapshot_digest",
    "compute_settings_snapshot_digest",
    "sign_attestation",
    "verify_attestation",
    "verify_attestation_full",
    "verify_attestation_signature",
]
