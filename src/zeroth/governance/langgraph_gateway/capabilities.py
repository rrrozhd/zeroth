"""Conservative capability reporting over optional adapter evidence."""

from __future__ import annotations

import math
import warnings
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from zeroth.contracts.langgraph_gateway.models import GovernanceLevel, RunCapabilityEvidence


class CapabilityEvidenceProvider(Protocol):
    """Read attested evidence without prescribing its eventual storage."""

    async def evidence_for_run(self, correlation_id: str) -> RunCapabilityEvidence | None:
        """Return evidence for a legacy correlation ID, if unambiguous."""
        ...

    async def evidence_for_governance_run(
        self, governance_run_id: str
    ) -> RunCapabilityEvidence | None:
        """Return evidence for this exact signed governance run ID."""
        ...


class NoCapabilityEvidenceProvider:
    """Foundation provider: gateway-only deployments have no adapter evidence."""

    async def evidence_for_run(self, correlation_id: str) -> None:
        """Accept a legacy correlation ID without reporting evidence."""
        return None

    async def evidence_for_governance_run(self, governance_run_id: str) -> None:
        """Accept a governance run ID without reporting evidence."""
        return None


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


class CapabilityReporter:
    """Clamp requested governance levels to valid, fresh evidence."""

    def __init__(
        self,
        evidence_provider: CapabilityEvidenceProvider | None = None,
        *,
        governance_evidence_provider: CapabilityEvidenceProvider | None = None,
        stale_after_seconds: float = 90.0,
        expected_graph_version: str | None = None,
        expected_adapter_version: str | None = None,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not math.isfinite(stale_after_seconds) or stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive and finite")
        self._evidence_provider = (
            NoCapabilityEvidenceProvider() if evidence_provider is None else evidence_provider
        )
        self._governance_evidence_provider = (
            self._evidence_provider
            if governance_evidence_provider is None
            else governance_evidence_provider
        )
        self._stale_after_seconds = stale_after_seconds
        self._expected_graph_version = expected_graph_version
        self._expected_adapter_version = expected_adapter_version
        self._now = now

    # noqa comment: this function's complexity predates ZER-24. The relocation
    # moved the module without changing a line of the body; reducing it here
    # would mix a behaviour-bearing refactor into a pure move.
    def _validated_level(  # noqa: C901
        self,
        evidence: RunCapabilityEvidence | None,
        *,
        governance_run_id: str | None = None,
        correlation_id: str | None = None,
        graph_version: str | None = None,
    ) -> GovernanceLevel:
        try:
            if evidence is None or not evidence.signature_valid:
                return GovernanceLevel.ADMISSION
            if governance_run_id is not None and evidence.run_id != governance_run_id:
                return GovernanceLevel.ADMISSION
            if correlation_id is not None and evidence.correlation_id != correlation_id:
                return GovernanceLevel.ADMISSION

            expected_graph_version = (
                graph_version if graph_version is not None else self._expected_graph_version
            )
            if (
                expected_graph_version is not None
                and evidence.graph_version != expected_graph_version
            ):
                return GovernanceLevel.ADMISSION
            if (
                self._expected_adapter_version is not None
                and evidence.adapter_version != self._expected_adapter_version
            ):
                return GovernanceLevel.ADMISSION

            observed_at = evidence.observed_at
            current_time = self._now()
            if observed_at.tzinfo is None or current_time.tzinfo is None:
                return GovernanceLevel.ADMISSION
            age_seconds = (current_time - observed_at).total_seconds()
            if not math.isfinite(age_seconds):
                return GovernanceLevel.ADMISSION
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
        except Exception:
            return GovernanceLevel.ADMISSION

    async def level_for_run(
        self,
        correlation_id: str,
        *,
        graph_version: str | None = None,
        deployment_evidence: RunCapabilityEvidence | None = None,
    ) -> GovernanceLevel:
        """Report legacy correlation evidence, if unambiguous. Deprecated."""
        warnings.warn(
            "level_for_run() is deprecated; use level_for_governance_run()",
            DeprecationWarning,
            stacklevel=2,
        )
        del deployment_evidence
        try:
            evidence = await self._evidence_provider.evidence_for_run(correlation_id)
        except Exception:
            return GovernanceLevel.ADMISSION
        return self._validated_level(
            evidence,
            correlation_id=correlation_id,
            graph_version=graph_version,
        )

    async def level_for_governance_run(
        self,
        governance_run_id: str,
        *,
        correlation_id: str | None = None,
        graph_version: str | None = None,
    ) -> GovernanceLevel:
        """Report capability for an exact signed governance run ID."""
        try:
            evidence = await self._governance_evidence_provider.evidence_for_governance_run(
                governance_run_id
            )
        except Exception:
            return GovernanceLevel.ADMISSION
        return self._validated_level(
            evidence,
            governance_run_id=governance_run_id,
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
        """Deprecated compatibility alias using legacy correlation semantics."""
        return await self.level_for_run(correlation_id, graph_version=graph_version)

    async def report_governance_run(
        self,
        governance_run_id: str,
        *,
        correlation_id: str | None = None,
        graph_version: str | None = None,
    ) -> GovernanceLevel:
        """Reporting alias for an exact signed governance run ID."""
        return await self.level_for_governance_run(
            governance_run_id,
            correlation_id=correlation_id,
            graph_version=graph_version,
        )

    def report_deployment(
        self,
        evidence: RunCapabilityEvidence | None = None,
        *,
        graph_version: str | None = None,
    ) -> GovernanceLevel:
        """Compatibility alias for deployment health consumers."""
        return self.level_for_deployment(evidence, graph_version=graph_version)
