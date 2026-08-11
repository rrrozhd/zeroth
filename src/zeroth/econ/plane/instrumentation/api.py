from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from zeroth.econ.plane.auth.deps import (
    get_current_scoped_db,
    require_claimed_tenant,
    require_roles,
)
from zeroth.econ.plane.auth.schemas import UserClaims
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.econ.plane.instrumentation.schemas import (
    ExecutionEventCreate,
    IngestResult,
    OutcomeBatchIngestRequest,
    OutcomeEventCreate,
    OutcomeQueryResponse,
)
from zeroth.econ.plane.instrumentation.service import ingest_execution, ingest_outcome, query_outcomes

router = APIRouter(tags=["instrumentation", "outcomes"])


def _outcome_out(row: object) -> OutcomeQueryResponse:
    occurred_at = getattr(row, "occurred_at", None) or getattr(row, "outcome_timestamp")
    payload = getattr(row, "outcome_payload_json", None) or {}
    if "value" not in payload and getattr(row, "outcome_value", "") != "":
        payload["value"] = getattr(row, "outcome_value")
    return OutcomeQueryResponse.model_validate(
        {
            "id": getattr(row, "id"),
            "tenant_id": getattr(row, "tenant_id", "tenant_default"),
            "join_key": getattr(row, "join_key", None) or getattr(row, "execution_id"),
            "capability_id": getattr(row, "capability_id"),
            "implementation_id": getattr(row, "implementation_id", None),
            "outcome_type": getattr(row, "outcome_type"),
            "outcome_payload_json": payload,
            "occurred_at": occurred_at,
            "provenance": getattr(row, "provenance", "MEASURED"),
        }
    )


@router.post("/instrumentation/executions", response_model=IngestResult)
def post_execution(
    payload: ExecutionEventCreate,
    db: ScopedSession = Depends(get_current_scoped_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst")),
) -> IngestResult:
    metadata = dict(payload.metadata)
    metadata_tenant = metadata.get("tenant_id")
    requested_tenant = payload.tenant_id or (
        str(metadata_tenant) if metadata_tenant is not None else None
    )
    tenant_id = require_claimed_tenant(_user, requested_tenant)
    metadata["tenant_id"] = tenant_id
    payload = payload.model_copy(update={"tenant_id": tenant_id, "metadata": metadata})
    try:
        status, row = ingest_execution(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return IngestResult(status=status, execution_id=row.execution_id)


@router.post("/instrumentation/outcomes", response_model=IngestResult)
def post_outcome(
    payload: OutcomeEventCreate,
    db: ScopedSession = Depends(get_current_scoped_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst")),
) -> IngestResult:
    payload = payload.model_copy(
        update={"tenant_id": require_claimed_tenant(_user, payload.tenant_id)}
    )
    try:
        row = ingest_outcome(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return IngestResult(status="inserted", execution_id=row.execution_id)


@router.post("/outcomes/ingest", response_model=list[IngestResult])
def ingest_outcome_batch(
    payload: OutcomeBatchIngestRequest,
    db: ScopedSession = Depends(get_current_scoped_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst")),
) -> list[IngestResult]:
    out: list[IngestResult] = []
    for event in payload.events:
        event = event.model_copy(
            update={"tenant_id": require_claimed_tenant(_user, event.tenant_id)}
        )
        try:
            row = ingest_outcome(db, event)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        out.append(IngestResult(status="inserted", execution_id=row.execution_id))
    return out


@router.get("/outcomes/query", response_model=list[OutcomeQueryResponse])
def get_outcomes(
    capability_id: str | None = None,
    implementation_id: str | None = None,
    outcome_type: str | None = None,
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    db: ScopedSession = Depends(get_current_scoped_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),
) -> list[OutcomeQueryResponse]:
    rows = query_outcomes(db, capability_id=capability_id, implementation_id=implementation_id, outcome_type=outcome_type, start=start, end=end)
    return [_outcome_out(row) for row in rows]
