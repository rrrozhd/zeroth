"""Recompute a run's governance level from state the server holds.

``CapabilityReporter._validated_level`` grants ``ENFORCED`` only for evidence
that carries ``governance_level is ENFORCED`` **and**
``tool_manifest_complete``. Nothing in the SDK can mint ``ENFORCED`` --
``report_tool_enforcement`` returns ``OBSERVED`` or ``ADMISSION`` -- so that
claim can only ever have originated client-side. This module is where it stops
being a claim.

**The manifest flag is derived, never copied.** ``tool_manifest_complete`` is
computed from the inventory the *server* has stored for the deployment: the
registered coverage must be exactly ``"complete"`` and its fingerprint must
equal the one bound into the signed attestation.
``RunAttestationPayload.inventory_coverage`` is read only to be ignored -- if
it were copied through, the flag that gates ``ENFORCED`` would be a
client-supplied boolean and the enforcement classification would be
self-asserted.

**``claimed_level`` is a ceiling, applied last.** It can only lower what the
server-side facts already support. An unrecognised claim lowers to
``ADMISSION`` rather than being discarded, matching how ``_gated_coverage``
falls back to ``PARTIAL`` and ``permits_full_enforcement`` refuses ``UNKNOWN``.

**The provider is bound to one tenant and deployment at construction.** The
Protocol passes only a ``correlation_id``, and a correlation id is
attacker-suppliable: ``zeroth.integrations.langgraph._correlation`` base64url-
decodes it out of ``config["configurable"]["_zeroth"]`` with no signature check.
A provider that looked up evidence by correlation alone would therefore let a
run assert someone else's correlation and read another tenant's evidence. The
tenant and deployment come from the trusted side of the call -- whoever
constructs the provider -- and every query is scoped by them.

**The binding is checked twice, deliberately.** Scoping the query is necessary
but not sufficient: it makes the *repository* responsible for a property the
provider is defined by. ``tenant_id``, ``deployment_ref`` and
``correlation_id`` are all bound inside the signed bytes, so
``_binds_to_this_provider`` compares the payload's own claims against this
provider's binding after the lookup returns. An attestation honestly signed for
another deployment -- nothing forged, merely paired with the wrong
registration -- is refused there even if the store hands it over.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from zeroth.core.langgraph_gateway.models import (
    GovernanceLevel,
    RunCapabilityEvidence,
)
from zeroth.governance.attestations.payload import (
    RunAttestationPayload,
    SignedRunAttestation,
)
from zeroth.governance.attestations.signing import verify_attestation
from zeroth.governance.attestations.store import (
    InventoryRegistration,
    InventoryRegistrationRepository,
    RunAttestationRepository,
)
from zeroth.governance.attestations.versions import (
    ADAPTER_VERSION,
    classify_version_agreement,
    permits_full_enforcement,
)
from zeroth.platform.primitives import utc_now
from zeroth.platform.signing import SigningKeyProvider

_COMPLETE_COVERAGE = "complete"
"""The one coverage token that completes a manifest.

Matched by exact equality against ``InventoryCoverage.COMPLETE``'s value. No
case folding and no stripping: every widening of this vocabulary is a
fail-open change, because it makes some token that is not ``"complete"``
count as complete.
"""

_LEVEL_RANK: dict[GovernanceLevel, int] = {
    GovernanceLevel.ADMISSION: 0,
    GovernanceLevel.OBSERVED: 1,
    GovernanceLevel.ENFORCED: 2,
}


def _coverage_is_complete(registration: InventoryRegistration | None) -> bool:
    """Return whether a registration declares full inventory coverage."""
    return registration is not None and registration.coverage == _COMPLETE_COVERAGE


def _claimed_ceiling(claimed_level: str) -> GovernanceLevel:
    """Return the ceiling a client's ``claimed_level`` imposes.

    An unrecognised token yields ``ADMISSION``: the server cannot honour a
    ceiling it does not understand, and refusing to grant anything is the
    fail-closed reading. Membership is tested explicitly rather than by
    constructing ``GovernanceLevel``, so garbage is a decision here instead of
    an exception caught somewhere upstream.

    Args:
        claimed_level: The level string carried in the signed payload.

    Returns:
        The highest level this claim permits.
    """
    for level in GovernanceLevel:
        if level.value == claimed_level:
            return level
    return GovernanceLevel.ADMISSION


def _apply_ceiling(level: GovernanceLevel, ceiling: GovernanceLevel) -> GovernanceLevel:
    """Return the weaker of a determined level and a claimed ceiling."""
    return level if _LEVEL_RANK[level] <= _LEVEL_RANK[ceiling] else ceiling


class PersistedCapabilityEvidenceProvider:
    """Serve run evidence recomputed from stored registrations and attestations."""

    def __init__(
        self,
        *,
        attestations: RunAttestationRepository,
        registrations: InventoryRegistrationRepository,
        signer: SigningKeyProvider | None,
        tenant_id: str,
        deployment_ref: str,
        expected_graph_version: str | None = None,
        expected_adapter_version: str | None = ADAPTER_VERSION,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        """Bind the provider to one tenant and deployment.

        Args:
            attestations: Store of signed run-start attestations.
            registrations: Store of declared tool inventories.
            signer: Key material the attestation signature is checked against;
                ``None`` makes every attestation unverifiable, never trusted.
            tenant_id: Tenant every lookup is scoped to.
            deployment_ref: Deployment whose registered inventory backs the runs
                this provider answers for.
            expected_graph_version: Graph version the gateway expects. Left
                ``None`` it classifies as ``UNKNOWN`` and no run reaches
                ``ENFORCED``. It is deliberately *not* derived from the stored
                registration: a registration is client-submitted, so deriving
                the expectation from it would let a client satisfy its own
                check.
            expected_adapter_version: Adapter wire contract the gateway expects.
            now: Clock used to enforce the attestation's signed ``expires_at``.
        """
        self._attestations = attestations
        self._registrations = registrations
        self._signer = signer
        self._tenant_id = tenant_id
        self._deployment_ref = deployment_ref
        self._expected_graph_version = expected_graph_version
        self._expected_adapter_version = expected_adapter_version
        self._now = now

    async def evidence_for_run(self, correlation_id: str) -> RunCapabilityEvidence | None:
        """Return recomputed evidence for one run, or ``None``.

        Args:
            correlation_id: The run correlation, scoped to this provider's
                tenant. A correlation stored under a different tenant reads as
                absent.

        Returns:
            Evidence whose level and manifest flag were derived server-side, or
            ``None`` when no attestation exists for this tenant and run, or when
            stored state could not be read. Never raises: the reporter treats a
            failure here as ``ADMISSION``, and mirroring its fail-closed posture
            keeps a storage fault from becoming a gateway error.
        """
        try:
            signed = await self._attestations.find_for_deployment(
                self._tenant_id,
                self._deployment_ref,
                correlation_id,
            )
            if signed is None or not self._binds_to_this_provider(signed, correlation_id):
                return None
            registration = await self._registrations.latest_for_deployment(
                self._tenant_id,
                self._deployment_ref,
            )
            return self._evidence(signed, registration)
        except Exception:  # noqa: BLE001 - trust boundary: unreadable state is no evidence
            return None

    def _binds_to_this_provider(
        self,
        signed: SignedRunAttestation,
        correlation_id: str,
    ) -> bool:
        """Return whether the signed payload names exactly what was asked for.

        The tenant, deployment and correlation are all bound *inside* the
        signed bytes, so this compares the provider's trusted binding against
        what the attestation itself claims -- it is not a second helping of the
        SQL predicate but the check that makes the predicate non-load-bearing.
        A repository widened, mis-indexed, or replaced by a double can hand
        back a foreign row; a provider defined by a tenant and a deployment
        must not emit evidence for anything else, whatever it was handed.

        Args:
            signed: The attestation the store returned.
            correlation_id: The run the caller asked about.

        Returns:
            True only when all three identities match this provider's binding.
        """
        payload = signed.payload
        return (
            payload.tenant_id == self._tenant_id
            and payload.deployment_ref == self._deployment_ref
            and payload.correlation_id == correlation_id
        )

    def _evidence(
        self,
        signed: SignedRunAttestation,
        registration: InventoryRegistration | None,
    ) -> RunCapabilityEvidence:
        """Build evidence from one attestation and the deployment's inventory."""
        payload = signed.payload
        signature_valid = verify_attestation(signed, self._signer)
        manifest_complete = self._manifest_complete(payload, registration)
        level = self._resolve_level(
            payload,
            signature_valid=signature_valid,
            manifest_complete=manifest_complete,
        )
        return RunCapabilityEvidence(
            correlation_id=payload.correlation_id,
            governance_level=level,
            observed_at=payload.issued_at,
            graph_version=payload.graph_version,
            signature_valid=signature_valid,
            tool_manifest_complete=manifest_complete,
        )

    def _manifest_complete(
        self,
        payload: RunAttestationPayload,
        registration: InventoryRegistration | None,
    ) -> bool:
        """Return whether stored state proves this run saw the whole inventory.

        Both halves come from the server side. The registration must declare
        complete coverage, and its fingerprint must be the one the run bound
        into its signed attestation -- otherwise the registration describes some
        other inventory and says nothing about this run.

        Compared with ``==`` rather than :func:`hmac.compare_digest`, unlike the
        digest comparison in ``zeroth.governance.decisions.repository``: there
        the attacker probes a secret-side value, here it supplies *both* sides,
        so a timing difference reveals nothing it does not already know.
        """
        if not _coverage_is_complete(registration):
            return False
        if registration is None or not registration.tools:
            # An empty identity set cannot evidence enforcement, however
            # honestly it is declared. Recomputing the digest (audit round 1)
            # closed the *forged* fingerprint, but not this: a caller can
            # register zero tools under ``coverage="complete"`` and attest the
            # digest the server itself computes for the empty set, at which
            # point both sides agree truthfully and the run reaches ENFORCED
            # while governing nothing. Round 2 reproduced exactly that. The
            # remaining support for completeness is then the adapter's bare
            # declaration, which is not evidence.
            #
            # This is also the reading migration 017 already applies to
            # pre-017 rows: ``_row_to_registration`` hydrates them with
            # ``tools=()`` because their stored digest was client-certified.
            return False
        return registration.inventory_fingerprint == payload.inventory_fingerprint

    def _resolve_level(
        self,
        payload: RunAttestationPayload,
        *,
        signature_valid: bool,
        manifest_complete: bool,
    ) -> GovernanceLevel:
        """Determine the level the server can prove, then lower it to the claim.

        ``expires_at`` gates ``ENFORCED`` only. It is bound into the signed
        material but deliberately unchecked by :func:`verify_attestation`, which
        answers "were these bytes signed by this key", so the TTL is enforced
        here against the provider's clock. An expired attestation still has a
        valid signature -- the fact is not rewritten, only its consequence.
        """
        if not signature_valid:
            return GovernanceLevel.ADMISSION
        agreement = classify_version_agreement(
            expected_graph_version=self._expected_graph_version,
            actual_graph_version=payload.graph_version,
            expected_adapter_version=self._expected_adapter_version,
            actual_adapter_version=payload.adapter_version,
        )
        expired = self._now() > payload.expires_at
        determined = (
            GovernanceLevel.ENFORCED
            if manifest_complete and permits_full_enforcement(agreement) and not expired
            else GovernanceLevel.OBSERVED
        )
        return _apply_ceiling(determined, _claimed_ceiling(payload.claimed_level))


__all__ = ["PersistedCapabilityEvidenceProvider"]
