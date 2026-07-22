"""Conservative capability reporting over optional adapter evidence."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from zeroth.core.langgraph_gateway.models import GovernanceLevel, RunCapabilityEvidence


class CapabilityEvidenceProvider(Protocol):
    """Read attested evidence without prescribing its eventual storage."""

    async def evidence_for_run(self, correlation_id: str) -> RunCapabilityEvidence | None:
        """Return evidence for this exact run, or ``None``."""
        ...


class NoCapabilityEvidenceProvider:
    """Foundation provider: gateway-only deployments have no adapter evidence."""

    async def evidence_for_run(self, correlation_id: str) -> None:
        return None


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


class CapabilityReporter:
    """Clamp requested governance levels to valid, fresh evidence."""

    def __init__(
        self,
        evidence_provider: CapabilityEvidenceProvider | None = None,
        *,
        stale_after_seconds: float = 90.0,
        expected_graph_version: str | None = None,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        self._evidence_provider = evidence_provider or NoCapabilityEvidenceProvider()
        self._stale_after_seconds = stale_after_seconds
        self._expected_graph_version = expected_graph_version
        self._now = now

    def _validated_level(
        self,
        evidence: RunCapabilityEvidence | None,
        *,
        correlation_id: str | None = None,
        graph_version: str | None = None,
    ) -> GovernanceLevel:
        if evidence is None or not evidence.signature_valid:
            return GovernanceLevel.ADMISSION
        if correlation_id is not None and evidence.correlation_id != correlation_id:
            return GovernanceLevel.ADMISSION

        expected_graph_version = graph_version or self._expected_graph_version
        if expected_graph_version is not None and evidence.graph_version != expected_graph_version:
            return GovernanceLevel.ADMISSION

        observed_at = evidence.observed_at
        if observed_at.tzinfo is None:
            return GovernanceLevel.ADMISSION
        age_seconds = (self._now() - observed_at).total_seconds()
        if age_seconds < 0 or age_seconds > self._stale_after_seconds:
            return GovernanceLevel.ADMISSION

        if evidence.governance_level is GovernanceLevel.ADMISSION:
            return GovernanceLevel.ADMISSION
        if (
            evidence.governance_level is GovernanceLevel.ENFORCED
            and evidence.tool_manifest_complete
        ):
            return GovernanceLevel.ENFORCED
        return GovernanceLevel.OBSERVED

    async def level_for_run(
        self,
        correlation_id: str,
        *,
        graph_version: str | None = None,
        deployment_evidence: RunCapabilityEvidence | None = None,
    ) -> GovernanceLevel:
        """Report a run from its own attestation; heartbeat evidence cannot upgrade it."""
        del deployment_evidence
        evidence = await self._evidence_provider.evidence_for_run(correlation_id)
        return self._validated_level(
            evidence,
            correlation_id=correlation_id,
            graph_version=graph_version,
        )

    def level_for_deployment(
        self,
        evidence: RunCapabilityEvidence | None = None,
        *,
        graph_version: str | None = None,
    ) -> GovernanceLevel:
        """Report last-known deployment capability from fresh heartbeat evidence."""
        return self._validated_level(evidence, graph_version=graph_version)

    async def report_run(
        self,
        correlation_id: str,
        *,
        graph_version: str | None = None,
    ) -> GovernanceLevel:
        """Compatibility alias for consumers phrased in reporting terms."""
        return await self.level_for_run(correlation_id, graph_version=graph_version)

    def report_deployment(
        self,
        evidence: RunCapabilityEvidence | None = None,
        *,
        graph_version: str | None = None,
    ) -> GovernanceLevel:
        """Compatibility alias for deployment health consumers."""
        return self.level_for_deployment(evidence, graph_version=graph_version)
