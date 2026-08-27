"""Service-layer persistence for repo checkouts and repo runs (ZER-37).

Durable records for staged repository checkouts (with their signed
attestations) and script executions against them, plus the attestation
digest/sign/verify triple. All construction happens in the service bootstrap;
a later wave wires the API and orchestration flows.
"""

from zeroth.service.repositories.attestation import (
    CheckoutAttestationPayload,
    build_checkout_attestation,
    sign_checkout_attestation,
    verify_checkout_attestation,
)
from zeroth.service.repositories.repo_models import (
    INPUT_PAYLOAD_CAP_BYTES,
    OUTPUT_PAYLOAD_CAP_BYTES,
    RepoCheckout,
    RepoCheckoutState,
    RepoRun,
    RepoRunState,
)
from zeroth.service.repositories.repository import (
    ClaimedRepoRun,
    SQLiteRepoCheckoutRepository,
    SQLiteRepoRunRepository,
)

__all__ = [
    "INPUT_PAYLOAD_CAP_BYTES",
    "OUTPUT_PAYLOAD_CAP_BYTES",
    "CheckoutAttestationPayload",
    "ClaimedRepoRun",
    "RepoCheckout",
    "RepoCheckoutState",
    "RepoRun",
    "RepoRunState",
    "SQLiteRepoCheckoutRepository",
    "SQLiteRepoRunRepository",
    "build_checkout_attestation",
    "sign_checkout_attestation",
    "verify_checkout_attestation",
]
