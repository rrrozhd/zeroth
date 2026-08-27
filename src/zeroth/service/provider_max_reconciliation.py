"""Signed, fail-closed reconciliation of unmeasurable provider outcomes.

This recovery path never calls a provider.  It converts one exact unmeasured
Regulus placeholder and its matching ambiguous reservation into an estimated
charge at the already-held maximum.  The reservation, execution event, and
economics operator log change in one SQL transaction; the service audit is an
append-only signed follow-up and is safely resumable if the process stops
between the two durable stores.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from zeroth.econ.plane.enforcement.models import AuditLog, CostReservation
from zeroth.econ.plane.instrumentation.models import ExecutionEvent
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.governance.audit.errors import DuplicateAuditIdError
from zeroth.governance.audit.models import NodeAuditRecord
from zeroth.governance.audit.verifier import AuditContinuityVerifier
from zeroth.governance.identity import ActorIdentity, AuthMethod, ServiceRole
from zeroth.platform.storage.scoping import TenantWideScopeContext


class ProviderMaxReconciliationError(RuntimeError):
    """Exact durable evidence was absent, unsafe, or internally inconsistent."""


def _required(value: str, name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} is required")
    return normalized


def _money(value: Decimal | float | str) -> Decimal:
    amount = Decimal(str(value))
    if not amount.is_finite() or amount <= 0:
        raise ValueError("held maximum cost must be finite and positive")
    return amount.quantize(Decimal("0.00000001"))


def _nonnegative_money(value: Decimal | float | str) -> Decimal:
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError("cost amount must be finite and non-negative")
    return amount.quantize(Decimal("0.00000001"))


@dataclass(frozen=True, slots=True)
class ProviderMaxReconciliationRequest:
    """Every asserted identity required to select one provider call exactly."""

    tenant_id: str
    campaign_id: str
    operation_id: str
    run_id: str
    deployment_ref: str
    node_id: str
    capability_id: str
    implementation_id: str
    model_version: str
    cost_event_id: str
    held_max_cost_usd: Decimal
    actor_sub: str
    reason: str

    def __post_init__(self) -> None:
        for name in (
            "tenant_id",
            "campaign_id",
            "operation_id",
            "run_id",
            "deployment_ref",
            "node_id",
            "capability_id",
            "implementation_id",
            "model_version",
            "cost_event_id",
            "actor_sub",
            "reason",
        ):
            _required(getattr(self, name), name)
        _money(self.held_max_cost_usd)


@dataclass(frozen=True, slots=True)
class ProviderMaxReconciliationResult:
    operation_id: str
    cost_event_id: str
    state: Literal["reconciled", "already_reconciled"]
    actual_cost_usd: Decimal
    released_cost_usd: Decimal
    provider_request_id: None
    operator_audit: Literal["appended", "already_present", "unsupported"]


class AmbiguousProviderMaxReconciler:
    """Resolve one ambiguous provider call at maximum without re-executing it."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        audit_repository: Any,
        audit_signer: Any,
    ) -> None:
        if audit_repository is None or audit_signer is None:
            raise ValueError("signed audit repository and signer are required")
        self._sessions = session_factory
        self._audits = audit_repository
        self._signer = audit_signer

    @staticmethod
    def _scope(raw: Session, tenant_id: str) -> ScopedSession:
        return ScopedSession(raw, TenantWideScopeContext(tenant_id=tenant_id))

    @staticmethod
    def _operator_audit_id(request: ProviderMaxReconciliationRequest) -> str:
        identity = "\x00".join(
            (request.tenant_id, request.operation_id, request.cost_event_id)
        ).encode()
        return f"audit_reconcile_{hashlib.sha256(identity).hexdigest()[:32]}"

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
    def _one_reservation(
        db: ScopedSession, request: ProviderMaxReconciliationRequest
    ) -> CostReservation:
        row = db.execute(
            select(CostReservation).where(
                CostReservation.tenant_id == request.tenant_id,
                CostReservation.operation_id == request.operation_id,
            )
        ).scalar_one_or_none()
        if row is None:
            raise ProviderMaxReconciliationError("exact reservation was not found")
        return row

    @staticmethod
    def _one_event(db: ScopedSession, request: ProviderMaxReconciliationRequest) -> ExecutionEvent:
        row = db.execute(
            select(ExecutionEvent).where(
                ExecutionEvent.tenant_id == request.tenant_id,
                ExecutionEvent.execution_id == request.cost_event_id,
            )
        ).scalar_one_or_none()
        if row is None:
            raise ProviderMaxReconciliationError("exact execution placeholder was not found")
        return row

    @staticmethod
    def _identity_mismatches(
        row: CostReservation | ExecutionEvent,
        request: ProviderMaxReconciliationRequest,
    ) -> list[str]:
        expected = {
            "tenant_id": request.tenant_id,
            "campaign_id": request.campaign_id,
            "operation_id": request.operation_id,
            "deployment_ref": request.deployment_ref,
            "evidence_kind": "production",
            "cost_event_id": request.cost_event_id,
            "provider_request_id": None,
        }
        if isinstance(row, ExecutionEvent):
            expected.pop("cost_event_id")
            expected |= {
                "execution_id": request.cost_event_id,
                "join_key": request.run_id,
                "capability_id": request.capability_id,
                "implementation_id": request.implementation_id,
                "model_version": request.model_version,
            }
        else:
            expected["run_id"] = request.run_id
        return [name for name, value in expected.items() if getattr(row, name) != value]

    @classmethod
    def _reservation_state(
        cls, row: CostReservation, request: ProviderMaxReconciliationRequest
    ) -> Literal["ambiguous", "committed"]:
        mismatches = cls._identity_mismatches(row, request)
        maximum = _money(request.held_max_cost_usd)
        if _money(row.max_cost_usd) != maximum:
            mismatches.append("max_cost_usd")
        if _money(row.held_cost_usd) != maximum:
            mismatches.append("held_cost_usd")
        if row.status == "ambiguous":
            if row.cleanup_status != "pending_reconciliation":
                mismatches.append("cleanup_status")
            if row.actual_cost_usd is not None:
                mismatches.append("actual_cost_usd")
            if row.cost_measurement != "unmeasured":
                mismatches.append("cost_measurement")
        elif row.status == "committed":
            if row.cleanup_status != "complete":
                mismatches.append("cleanup_status")
            if row.actual_cost_usd is None or _money(row.actual_cost_usd) != maximum:
                mismatches.append("actual_cost_usd")
            if row.cost_measurement != "estimated":
                mismatches.append("cost_measurement")
            if _nonnegative_money(row.released_cost_usd or 0) != Decimal("0.00000000"):
                mismatches.append("released_cost_usd")
        else:
            mismatches.append("status")
        if mismatches:
            raise ProviderMaxReconciliationError(
                "reservation evidence mismatch: " + ", ".join(dict.fromkeys(mismatches))
            )
        return row.status  # type: ignore[return-value]

    @classmethod
    def _event_state(
        cls, row: ExecutionEvent, request: ProviderMaxReconciliationRequest
    ) -> Literal["placeholder", "estimated"]:
        mismatches = cls._identity_mismatches(row, request)
        metadata_expected = {
            "tenant_id": request.tenant_id,
            "campaign_id": request.campaign_id,
            "operation_id": request.operation_id,
            "run_id": request.run_id,
            "provider_request_id": None,
        }
        mismatches.extend(
            f"event_metadata.{name}"
            for name, value in metadata_expected.items()
            if (row.event_metadata or {}).get(name) != value
        )
        if row.tool_cost_usd is not None or row.compute_cost_usd is not None:
            mismatches.append("non-token cost")
        maximum = _money(request.held_max_cost_usd)
        if row.cost_measurement == "unmeasured":
            if row.cleanup_status != "pending_reconciliation":
                mismatches.append("cleanup_status")
            if row.token_cost_usd is not None:
                mismatches.append("token_cost_usd")
            if row.usage_measurement != "unmeasured":
                mismatches.append("usage_measurement")
            state: Literal["placeholder", "estimated"] = "placeholder"
        elif row.cost_measurement == "estimated":
            if row.cleanup_status != "complete":
                mismatches.append("cleanup_status")
            if row.token_cost_usd is None or _money(row.token_cost_usd) != maximum:
                mismatches.append("token_cost_usd")
            if row.usage_measurement != "unmeasured":
                mismatches.append("usage_measurement")
            if (row.event_metadata or {}).get("operator_reconciliation") != "held_maximum":
                mismatches.append("event_metadata.operator_reconciliation")
            state = "estimated"
        else:
            mismatches.append("cost_measurement")
            state = "placeholder"
        if mismatches:
            raise ProviderMaxReconciliationError(
                "execution event evidence mismatch: " + ", ".join(dict.fromkeys(mismatches))
            )
        return state

    async def _source_audit(
        self, request: ProviderMaxReconciliationRequest
    ) -> NodeAuditRecord:
        report = await AuditContinuityVerifier(self._audits, signer=self._signer).verify_run(
            request.run_id,
            tenant_id=request.tenant_id,
        )
        if not report.verified or report.signature_verified is not True:
            raise ProviderMaxReconciliationError("signed audit verification failed")
        records = await self._audits.list_by_run(
            request.run_id,
            tenant_id=request.tenant_id,
        )
        expected_audit_id = f"audit_{request.cost_event_id}"
        matches = [record for record in records if record.audit_id == expected_audit_id]
        if len(matches) != 1:
            raise ProviderMaxReconciliationError("expected exactly one ambiguous provider audit")
        record = matches[0]
        expected = {
            "tenant_id": request.tenant_id,
            "campaign_id": request.campaign_id,
            "run_id": request.run_id,
            "deployment_ref": request.deployment_ref,
            "node_id": request.node_id,
            "status": "ambiguous",
            "cost_event_id": request.cost_event_id,
        }
        mismatches = [name for name, value in expected.items() if getattr(record, name) != value]
        metadata_expected = {
            "campaign_id": request.campaign_id,
            "operation_id": request.operation_id,
            "run_id": request.run_id,
            "implementation_id": request.model_version,
            "cost_measurement": "unmeasured",
            "cleanup_status": "pending_reconciliation",
            "provider_request_id": None,
        }
        if not all(
            record.execution_metadata.get(name) == value
            for name, value in metadata_expected.items()
        ) and not self._is_metadata_only_capture(record):
            mismatches.append("execution_metadata")
        if record.cost_usd is not None or record.estimated_cost_usd is not None:
            mismatches.append("audit cost")
        if record.cost_measurement is None or record.cost_measurement.value != "unmeasured":
            mismatches.append("cost_measurement")
        if record.completed_at is None:
            mismatches.append("completed_at")
        if mismatches:
            raise ProviderMaxReconciliationError(
                "signed audit evidence mismatch: " + ", ".join(dict.fromkeys(mismatches))
            )
        return record

    @staticmethod
    def _verify_econ_log(row: AuditLog, request: ProviderMaxReconciliationRequest) -> None:
        expected = {
            "tenant_id": request.tenant_id,
            "actor_sub": request.actor_sub,
            "action": "operator_reconcile_ambiguous_provider_max",
            "entity_type": "cost_reservation",
            "entity_id": request.operation_id,
        }
        mismatches = [name for name, value in expected.items() if getattr(row, name) != value]
        payload = row.payload or {}
        payload_expected = {
            "campaign_id": request.campaign_id,
            "run_id": request.run_id,
            "deployment_ref": request.deployment_ref,
            "cost_event_id": request.cost_event_id,
            "provider_request_id": None,
            "actual_cost_usd": str(_money(request.held_max_cost_usd)),
            "released_cost_usd": "0.00000000",
            "cost_measurement": "estimated",
            "cleanup_status": "complete",
            "resolution": "held_maximum",
        }
        mismatches.extend(
            f"payload.{name}"
            for name, value in payload_expected.items()
            if payload.get(name) != value
        )
        if mismatches:
            raise ProviderMaxReconciliationError(
                "existing economics operator audit mismatch: " + ", ".join(mismatches)
            )

    def _settle_economics(
        self, request: ProviderMaxReconciliationRequest
    ) -> Literal["reconciled", "already_reconciled"]:
        maximum = _money(request.held_max_cost_usd)
        with self._sessions() as raw:
            db = self._scope(raw, request.tenant_id)
            reservation = self._one_reservation(db, request)
            event = self._one_event(db, request)
            reservation_state = self._reservation_state(reservation, request)
            event_state = self._event_state(event, request)
            if reservation_state == "committed" or event_state == "estimated":
                if (reservation_state, event_state) != ("committed", "estimated"):
                    raise ProviderMaxReconciliationError(
                        "reservation and execution event reconciliation states disagree"
                    )
                logs = list(
                    db.execute(
                        select(AuditLog).where(
                            AuditLog.tenant_id == request.tenant_id,
                            AuditLog.action == "operator_reconcile_ambiguous_provider_max",
                            AuditLog.entity_type == "cost_reservation",
                            AuditLog.entity_id == request.operation_id,
                        )
                    ).scalars()
                )
                if len(logs) != 1:
                    raise ProviderMaxReconciliationError(
                        "committed reconciliation requires exactly one economics operator audit"
                    )
                self._verify_econ_log(logs[0], request)
                return "already_reconciled"

            now = datetime.now(UTC).replace(tzinfo=None)
            # The raw session remains scope-bound after ``ScopedSession`` is
            # constructed; use its CursorResult only for the affected-row count.
            reservation_update = raw.execute(
                update(CostReservation)
                .where(
                    CostReservation.tenant_id == request.tenant_id,
                    CostReservation.operation_id == request.operation_id,
                    CostReservation.status == "ambiguous",
                    CostReservation.cleanup_status == "pending_reconciliation",
                    CostReservation.provider_request_id.is_(None),
                    CostReservation.actual_cost_usd.is_(None),
                )
                .values(
                    status="committed",
                    actual_cost_usd=maximum,
                    held_cost_usd=maximum,
                    released_cost_usd=Decimal("0.00000000"),
                    cost_measurement="estimated",
                    cleanup_status="complete",
                    updated_at=now,
                )
            )
            if reservation_update.rowcount != 1:
                raw.rollback()
                raise ProviderMaxReconciliationError(
                    "reservation changed concurrently; inspect authoritative outcome before retry"
                )
            metadata = dict(event.event_metadata or {})
            metadata.update(
                {
                    "cleanup_status": "complete",
                    "operator_reconciliation": "held_maximum",
                    "cost_measurement": "estimated",
                }
            )
            event_update = raw.execute(
                update(ExecutionEvent)
                .where(
                    ExecutionEvent.id == event.id,
                    ExecutionEvent.tenant_id == request.tenant_id,
                    ExecutionEvent.execution_id == request.cost_event_id,
                    ExecutionEvent.cost_measurement == "unmeasured",
                    ExecutionEvent.cleanup_status == "pending_reconciliation",
                    ExecutionEvent.provider_request_id.is_(None),
                    ExecutionEvent.token_cost_usd.is_(None),
                )
                .values(
                    token_cost_usd=maximum,
                    cost_measurement="estimated",
                    usage_measurement="unmeasured",
                    cleanup_status="complete",
                    event_metadata=metadata,
                )
            )
            if event_update.rowcount != 1:
                raw.rollback()
                raise ProviderMaxReconciliationError(
                    "execution placeholder changed concurrently; no reconciliation committed"
                )
            existing_logs = list(
                db.execute(
                    select(AuditLog).where(
                        AuditLog.tenant_id == request.tenant_id,
                        AuditLog.action == "operator_reconcile_ambiguous_provider_max",
                        AuditLog.entity_type == "cost_reservation",
                        AuditLog.entity_id == request.operation_id,
                    )
                ).scalars()
            )
            if existing_logs:
                raw.rollback()
                raise ProviderMaxReconciliationError(
                    "economics operator audit exists before reservation reconciliation"
                )
            db.add(
                AuditLog(
                    tenant_id=request.tenant_id,
                    actor_sub=request.actor_sub,
                    action="operator_reconcile_ambiguous_provider_max",
                    entity_type="cost_reservation",
                    entity_id=request.operation_id,
                    payload={
                        "campaign_id": request.campaign_id,
                        "run_id": request.run_id,
                        "deployment_ref": request.deployment_ref,
                        "cost_event_id": request.cost_event_id,
                        "provider_request_id": None,
                        "actual_cost_usd": str(maximum),
                        "released_cost_usd": "0.00000000",
                        "cost_measurement": "estimated",
                        "cleanup_status": "complete",
                        "resolution": "held_maximum",
                        "reason": request.reason,
                        "source": "signed_ambiguous_node_audit",
                    },
                    created_at=now,
                )
            )
            raw.commit()
            return "reconciled"

    async def _append_operator_audit(
        self,
        request: ProviderMaxReconciliationRequest,
        source: NodeAuditRecord,
    ) -> Literal["appended", "already_present", "unsupported"]:
        audit_id = self._operator_audit_id(request)
        records = await self._audits.list_by_run(
            request.run_id,
            tenant_id=request.tenant_id,
        )
        existing = [record for record in records if record.audit_id == audit_id]
        if existing:
            if len(existing) != 1:
                raise ProviderMaxReconciliationError("operator audit identity is not unique")
            record = existing[0]
            metadata = record.execution_metadata
            direct_metadata_matches = (
                metadata.get("operation_id") == request.operation_id
                and metadata.get("cost_event_id") == request.cost_event_id
                and metadata.get("resolution") == "held_maximum"
            )
            if (
                record.tenant_id != request.tenant_id
                or record.campaign_id != request.campaign_id
                or record.run_id != request.run_id
                or record.deployment_ref != request.deployment_ref
                or record.node_id != "operator.cost_reconciliation"
                or record.status != "completed"
                or not (direct_metadata_matches or self._is_metadata_only_capture(record))
            ):
                raise ProviderMaxReconciliationError("existing operator audit mismatch")
            return "already_present"
        writer = getattr(self._audits, "write", None)
        if not callable(writer):
            return "unsupported"
        maximum = _money(request.held_max_cost_usd)
        now = datetime.now(UTC)
        record = NodeAuditRecord(
            audit_id=audit_id,
            run_id=request.run_id,
            thread_id=source.thread_id,
            node_id="operator.cost_reconciliation",
            graph_version_ref=source.graph_version_ref,
            deployment_ref=request.deployment_ref,
            tenant_id=request.tenant_id,
            workspace_id=source.workspace_id,
            campaign_id=request.campaign_id,
            status="completed",
            actor=ActorIdentity(
                subject=request.actor_sub,
                auth_method=AuthMethod.API_KEY,
                roles=[ServiceRole.PLATFORM_ADMIN],
                tenant_id=request.tenant_id,
                workspace_id=source.workspace_id,
            ),
            execution_metadata={
                "operation_id": request.operation_id,
                "cost_event_id": request.cost_event_id,
                "source_audit_id": source.audit_id,
                "resolution": "held_maximum",
                "actual_cost_usd": str(maximum),
                "cost_measurement": "estimated",
                "provider_request_id": None,
                "cleanup_status": "complete",
                "reason": request.reason,
            },
            cost_measurement="unmeasured",
            started_at=now,
            completed_at=now,
        )
        try:
            written = await writer(record)
        except DuplicateAuditIdError:
            return "already_present"
        if written.record_signature is None:
            raise ProviderMaxReconciliationError("operator reconciliation audit was not signed")
        report = await AuditContinuityVerifier(self._audits, signer=self._signer).verify_run(
            request.run_id,
            tenant_id=request.tenant_id,
        )
        if not report.verified or report.signature_verified is not True:
            raise ProviderMaxReconciliationError(
                "signed chain failed after operator reconciliation audit append"
            )
        return "appended"

    async def reconcile(
        self, request: ProviderMaxReconciliationRequest
    ) -> ProviderMaxReconciliationResult:
        # Verify the exact SQL identities before consulting a similarly-named
        # audit; this prevents a wrong-tenant request from learning audit state.
        with self._sessions() as raw:
            db = self._scope(raw, request.tenant_id)
            self._reservation_state(self._one_reservation(db, request), request)
            self._event_state(self._one_event(db, request), request)
        source = await self._source_audit(request)
        state = self._settle_economics(request)
        operator_audit = await self._append_operator_audit(request, source)
        return ProviderMaxReconciliationResult(
            operation_id=request.operation_id,
            cost_event_id=request.cost_event_id,
            state=state,
            actual_cost_usd=_money(request.held_max_cost_usd),
            released_cost_usd=Decimal("0.00000000"),
            provider_request_id=None,
            operator_audit=operator_audit,
        )
