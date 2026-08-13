from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from zeroth.econ.plane.capabilities.models import Capability, Implementation
from zeroth.econ.plane.capabilities.service import active_experiment, pick_ab_arm
from zeroth.econ.plane.connectors.service import enqueue_connector_event
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.instrumentation.models import ExecutionEvent, OutcomeEvent
from zeroth.econ.plane.instrumentation.schemas import ExecutionEventCreate, OutcomeEventCreate
from zeroth.econ.plane.scoped_session import ScopedSession


def _require_exact_scoped_session(db: object) -> ScopedSession:
    if type(db) is not ScopedSession:
        raise TypeError("instrumentation persistence requires an exact ScopedSession")
    return db


def _bound_tenant(db: ScopedSession) -> str:
    if db.scope is None:
        raise ValueError("instrumentation requires a tenant-bound scope")
    return db.scope.tenant_id


def _assert_requested_tenant(requested: str | None, tenant_id: str) -> None:
    normalized = "default" if requested == "tenant_default" else requested
    if normalized is not None and normalized != tenant_id:
        raise ValueError("tenant ownership does not match the bound scope")


def _require_capability_and_implementation(
    db: ScopedSession,
    *,
    tenant_id: str,
    capability_id: str,
    implementation_id: str | None,
) -> Capability:
    capability = db.execute(
        select(Capability).where(
            Capability.tenant_id == tenant_id,
            Capability.id == capability_id,
        )
    ).scalar_one_or_none()
    if capability is None:
        raise ValueError("capability does not exist in the bound tenant")
    if implementation_id is None:
        return capability
    implementation = db.execute(
        select(Implementation).where(
            Implementation.tenant_id == tenant_id,
            Implementation.id == implementation_id,
            Implementation.capability_id == capability_id,
        )
    ).scalar_one_or_none()
    if implementation is None:
        raise ValueError("implementation does not belong to the capability in the bound tenant")
    return capability


def _derive_join_key_from_metadata(metadata: dict) -> str:
    for key in ("request_id", "trace_id", "run_id"):
        value = metadata.get(key)
        if value:
            return str(value)
    return ""


_ASSIGNMENT_METADATA_KEYS = {
    "experiment_id",
    "assigned_arm",
    "assignment_key_type",
    "assignment_input_hash",
}


def _event_metadata_identity(metadata: dict | None) -> dict:
    return {
        key: value
        for key, value in (metadata or {}).items()
        if key not in _ASSIGNMENT_METADATA_KEYS
    }


def _datetime_identity(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _execution_conflicts(
    existing: ExecutionEvent,
    payload: ExecutionEventCreate,
    *,
    join_key: str,
    metadata: dict,
) -> list[str]:
    comparisons = {
        "execution_id": (existing.execution_id, payload.execution_id),
        "capability_id": (existing.capability_id, payload.capability_id),
        "implementation_id": (existing.implementation_id, payload.implementation_id),
        "join_key": (existing.join_key, join_key),
        "timestamp": (_datetime_identity(existing.timestamp), _datetime_identity(payload.timestamp)),
        "model_version": (existing.model_version, payload.model_version),
        "token_cost_usd": (existing.token_cost_usd, payload.token_cost_usd),
        "tool_cost_usd": (existing.tool_cost_usd, payload.tool_cost_usd),
        "compute_cost_usd": (existing.compute_cost_usd, payload.compute_cost_usd),
        "latency_ms": (existing.latency_ms, payload.latency_ms),
        "compute_time_ms": (existing.compute_time_ms, payload.compute_time_ms),
        "metadata": (
            _event_metadata_identity(existing.event_metadata),
            _event_metadata_identity(metadata),
        ),
    }
    return [field for field, (stored, requested) in comparisons.items() if stored != requested]


def ingest_execution(
    db: ScopedSession, payload: ExecutionEventCreate
) -> tuple[str, ExecutionEvent]:
    db = _require_exact_scoped_session(db)
    tenant_id = _bound_tenant(db)
    metadata = dict(payload.metadata)
    _assert_requested_tenant(payload.tenant_id, tenant_id)
    metadata_tenant = metadata.get("tenant_id")
    _assert_requested_tenant(
        str(metadata_tenant) if metadata_tenant is not None else None,
        tenant_id,
    )
    metadata["tenant_id"] = tenant_id
    join_key = payload.join_key or _derive_join_key_from_metadata(metadata) or payload.execution_id
    if settings.strict_join_key_enforcement and not join_key:
        raise ValueError("join_key is required for execution ingestion")

    existing = db.execute(
        select(ExecutionEvent).where(
            ExecutionEvent.tenant_id == tenant_id,
            ExecutionEvent.execution_id == payload.execution_id,
        )
    ).scalar_one_or_none()
    if existing:
        conflicts = _execution_conflicts(existing, payload, join_key=join_key, metadata=metadata)
        if conflicts:
            raise ValueError(
                "execution_id already exists with conflicting immutable fields: "
                + ", ".join(conflicts)
            )
        return "duplicate", existing

    _require_capability_and_implementation(
        db,
        tenant_id=tenant_id,
        capability_id=payload.capability_id,
        implementation_id=payload.implementation_id,
    )

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


def ingest_outcome(db: ScopedSession, payload: OutcomeEventCreate) -> OutcomeEvent:
    db = _require_exact_scoped_session(db)
    tenant_id = _bound_tenant(db)
    _assert_requested_tenant(payload.tenant_id, tenant_id)
    _require_capability_and_implementation(
        db,
        tenant_id=tenant_id,
        capability_id=payload.capability_id,
        implementation_id=None,
    )
    linked_execution = None
    if payload.execution_id:
        linked_execution = db.execute(
            select(ExecutionEvent).where(
                ExecutionEvent.tenant_id == tenant_id,
                ExecutionEvent.execution_id == payload.execution_id,
            )
        ).scalar_one_or_none()
        if linked_execution is None:
            raise ValueError("execution_id was not found in the bound tenant")

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
    implementation_id = payload.implementation_id or (
        linked_execution.implementation_id if linked_execution is not None else None
    )
    _require_capability_and_implementation(
        db,
        tenant_id=tenant_id,
        capability_id=payload.capability_id,
        implementation_id=implementation_id,
    )

    occurred_at = payload.occurred_at or payload.outcome_timestamp or datetime.now(timezone.utc)
    outcome_payload = dict(payload.outcome_payload_json)
    if payload.outcome_value is not None and "value" not in outcome_payload:
        outcome_payload["value"] = payload.outcome_value

    row = OutcomeEvent(
        tenant_id=tenant_id,
        join_key=join_key,
        execution_id=payload.execution_id or join_key,
        capability_id=payload.capability_id,
        implementation_id=implementation_id,
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
                implementation_id=implementation_id,
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
    db: ScopedSession,
    capability_id: str | None = None,
    implementation_id: str | None = None,
    outcome_type: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[OutcomeEvent]:
    db = _require_exact_scoped_session(db)
    tenant_id = _bound_tenant(db)
    stmt = select(OutcomeEvent).where(OutcomeEvent.tenant_id == tenant_id)
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
