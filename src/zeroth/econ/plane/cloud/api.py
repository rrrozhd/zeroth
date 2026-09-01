"""Authenticated SDK-compatible evidence ingestion routes."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal, Union

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from zeroth.econ.measurement import MeasurementState
from zeroth.econ.plane.auth.scoped import ScopedUserClaims as UserClaims
from zeroth.econ.plane.cloud.auth import get_cloud_scoped_db, require_cloud_roles
from zeroth.econ.plane.cloud.entitlements import EntitlementError, release_usage, reserve_usage
from zeroth.econ.plane.cloud.schemas import SdkExecutionEvent, SdkOutcomeEvent
from zeroth.econ.plane.instrumentation.schemas import (
    ExecutionEventCreate,
    IngestResult,
)
from zeroth.econ.plane.instrumentation.models import ExecutionEvent, OutcomeEvent
from zeroth.econ.plane.instrumentation.service import (
    ingest_execution,
    ingest_outcome_with_status,
)
from zeroth.econ.plane.scoped_session import ScopedSession

router = APIRouter(tags=["zeroth-cloud-sdk"])


class _CloudOutcomeCreate(BaseModel):
    """Internal adapter that keeps arbitrary SDK outcomes off the legacy ABI."""

    tenant_id: str
    execution_id: str | None = None
    join_key: str
    capability_id: str
    implementation_id: str
    outcome_type: str = Field(min_length=1, max_length=64)
    outcome_value: Union[float, bool, str] | None = None
    outcome_payload_json: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime
    outcome_timestamp: datetime
    provenance: Literal["MEASURED", "INFERRED", "MIXED"] = "MEASURED"


def _stable_execution_id(payload: SdkExecutionEvent) -> str:
    if payload.event_id is not None:
        return payload.event_id
    identity = json.dumps(
        {
            "workflow": payload.workflow,
            "workflow_version": payload.workflow_version,
            "run_id": payload.run_id,
            "step": payload.step,
            "attempt": payload.attempt,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f"sdk_{hashlib.sha256(identity).hexdigest()[:32]}"


@router.post("/executions", response_model=IngestResult)
def record_execution(
    payload: SdkExecutionEvent,
    db: ScopedSession = Depends(get_cloud_scoped_db),  # noqa: B008
    user: UserClaims = Depends(require_cloud_roles("Admin", "Analyst")),  # noqa: B008
) -> IngestResult:
    execution_id = _stable_execution_id(payload)
    already_recorded = db.scalars(
        select(ExecutionEvent.id).where(ExecutionEvent.execution_id == execution_id)
    ).first()
    try:
        reserved = False if already_recorded is not None else reserve_usage(db, "events")
    except EntitlementError as exc:
        raise HTTPException(status_code=402, detail=exc.detail) from exc
    metadata = dict(payload.metadata)
    metadata.update({"step": payload.step, "attempt": payload.attempt})
    if payload.subject_id is not None:
        metadata["subject_id"] = payload.subject_id
    if payload.dimensions:
        metadata["dimensions"] = payload.dimensions
    metadata["tenant_id"] = user.tenant_id
    measured = payload.cost_measurement != "unmeasured"
    event = ExecutionEventCreate(
        tenant_id=user.tenant_id,
        execution_id=execution_id,
        join_key=payload.run_id,
        timestamp=payload.recorded_at,
        capability_id=payload.workflow,
        implementation_id=payload.workflow_version,
        model_version=payload.model_version,
        token_cost_usd=payload.cost_usd if measured else None,
        tool_cost_usd=None,
        compute_cost_usd=None,
        cost_measurement=MeasurementState(payload.cost_measurement),
        usage_measurement=MeasurementState.UNMEASURED,
        latency_ms=payload.latency_ms,
        metadata=metadata,
    )
    try:
        status, row = ingest_execution(db, event)
    except ValueError as exc:
        db.rollback()
        if reserved:
            release_usage(db, "events")
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        if reserved:
            release_usage(db, "events")
        raise HTTPException(status_code=409, detail="execution identity conflict; retry") from exc
    if reserved and status != "inserted":
        release_usage(db, "events")
    return IngestResult(status=status, execution_id=row.execution_id)


@router.post("/outcomes", response_model=IngestResult)
def record_outcome(
    payload: SdkOutcomeEvent,
    db: ScopedSession = Depends(get_cloud_scoped_db),  # noqa: B008
    user: UserClaims = Depends(require_cloud_roles("Admin", "Analyst")),  # noqa: B008
) -> IngestResult:
    already_recorded = db.scalars(
        select(OutcomeEvent.id).where(
            OutcomeEvent.join_key == payload.run_id,
            OutcomeEvent.capability_id == payload.workflow,
            OutcomeEvent.implementation_id == payload.workflow_version,
            OutcomeEvent.outcome_type == payload.outcome_type,
            OutcomeEvent.occurred_at == payload.occurred_at,
        )
    ).first()
    try:
        reserved = False if already_recorded is not None else reserve_usage(db, "events")
    except EntitlementError as exc:
        raise HTTPException(status_code=402, detail=exc.detail) from exc
    outcome_payload: dict[str, object] = {
        "accepted": payload.accepted,
        "metadata": payload.metadata,
    }
    if payload.value_usd is not None:
        outcome_payload["value_usd"] = str(payload.value_usd)
    if payload.score is not None:
        outcome_payload["score"] = payload.score
    if payload.subject_id is not None:
        outcome_payload["subject_id"] = payload.subject_id
    if payload.dimensions:
        outcome_payload["dimensions"] = payload.dimensions
    event = _CloudOutcomeCreate(
        tenant_id=user.tenant_id,
        join_key=payload.run_id,
        capability_id=payload.workflow,
        implementation_id=payload.workflow_version,
        outcome_type=payload.outcome_type,
        outcome_value=payload.accepted,
        outcome_payload_json=outcome_payload,
        occurred_at=payload.occurred_at,
        outcome_timestamp=payload.occurred_at,
        provenance=payload.provenance.upper(),
    )
    try:
        status, row = ingest_outcome_with_status(db, event)
    except ValueError as exc:
        db.rollback()
        if reserved:
            release_usage(db, "events")
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        if reserved:
            release_usage(db, "events")
        raise HTTPException(status_code=409, detail="outcome identity conflict; retry") from exc
    if reserved and status != "inserted":
        release_usage(db, "events")
    return IngestResult(status=status, execution_id=row.execution_id)
