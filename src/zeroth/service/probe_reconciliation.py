"""Fail-closed operator recovery for a completed probe missing Regulus delivery.

This module only reconciles durable evidence.  It has no provider or connector
dependency and cannot repeat the billable operation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from zeroth.econ.analytics.identity import capability_identity, implementation_identity
from zeroth.econ.analytics.registration import register_probe_economics
from zeroth.econ.instrumentation.schemas import ExecutionEvent
from zeroth.econ.measurement import MeasurementState
from zeroth.econ.plane.enforcement.models import AuditLog, CostReservation
from zeroth.econ.plane.enforcement.service import reconcile_cost
from zeroth.econ.plane.instrumentation.models import ExecutionEvent as StoredExecutionEvent
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.governance.audit.models import NodeAuditRecord
from zeroth.governance.audit.verifier import AuditContinuityVerifier
from zeroth.platform.storage.scoping import TenantWideScopeContext


class ProbeReconciliationError(RuntimeError):
    """The durable evidence was absent, ambiguous, or internally inconsistent."""


@dataclass(frozen=True, slots=True)
class ProbeReconciliationRequest:
    """Operator-asserted identity and amounts for one recovery.

    Requiring every value prevents a broad "find something close" recovery from
    silently selecting a different tenant, campaign, run, or provider call.
    """

    tenant_id: str
    campaign_id: str
    operation_id: str
    run_id: str
    deployment_ref: str
    capability_id: str
    implementation_id: str
    cost_event_id: str
    provider_request_id: str
    held_max_cost_usd: Decimal
    actual_cost_usd: Decimal
    cost_measurement: Literal["measured", "estimated"]
    actor_sub: str

    def __post_init__(self) -> None:
        identifiers = (
            self.tenant_id,
            self.campaign_id,
            self.operation_id,
            self.run_id,
            self.deployment_ref,
            self.capability_id,
            self.implementation_id,
            self.cost_event_id,
            self.provider_request_id,
            self.actor_sub,
        )
        if any(not value.strip() for value in identifiers):
            raise ValueError("all probe reconciliation identifiers are required")
        maximum = _money(self.held_max_cost_usd)
        actual = _money(self.actual_cost_usd)
        if maximum <= 0:
            raise ValueError("held maximum cost must be positive")
        if actual > maximum:
            raise ValueError("actual cost exceeds held maximum")


@dataclass(frozen=True, slots=True)
class ProbeReconciliationResult:
    operation_id: str
    cost_event_id: str
    delivery: Literal["inserted", "already_present"]
    actual_cost_usd: Decimal
    released_cost_usd: Decimal


@dataclass(frozen=True, slots=True)
class _VerifiedEvidence:
    audit: NodeAuditRecord
    event: ExecutionEvent


def _money(value: Decimal | float | str) -> Decimal:
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError("cost amount must be finite and non-negative")
    return amount.quantize(Decimal("0.00000001"))


class AmbiguousProbeReconciler:
    """Verify, deliver, and settle one already-completed ambiguous probe."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        regulus_client: Any,
        audit_repository: Any,
        audit_signer: Any,
    ) -> None:
        if regulus_client is None or audit_repository is None or audit_signer is None:
            raise ValueError("Regulus, audit repository, and audit signer are required")
        self._sessions = session_factory
        self._regulus = regulus_client
        self._audits = audit_repository
        self._signer = audit_signer

    @staticmethod
    def _scope(raw: Session, tenant_id: str) -> ScopedSession:
        return ScopedSession(raw, TenantWideScopeContext(tenant_id=tenant_id))

    def _reservation(self, request: ProbeReconciliationRequest) -> CostReservation:
        with self._sessions() as raw:
            db = self._scope(raw, request.tenant_id)
            row = db.execute(
                select(CostReservation).where(CostReservation.operation_id == request.operation_id)
            ).scalar_one_or_none()
            if row is None:
                raise ProbeReconciliationError("exact reservation was not found")
            expected = {
                "status": "ambiguous",
                "campaign_id": request.campaign_id,
                "run_id": request.run_id,
                "deployment_ref": request.deployment_ref,
                "evidence_kind": "production",
                "cost_event_id": request.cost_event_id,
                "provider_request_id": request.provider_request_id,
                "cleanup_status": "pending_regulus_delivery",
            }
            mismatches = [name for name, value in expected.items() if getattr(row, name) != value]
            if _money(row.max_cost_usd) != _money(request.held_max_cost_usd):
                mismatches.append("max_cost_usd")
            if _money(row.held_cost_usd) != _money(request.held_max_cost_usd):
                mismatches.append("held_cost_usd")
            if row.actual_cost_usd is not None:
                mismatches.append("actual_cost_usd")
            if mismatches:
                raise ProbeReconciliationError(
                    "reservation evidence mismatch: " + ", ".join(mismatches)
                )
            raw.expunge(row)
            return row

    async def _audit(self, request: ProbeReconciliationRequest) -> NodeAuditRecord:
        report = await AuditContinuityVerifier(self._audits, signer=self._signer).verify_run(
            request.run_id,
            tenant_id=request.tenant_id,
            deployment_ref=request.deployment_ref,
        )
        if not report.verified or report.signature_verified is not True:
            raise ProbeReconciliationError("signed audit verification failed")
        records = await self._audits.list_by_run(
            request.run_id,
            tenant_id=request.tenant_id,
            deployment_ref=request.deployment_ref,
        )
        expected_audit_id = f"audit_{request.cost_event_id}"
        matches = [record for record in records if record.audit_id == expected_audit_id]
        if len(matches) != 1:
            raise ProbeReconciliationError("expected exactly one completed probe audit")
        record = matches[0]
        expected = {
            "tenant_id": request.tenant_id,
            "campaign_id": request.campaign_id,
            "run_id": request.run_id,
            "deployment_ref": request.deployment_ref,
            "node_id": request.capability_id,
            "status": "completed",
            "cost_event_id": request.cost_event_id,
        }
        mismatches = [name for name, value in expected.items() if getattr(record, name) != value]
        metadata_expected = {
            "campaign_id": request.campaign_id,
            "operation_id": request.operation_id,
            "run_id": request.run_id,
            "implementation_id": request.implementation_id,
            "cost_measurement": request.cost_measurement,
        }
        direct_metadata_matches = all(
            record.execution_metadata.get(name) == value
            for name, value in metadata_expected.items()
        )
        if not direct_metadata_matches and not self._is_metadata_only_capture(record):
            mismatches.append("execution_metadata")
        if record.completed_at is None:
            mismatches.append("completed_at")
        if (
            record.cost_measurement is None
            or record.cost_measurement.value != request.cost_measurement
        ):
            mismatches.append("cost_measurement")
        if record.cost_usd is None or _money(record.cost_usd) != _money(request.actual_cost_usd):
            mismatches.append("audit actual cost")
        if record.token_usage is None or record.token_usage.model_name != request.implementation_id:
            mismatches.append("audit implementation identity")
        if mismatches:
            raise ProbeReconciliationError(
                "signed audit evidence mismatch: " + ", ".join(mismatches)
            )
        return record

    @staticmethod
    def _is_metadata_only_capture(record: NodeAuditRecord) -> bool:
        capture = record.execution_metadata.get("audit_capture")
        if not isinstance(capture, dict):
            return False
        dropped = capture.get("dropped_fields")
        execution = dropped.get("execution_metadata") if isinstance(dropped, dict) else None
        return (
            capture.get("classification") == "metadata_only"
            and capture.get("content_retained") is False
            and isinstance(execution, dict)
            and isinstance(execution.get("count"), int)
            and execution["count"] > 0
            and execution.get("dropped_keys") == execution["count"]
            and isinstance(execution.get("hmac_sha256"), str)
            and len(execution["hmac_sha256"]) == 64
        )

    @staticmethod
    def _event(request: ProbeReconciliationRequest, audit: NodeAuditRecord) -> ExecutionEvent:
        capability_id = capability_identity(
            request.tenant_id, request.deployment_ref, request.capability_id
        )
        implementation_id = implementation_identity(capability_id, request.implementation_id)
        usage = audit.token_usage
        latency_ms = 0
        if audit.completed_at is not None:
            latency_ms = max(0, int((audit.completed_at - audit.started_at).total_seconds() * 1000))
        metadata = {
            "campaign_id": request.campaign_id,
            "operation_id": request.operation_id,
            "run_id": request.run_id,
            "provider_request_id": request.provider_request_id,
            "cleanup_status": "complete",
            "probe": True,
            "prompt_tokens": usage.input_tokens if usage else None,
            "completion_tokens": usage.output_tokens if usage else None,
            "probe_capability_id": request.capability_id,
            "probe_implementation_id": request.implementation_id,
        }
        return ExecutionEvent(
            execution_id=request.cost_event_id,
            join_key=request.run_id,
            tenant_id=request.tenant_id,
            campaign_id=request.campaign_id,
            operation_id=request.operation_id,
            deployment_ref=request.deployment_ref,
            evidence_kind="production",
            provider_request_id=request.provider_request_id,
            cleanup_status="complete",
            timestamp=audit.completed_at or audit.started_at,
            capability_id=capability_id,
            implementation_id=implementation_id,
            model_version=request.implementation_id,
            token_cost_usd=_money(request.actual_cost_usd),
            cost_measurement=MeasurementState(request.cost_measurement),
            usage_measurement=(
                MeasurementState.MEASURED if usage is not None else MeasurementState.UNMEASURED
            ),
            latency_ms=latency_ms,
            metadata=metadata,
        )

    def _stored_event(self, request: ProbeReconciliationRequest) -> StoredExecutionEvent | None:
        with self._sessions() as raw:
            db = self._scope(raw, request.tenant_id)
            row = db.execute(
                select(StoredExecutionEvent).where(
                    StoredExecutionEvent.execution_id == request.cost_event_id
                )
            ).scalar_one_or_none()
            if row is not None:
                raw.expunge(row)
            return row

    @staticmethod
    def _verify_stored_event(stored: StoredExecutionEvent, event: ExecutionEvent) -> None:
        expected = {
            "campaign_id": event.campaign_id,
            "operation_id": event.operation_id,
            "deployment_ref": event.deployment_ref,
            "evidence_kind": event.evidence_kind,
            "provider_request_id": event.provider_request_id,
            "cleanup_status": event.cleanup_status,
            "execution_id": event.execution_id,
            "join_key": event.join_key,
            "capability_id": event.capability_id,
            "implementation_id": event.implementation_id,
            "model_version": event.model_version,
            "cost_measurement": event.cost_measurement.value,
            "usage_measurement": event.usage_measurement.value,
            "latency_ms": event.latency_ms,
            # Ingestion binds tenant ownership into metadata even when the SDK
            # payload omits it, so compare against that canonical stored form.
            "event_metadata": event.metadata | {"tenant_id": event.tenant_id},
        }
        mismatches = [name for name, value in expected.items() if getattr(stored, name) != value]
        if _money(stored.token_cost_usd or 0) != _money(event.token_cost_usd or 0):
            mismatches.append("token_cost_usd")
        if stored.tool_cost_usd is not None or stored.compute_cost_usd is not None:
            mismatches.append("non-token cost")
        if mismatches:
            raise ProbeReconciliationError(
                "existing execution event mismatch: " + ", ".join(mismatches)
            )

    async def _verified_evidence(self, request: ProbeReconciliationRequest) -> _VerifiedEvidence:
        self._reservation(request)
        audit = await self._audit(request)
        return _VerifiedEvidence(audit=audit, event=self._event(request, audit))

    async def deliver_verified_event(
        self, request: ProbeReconciliationRequest
    ) -> Literal["inserted", "already_present"]:
        """Deliver only the event, leaving the reservation ambiguous.

        Exposed for operators that need to resume after a process interruption;
        normal callers should use :meth:`reconcile`.
        """
        evidence = await self._verified_evidence(request)
        existing = self._stored_event(request)
        if existing is not None:
            self._verify_stored_event(existing, evidence.event)
            return "already_present"
        with self._sessions() as raw:
            register_probe_economics(
                self._scope(raw, request.tenant_id),
                tenant_id=request.tenant_id,
                deployment_ref=request.deployment_ref,
                capability_name=request.capability_id,
                implementation_name=request.implementation_id,
            )
        self._regulus.track_execution_confirmed(evidence.event)
        stored = self._stored_event(request)
        if stored is None:
            raise ProbeReconciliationError(
                "Regulus delivery returned without durable execution event"
            )
        self._verify_stored_event(stored, evidence.event)
        return "inserted"

    async def reconcile(self, request: ProbeReconciliationRequest) -> ProbeReconciliationResult:
        delivery = await self.deliver_verified_event(request)
        with self._sessions() as raw:
            db = self._scope(raw, request.tenant_id)
            reservation = reconcile_cost(
                db,
                operation_id=request.operation_id,
                actual_cost_usd=request.actual_cost_usd,
                cost_measurement=request.cost_measurement,
                provider_request_id=request.provider_request_id,
                cleanup_status="complete",
            )
            db.add(
                AuditLog(
                    tenant_id=request.tenant_id,
                    actor_sub=request.actor_sub,
                    action="operator_reconcile_probe_delivery",
                    entity_type="cost_reservation",
                    entity_id=request.operation_id,
                    payload={
                        "campaign_id": request.campaign_id,
                        "run_id": request.run_id,
                        "cost_event_id": request.cost_event_id,
                        "provider_request_id": request.provider_request_id,
                        "actual_cost_usd": str(_money(request.actual_cost_usd)),
                        "released_cost_usd": str(_money(reservation.released_cost_usd)),
                        "delivery": delivery,
                        "source": "signed_node_audit",
                    },
                    created_at=datetime.now(UTC).replace(tzinfo=None),
                )
            )
            db.commit()
            released = _money(reservation.released_cost_usd)
        return ProbeReconciliationResult(
            operation_id=request.operation_id,
            cost_event_id=request.cost_event_id,
            delivery=delivery,
            actual_cost_usd=_money(request.actual_cost_usd),
            released_cost_usd=released,
        )
