"""Digest and attestation helpers for deployment snapshots."""

from __future__ import annotations

import hashlib

from zeroth.storage.json import to_json_value


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
        "graph_snapshot_digest": compute_graph_snapshot_digest(
            deployment.serialized_graph
        ),
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


def _digest_json(payload: dict[str, object]) -> str:
    return hashlib.sha256(to_json_value(payload).encode("utf-8")).hexdigest()
