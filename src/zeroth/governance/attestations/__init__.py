"""Signed run-start attestations binding run identity to a tool inventory.

The server must never echo a client-claimed governance level. This package
supplies the signed payload that lets a verifier attribute a claim to a key and
to one specific run, so the enforcement ceiling can be recomputed server-side
instead of trusted from the wire.
"""

from zeroth.governance.attestations.heartbeat import (
    DEFAULT_STALE_AFTER_SECONDS,
    DeploymentStatusResolver,
    Heartbeat,
    HeartbeatRepository,
)
from zeroth.governance.attestations.inventory import (
    RegisteredTool,
    recompute_inventory_fingerprint,
)
from zeroth.governance.attestations.payload import (
    RunAttestationPayload,
    SignedRunAttestation,
)
from zeroth.governance.attestations.provider import (
    PersistedCapabilityEvidenceProvider,
)
from zeroth.governance.attestations.signing import (
    attestation_digest,
    sign_attestation,
    verify_attestation,
)
from zeroth.governance.attestations.store import (
    InventoryRegistration,
    InventoryRegistrationRepository,
    RunAttestationRepository,
)

__all__ = [
    "DEFAULT_STALE_AFTER_SECONDS",
    "DeploymentStatusResolver",
    "Heartbeat",
    "HeartbeatRepository",
    "InventoryRegistration",
    "InventoryRegistrationRepository",
    "PersistedCapabilityEvidenceProvider",
    "RegisteredTool",
    "RunAttestationPayload",
    "RunAttestationRepository",
    "SignedRunAttestation",
    "attestation_digest",
    "recompute_inventory_fingerprint",
    "sign_attestation",
    "verify_attestation",
]
