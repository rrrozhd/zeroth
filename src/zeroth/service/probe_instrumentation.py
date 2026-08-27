"""Shared persistent cost, audit, and Regulus path for explicit live probes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from time import perf_counter
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy.orm import Session

from zeroth.econ.analytics.identity import capability_identity, implementation_identity
from zeroth.econ.analytics.registration import register_probe_economics
from zeroth.econ.instrumentation.schemas import ExecutionEvent
from zeroth.econ.measurement import MeasurementState
from zeroth.econ.plane.enforcement.service import (
    commit_cost,
    mark_cost_ambiguous,
    release_cost,
    reserve_cost,
)
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.governance.audit.models import NodeAuditRecord, TokenUsage
from zeroth.integrations.memory.embedding_calls import (
    EmbeddingCallBound,
    EmbeddingCallIdentity,
    EmbeddingCallResult,
)
from zeroth.platform.storage.scoping import TenantWideScopeContext


@dataclass(frozen=True, slots=True)
class ProbeEvidence:
    cost_event_id: str
    cost_measurement: str
    provider_request_id: str | None
    cleanup_status: str


@dataclass(frozen=True, slots=True)
class _PendingEmbedding:
    identity: EmbeddingCallIdentity
    bound: EmbeddingCallBound
    capability_id: str
    implementation_id: str
    started_at: float


def _event_id(tenant_id: str, operation_id: str) -> str:
    return f"probe_{uuid5(NAMESPACE_URL, f'{tenant_id}:{operation_id}').hex}"


class PersistentProbeInstrumentation:
    """Persist admission first, then emit the same identity to Audit and Regulus."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        regulus_client: Any,
        audit_repository: Any,
        deployment_ref: str,
        graph_version_ref: str,
        workspace_id: str | None,
    ) -> None:
        self._sessions = session_factory
        self._regulus = regulus_client
        self._audit = audit_repository
        self._deployment_ref = deployment_ref
        self._graph_version_ref = graph_version_ref
        self._workspace_id = workspace_id

    def _scope(self, raw: Session, tenant_id: str) -> ScopedSession:
        return ScopedSession(raw, TenantWideScopeContext(tenant_id=tenant_id))

    def _require_sinks(self) -> None:
        if self._regulus is None or self._audit is None:
            raise RuntimeError("probe audit/economics control plane is unavailable")

    async def reserve_probe(
        self,
        *,
        tenant_id: str,
        campaign_id: str | None,
        operation_id: str,
        run_id: str | None,
        max_cost_usd: str,
        run_cap_usd: str | None,
        capability_id: str,
        implementation_id: str,
    ) -> None:
        self._require_sinks()
        with self._sessions() as raw:
            scoped = self._scope(raw, tenant_id)
            register_probe_economics(
                scoped,
                tenant_id=tenant_id,
                deployment_ref=self._deployment_ref,
                capability_name=capability_id,
                implementation_name=implementation_id,
            )
            reserve_cost(
                scoped,
                operation_id=operation_id,
                max_cost_usd=Decimal(max_cost_usd),
                campaign_id=campaign_id,
                run_id=run_id,
                deployment_ref=self._deployment_ref,
                evidence_kind="production",
                run_cap_usd=Decimal(run_cap_usd) if run_cap_usd is not None else None,
                require_new=True,
            )

    async def _emit(
        self,
        *,
        tenant_id: str,
        campaign_id: str | None,
        operation_id: str,
        run_id: str | None,
        capability_id: str,
        implementation_id: str,
        cost_event_id: str,
        actual_cost_usd: Decimal | None,
        cost_measurement: str,
        provider_request_id: str | None,
        cleanup_status: str,
        latency_ms: int | float,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        status: str = "completed",
    ) -> None:
        self._require_sinks()
        measurement = MeasurementState(cost_measurement)
        metadata = {
            "campaign_id": campaign_id,
            "operation_id": operation_id,
            "run_id": run_id,
            "provider_request_id": provider_request_id,
            "cleanup_status": cleanup_status,
            "probe": not operation_id.startswith("workflow:"),
        }
        registered_capability_id = capability_identity(
            tenant_id,
            self._deployment_ref,
            capability_id,
        )
        registered_implementation_id = implementation_identity(
            registered_capability_id,
            implementation_id,
        )
        event = ExecutionEvent(
            execution_id=cost_event_id,
            join_key=run_id or operation_id,
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            operation_id=operation_id,
            deployment_ref=self._deployment_ref,
            evidence_kind="production",
            provider_request_id=provider_request_id,
            cleanup_status=cleanup_status,
            capability_id=registered_capability_id,
            implementation_id=registered_implementation_id,
            model_version=implementation_id,
            token_cost_usd=actual_cost_usd,
            cost_measurement=measurement,
            usage_measurement=(
                MeasurementState.MEASURED
                if input_tokens is not None or output_tokens is not None
                else MeasurementState.UNMEASURED
            ),
            latency_ms=int(latency_ms),
            metadata=metadata
            | {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "probe_capability_id": capability_id,
                "probe_implementation_id": implementation_id,
            },
        )

        now = datetime.now(UTC)
        await self._audit.write(
            NodeAuditRecord(
                audit_id=f"audit_{cost_event_id}",
                run_id=run_id or f"probe:{operation_id}",
                node_id=capability_id,
                graph_version_ref=self._graph_version_ref,
                deployment_ref=self._deployment_ref,
                tenant_id=tenant_id,
                workspace_id=self._workspace_id,
                campaign_id=campaign_id,
                status=status,
                execution_metadata=metadata
                | {
                    "implementation_id": implementation_id,
                    "cost_measurement": cost_measurement,
                },
                token_usage=(
                    TokenUsage(
                        input_tokens=input_tokens or 0,
                        output_tokens=output_tokens or 0,
                        total_tokens=(input_tokens or 0) + (output_tokens or 0),
                        model_name=implementation_id,
                    )
                    if input_tokens is not None or output_tokens is not None
                    else None
                ),
                cost_usd=float(actual_cost_usd) if actual_cost_usd is not None else None,
                cost_measurement=measurement,
                cost_event_id=cost_event_id,
                started_at=now,
                completed_at=now,
            )
        )
        self._regulus.track_execution_confirmed(event)

    async def commit_probe(
        self,
        *,
        tenant_id: str,
        campaign_id: str | None,
        operation_id: str,
        run_id: str | None,
        capability_id: str,
        implementation_id: str,
        actual_cost_usd: str,
        cost_measurement: str,
        provider_request_id: str | None,
        cleanup_status: str,
        latency_ms: int | float,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> ProbeEvidence:
        event_id = _event_id(tenant_id, operation_id)
        actual = Decimal(actual_cost_usd)
        try:
            await self._emit(
                tenant_id=tenant_id,
                campaign_id=campaign_id,
                operation_id=operation_id,
                run_id=run_id,
                capability_id=capability_id,
                implementation_id=implementation_id,
                cost_event_id=event_id,
                actual_cost_usd=actual,
                cost_measurement=cost_measurement,
                provider_request_id=provider_request_id,
                cleanup_status=cleanup_status,
                latency_ms=latency_ms,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        except Exception:
            with self._sessions() as raw:
                mark_cost_ambiguous(
                    self._scope(raw, tenant_id),
                    operation_id=operation_id,
                    cost_event_id=event_id,
                    provider_request_id=provider_request_id,
                    cleanup_status="pending_regulus_delivery",
                )
            raise
        with self._sessions() as raw:
            commit_cost(
                self._scope(raw, tenant_id),
                operation_id=operation_id,
                actual_cost_usd=actual,
                cost_measurement=cost_measurement,
                cost_event_id=event_id,
                provider_request_id=provider_request_id,
                cleanup_status=cleanup_status,
            )
        return ProbeEvidence(event_id, cost_measurement, provider_request_id, cleanup_status)

    async def mark_probe_ambiguous(
        self,
        *,
        tenant_id: str,
        campaign_id: str | None,
        operation_id: str,
        run_id: str | None,
        capability_id: str,
        implementation_id: str,
        latency_ms: int | float,
        cleanup_status: str = "pending_reconciliation",
        provider_request_id: str | None = None,
    ) -> ProbeEvidence:
        event_id = _event_id(tenant_id, operation_id)
        with self._sessions() as raw:
            mark_cost_ambiguous(
                self._scope(raw, tenant_id),
                operation_id=operation_id,
                cost_event_id=event_id,
                provider_request_id=provider_request_id,
                cleanup_status=cleanup_status,
            )
        await self._emit(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            operation_id=operation_id,
            run_id=run_id,
            capability_id=capability_id,
            implementation_id=implementation_id,
            cost_event_id=event_id,
            actual_cost_usd=None,
            cost_measurement="unmeasured",
            provider_request_id=provider_request_id,
            cleanup_status=cleanup_status,
            latency_ms=latency_ms,
            status="ambiguous",
        )
        return ProbeEvidence(event_id, "unmeasured", provider_request_id, cleanup_status)

    async def release_probe(
        self,
        *,
        tenant_id: str,
        operation_id: str,
        cleanup_status: str = "complete",
    ) -> ProbeEvidence:
        event_id = _event_id(tenant_id, operation_id)
        with self._sessions() as raw:
            release_cost(
                self._scope(raw, tenant_id),
                operation_id=operation_id,
                cleanup_status=cleanup_status,
            )
        return ProbeEvidence(event_id, "measured", None, cleanup_status)


class PersistentEmbeddingInstrumentation:
    """Adapt workflow embedding calls to the persistent probe accounting path."""

    def __init__(
        self,
        *,
        instrumentation: PersistentProbeInstrumentation,
        cost_estimator: Any,
        run_cap_usd: Decimal,
    ) -> None:
        if run_cap_usd <= 0:
            raise ValueError("run_cap_usd must be positive")
        self._instrumentation = instrumentation
        self._cost_estimator = cost_estimator
        self._run_cap_usd = run_cap_usd
        self._pending: dict[str, _PendingEmbedding] = {}
        self._settled: dict[EmbeddingCallIdentity, list[dict[str, Any]]] = {}

    @staticmethod
    def _input_tokens(usage: Any) -> int | None:
        if not isinstance(usage, dict):
            return None
        for key in ("input_tokens", "prompt_tokens", "total_tokens"):
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        return None

    async def reserve(
        self,
        identity: EmbeddingCallIdentity,
        bound: EmbeddingCallBound,
    ) -> str:
        maximum = Decimal(
            str(
                self._cost_estimator.estimate(
                    bound.model,
                    input_tokens=bound.input_utf8_bytes,
                    output_tokens=0,
                )
            )
        )
        if not maximum.is_finite() or maximum <= 0:
            raise RuntimeError("embedding maximum cost is not calculable")
        operation_id = (
            f"workflow-embedding:{identity.run_id[:48]}:{identity.node_id[:32]}:"
            f"{identity.operation}:{uuid4().hex}"
        )
        capability_id = f"memory.embedding.{identity.node_id}"
        implementation_id = bound.model
        await self._instrumentation.reserve_probe(
            tenant_id=identity.tenant_id,
            campaign_id=identity.campaign_id,
            operation_id=operation_id,
            run_id=identity.run_id,
            max_cost_usd=str(maximum),
            run_cap_usd=str(self._run_cap_usd),
            capability_id=capability_id,
            implementation_id=implementation_id,
        )
        self._pending[operation_id] = _PendingEmbedding(
            identity=identity,
            bound=bound,
            capability_id=capability_id,
            implementation_id=implementation_id,
            started_at=perf_counter(),
        )
        return operation_id

    def _pop(self, reservation_id: str) -> _PendingEmbedding:
        try:
            return self._pending.pop(reservation_id)
        except KeyError as exc:
            raise RuntimeError("unknown embedding reservation") from exc

    async def succeed(self, reservation_id: str, result: EmbeddingCallResult) -> None:
        pending = self._pending.get(reservation_id)
        if pending is None:
            raise RuntimeError("unknown embedding reservation")
        input_tokens = self._input_tokens(result.usage)
        if input_tokens is None:
            await self.ambiguous(reservation_id, "missing_provider_usage")
            raise RuntimeError("embedding provider usage is required")
        actual = Decimal(
            str(
                self._cost_estimator.estimate(
                    pending.bound.model,
                    input_tokens=input_tokens,
                    output_tokens=0,
                )
            )
        )
        evidence = await self._instrumentation.commit_probe(
            tenant_id=pending.identity.tenant_id,
            campaign_id=pending.identity.campaign_id,
            operation_id=reservation_id,
            run_id=pending.identity.run_id,
            capability_id=pending.capability_id,
            implementation_id=pending.implementation_id,
            actual_cost_usd=str(actual),
            cost_measurement="estimated",
            provider_request_id=result.provider_request_id,
            cleanup_status="complete",
            latency_ms=int((perf_counter() - pending.started_at) * 1000),
            input_tokens=input_tokens,
            output_tokens=0,
        )
        self._settled.setdefault(pending.identity, []).append(
            {
                "operation_id": reservation_id,
                "cost_event_id": evidence.cost_event_id,
                "provider_request_id": result.provider_request_id,
                "estimated_cost_usd": actual,
                "cost_measurement": "estimated",
                "cleanup_status": evidence.cleanup_status,
            }
        )
        self._pop(reservation_id)

    async def consume_call_costs(
        self, identity: EmbeddingCallIdentity
    ) -> tuple[dict[str, Any], ...]:
        """Return completed call costs once for promotion into node history."""
        return tuple(self._settled.pop(identity, ()))

    async def ambiguous(self, reservation_id: str, reason: str) -> None:
        pending = self._pending.get(reservation_id)
        if pending is None:
            raise RuntimeError("unknown embedding reservation")
        await self._instrumentation.mark_probe_ambiguous(
            tenant_id=pending.identity.tenant_id,
            campaign_id=pending.identity.campaign_id,
            operation_id=reservation_id,
            run_id=pending.identity.run_id,
            capability_id=pending.capability_id,
            implementation_id=pending.implementation_id,
            latency_ms=int((perf_counter() - pending.started_at) * 1000),
            cleanup_status=f"pending_reconciliation:{reason[:64]}",
        )
        self._pop(reservation_id)


class ReservedProbeEmbeddingInstrumentation:
    """Settle one already-reserved connector probe from its embedding response.

    Connector tests reserve the caller-supplied operation ID before touching the
    backend.  This hook is installed around the probe connector call so the
    embedding boundary can commit that same reservation with provider usage and
    request identity.  It is deliberately single-use: the cheap connector probe
    contract performs exactly one embedding-producing write.
    """

    def __init__(
        self,
        *,
        instrumentation: PersistentProbeInstrumentation,
        cost_estimator: Any,
        tenant_id: str,
        campaign_id: str,
        operation_id: str,
        run_id: str | None,
        capability_id: str,
        implementation_id: str,
    ) -> None:
        required = (
            tenant_id,
            campaign_id,
            operation_id,
            capability_id,
            implementation_id,
        )
        if any(not value for value in required):
            raise ValueError("reserved probe embedding identities are required")
        self._instrumentation = instrumentation
        self._cost_estimator = cost_estimator
        self._tenant_id = tenant_id
        self._campaign_id = campaign_id
        self._operation_id = operation_id
        self._run_id = run_id
        self._capability_id = capability_id
        self._implementation_id = implementation_id
        self._started_at: float | None = None
        self._bound: EmbeddingCallBound | None = None
        self.evidence: ProbeEvidence | None = None
        self.estimated_cost_usd: Decimal | None = None

    async def reserve(
        self,
        identity: EmbeddingCallIdentity,
        bound: EmbeddingCallBound,
    ) -> str:
        if self._started_at is not None or self.evidence is not None:
            raise RuntimeError("connector probe must perform exactly one embedding call")
        if (
            identity.tenant_id != self._tenant_id
            or identity.campaign_id != self._campaign_id
            or identity.run_id != (self._run_id or identity.run_id)
            or bound.model != self._implementation_id
        ):
            raise RuntimeError("connector probe embedding identity changed after admission")
        self._started_at = perf_counter()
        self._bound = bound
        return self._operation_id

    def _require_pending(self, reservation_id: str) -> tuple[EmbeddingCallBound, float]:
        if (
            reservation_id != self._operation_id
            or self._bound is None
            or self._started_at is None
            or self.evidence is not None
        ):
            raise RuntimeError("unknown or settled connector probe reservation")
        return self._bound, self._started_at

    async def succeed(self, reservation_id: str, result: EmbeddingCallResult) -> None:
        bound, started_at = self._require_pending(reservation_id)
        input_tokens = PersistentEmbeddingInstrumentation._input_tokens(result.usage)
        if input_tokens is None:
            await self.ambiguous(reservation_id, "missing_provider_usage")
            raise RuntimeError("embedding provider usage is required")
        actual = Decimal(
            str(
                self._cost_estimator.estimate(
                    bound.model,
                    input_tokens=input_tokens,
                    output_tokens=0,
                )
            )
        )
        if not actual.is_finite() or actual < 0:
            await self.ambiguous(reservation_id, "invalid_cost_measurement")
            raise RuntimeError("embedding actual cost is not calculable")
        self.evidence = await self._instrumentation.commit_probe(
            tenant_id=self._tenant_id,
            campaign_id=self._campaign_id,
            operation_id=self._operation_id,
            run_id=self._run_id,
            capability_id=self._capability_id,
            implementation_id=self._implementation_id,
            actual_cost_usd=str(actual),
            cost_measurement="estimated",
            provider_request_id=result.provider_request_id,
            cleanup_status="complete",
            latency_ms=int((perf_counter() - started_at) * 1000),
            input_tokens=input_tokens,
            output_tokens=0,
        )
        self.estimated_cost_usd = actual

    async def ambiguous(self, reservation_id: str, reason: str) -> None:
        _, started_at = self._require_pending(reservation_id)
        self.evidence = await self._instrumentation.mark_probe_ambiguous(
            tenant_id=self._tenant_id,
            campaign_id=self._campaign_id,
            operation_id=self._operation_id,
            run_id=self._run_id,
            capability_id=self._capability_id,
            implementation_id=self._implementation_id,
            latency_ms=int((perf_counter() - started_at) * 1000),
            cleanup_status=f"pending_reconciliation:{reason[:64]}",
        )
