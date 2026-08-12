from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from zeroth.econ.plane.capabilities.service import active_experiment, pick_ab_arm
from zeroth.econ.plane.connectors.service import enqueue_connector_event
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.common.tenant import resolve_tenant_id
from zeroth.econ.plane.instrumentation.models import ExecutionEvent, OutcomeEvent
from zeroth.econ.plane.instrumentation.schemas import ExecutionEventCreate, OutcomeEventCreate


def _derive_join_key_from_metadata(metadata: dict) -> str:
    for key in ("request_id", "trace_id", "run_id"):
        value = metadata.get(key)
        if value:
            return str(value)
    return ""


def ingest_execution(db: Session, payload: ExecutionEventCreate) -> tuple[str, ExecutionEvent]:
    metadata = dict(payload.metadata)
    join_key = payload.join_key or _derive_join_key_from_metadata(metadata) or payload.execution_id
    if settings.strict_join_key_enforcement and not join_key:
        raise ValueError("join_key is required for execution ingestion")

    existing = db.execute(select(ExecutionEvent).where(ExecutionEvent.execution_id == payload.execution_id)).scalar_one_or_none()
    if existing:
        if existing.join_key != join_key:
            raise ValueError("execution_id already exists with a different join_key")
        return "duplicate", existing

    tenant_id = resolve_tenant_id(payload.tenant_id or metadata.get("tenant_id"))
    experiment = active_experiment(db, payload.capability_id, mode="AB")
    if experiment is not None and join_key:
        assignment_input = join_key
        if experiment.assignment_key == "user_id":
            assignment_input = str(metadata.get("user_id", join_key))
        arm = pick_ab_arm(assignment_input, experiment.target_pct)
        metadata.update(
            {
                "experiment_id": experiment.id,
                "assigned_arm": arm,
                "assignment_key_type": experiment.assignment_key,
                "assignment_input_hash": assignment_input,
            }
        )

    row = ExecutionEvent(
        tenant_id=tenant_id,
        execution_id=payload.execution_id,
        join_key=join_key,
        timestamp=payload.timestamp,
        capability_id=payload.capability_id,
        implementation_id=payload.implementation_id,
        model_version=payload.model_version,
        token_cost_usd=payload.token_cost_usd,
        tool_cost_usd=payload.tool_cost_usd,
        compute_cost_usd=payload.compute_cost_usd,
        cost_measurement=payload.cost_measurement.value,
        usage_measurement=payload.usage_measurement.value,
        latency_ms=payload.latency_ms,
        compute_time_ms=payload.compute_time_ms,
        event_metadata=metadata,
    )
    db.add(row)
    if settings.connectors_enabled:
        try:
            enqueue_connector_event(
                db,
                tenant_id=tenant_id,
                event_type="execution.event",
                event_key=payload.execution_id,
                join_key=join_key,
                capability_id=payload.capability_id,
                implementation_id=payload.implementation_id,
                payload={
                    "execution_id": payload.execution_id,
                    "timestamp": payload.timestamp.isoformat(),
                    "model_version": payload.model_version,
                    "token_cost_usd": str(payload.token_cost_usd),
                    "tool_cost_usd": str(payload.tool_cost_usd),
                    "compute_cost_usd": str(payload.compute_cost_usd),
                    "cost_measurement": payload.cost_measurement.value,
                    "usage_measurement": payload.usage_measurement.value,
                    "latency_ms": payload.latency_ms,
                    "compute_time_ms": payload.compute_time_ms,
                    "metadata": metadata,
                },
            )
        except Exception:  # noqa: BLE001
            # Connector path is best-effort and must not fail ingestion.
            pass
    db.commit()
    db.refresh(row)
    return "inserted", row


def ingest_outcome(db: Session, payload: OutcomeEventCreate) -> OutcomeEvent:
    tenant_id = resolve_tenant_id(payload.tenant_id)
    linked_execution = None
    if payload.execution_id:
        linked_execution = db.execute(select(ExecutionEvent).where(ExecutionEvent.execution_id == payload.execution_id)).scalar_one_or_none()

    join_key = payload.join_key or (linked_execution.join_key if linked_execution else "") or payload.execution_id or ""
    if settings.strict_join_key_enforcement and not join_key:
        raise ValueError("join_key is required for outcome ingestion")
    if linked_execution is not None and join_key != linked_execution.join_key:
        raise ValueError("outcome join_key does not match execution join_key")
    if linked_execution is not None and payload.capability_id != linked_execution.capability_id:
        raise ValueError("outcome capability_id does not match execution capability_id")
    if (
        linked_execution is not None
        and payload.implementation_id is not None
        and payload.implementation_id != linked_execution.implementation_id
    ):
        raise ValueError("outcome implementation_id does not match execution implementation_id")

    occurred_at = payload.occurred_at or payload.outcome_timestamp or datetime.now(timezone.utc)
    outcome_payload = dict(payload.outcome_payload_json)
    if payload.outcome_value is not None and "value" not in outcome_payload:
        outcome_payload["value"] = payload.outcome_value

    row = OutcomeEvent(
        tenant_id=tenant_id,
        join_key=join_key,
        execution_id=payload.execution_id or join_key,
        capability_id=payload.capability_id,
        implementation_id=payload.implementation_id,
        outcome_type=payload.outcome_type,
        outcome_payload_json=outcome_payload,
        outcome_value=str(payload.outcome_value) if payload.outcome_value is not None else "",
        occurred_at=occurred_at,
        ingested_at=datetime.now(timezone.utc),
        outcome_timestamp=payload.outcome_timestamp or occurred_at,
        provenance=payload.provenance,
    )
    db.add(row)
    if settings.connectors_enabled:
        try:
            enqueue_connector_event(
                db,
                tenant_id=tenant_id,
                event_type="outcome.event",
                event_key=f"{join_key}:{occurred_at.isoformat()}:{payload.outcome_type}",
                join_key=join_key,
                capability_id=payload.capability_id,
                implementation_id=payload.implementation_id,
                payload={
                    "execution_id": payload.execution_id,
                    "outcome_type": payload.outcome_type,
                    "outcome_value": payload.outcome_value,
                    "outcome_payload_json": outcome_payload,
                    "occurred_at": occurred_at.isoformat(),
                    "provenance": payload.provenance,
                },
            )
        except Exception:  # noqa: BLE001
            pass
    db.commit()
    db.refresh(row)
    return row


def query_outcomes(
    db: Session,
    capability_id: str | None = None,
    implementation_id: str | None = None,
    outcome_type: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[OutcomeEvent]:
    stmt = select(OutcomeEvent)
    if capability_id:
        stmt = stmt.where(OutcomeEvent.capability_id == capability_id)
    if implementation_id:
        stmt = stmt.where(OutcomeEvent.implementation_id == implementation_id)
    if outcome_type:
        stmt = stmt.where(OutcomeEvent.outcome_type == outcome_type)
    if start:
        stmt = stmt.where(OutcomeEvent.occurred_at >= start)
    if end:
        stmt = stmt.where(OutcomeEvent.occurred_at <= end)
    return list(db.execute(stmt.order_by(OutcomeEvent.id.desc())).scalars())
