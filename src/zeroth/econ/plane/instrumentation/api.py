from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from zeroth.econ.plane.auth.deps import (
    get_current_scoped_db,
    require_claimed_tenant,
    require_roles,
)
from zeroth.econ.plane.auth.scoped import ScopedUserClaims as UserClaims
from zeroth.econ.plane.instrumentation.schemas import (
    ExecutionEventCreate,
    IngestResult,
    OutcomeBatchIngestRequest,
    OutcomeEventCreate,
    OutcomeQueryResponse,
)
from zeroth.econ.plane.instrumentation.service import (
    ingest_execution,
    ingest_outcome_with_status,
    ingest_outcomes,
    query_outcomes,
)
from zeroth.econ.plane.scoped_session import ScopedSession

router = APIRouter(tags=["instrumentation", "outcomes"])


def _outcome_out(row: object) -> OutcomeQueryResponse:
    occurred_at = getattr(row, "occurred_at", None) or row.outcome_timestamp
    payload = getattr(row, "outcome_payload_json", None) or {}
    if "value" not in payload and getattr(row, "outcome_value", "") != "":
        payload["value"] = row.outcome_value
    return OutcomeQueryResponse.model_validate(
        {
            "id": row.id,
            "tenant_id": row.tenant_id,
            "join_key": getattr(row, "join_key", None) or row.execution_id,
            "capability_id": row.capability_id,
            "implementation_id": getattr(row, "implementation_id", None),
            "outcome_type": row.outcome_type,
            "outcome_payload_json": payload,
            "occurred_at": occurred_at,
            "provenance": getattr(row, "provenance", "MEASURED"),
        }
    )


@router.post("/instrumentation/executions", response_model=IngestResult)
def post_execution(
    payload: ExecutionEventCreate,
    db: ScopedSession = Depends(get_current_scoped_db),  # noqa: B008
    _user: UserClaims = Depends(require_roles("Admin", "Analyst")),  # noqa: B008
) -> IngestResult:
    metadata = dict(payload.metadata)
    metadata_tenant = metadata.get("tenant_id")
    require_claimed_tenant(_user, payload.tenant_id)
    require_claimed_tenant(
        _user, str(metadata_tenant) if metadata_tenant is not None else None
    )
    tenant_id = _user.tenant_id
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
    db: ScopedSession = Depends(get_current_scoped_db),  # noqa: B008
    _user: UserClaims = Depends(require_roles("Admin", "Analyst")),  # noqa: B008
) -> IngestResult:
    payload = payload.model_copy(
        update={"tenant_id": require_claimed_tenant(_user, payload.tenant_id)}
    )
    try:
        status, row = ingest_outcome_with_status(db, payload)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return IngestResult(status=status, execution_id=row.execution_id)


@router.post("/outcomes/ingest", response_model=list[IngestResult])
def ingest_outcome_batch(
    payload: OutcomeBatchIngestRequest,
    db: ScopedSession = Depends(get_current_scoped_db),  # noqa: B008
    _user: UserClaims = Depends(require_roles("Admin", "Analyst")),  # noqa: B008
) -> list[IngestResult]:
    # The whole batch is one transaction (see service.ingest_outcomes).  Claim
    # checks run first so a cross-tenant event is refused before anything is
    # staged, and a rejection anywhere leaves the batch entirely uncommitted --
    # a 422 now means "none of these landed", which the caller can act on.
    try:
        events = [
            event.model_copy(update={"tenant_id": require_claimed_tenant(_user, event.tenant_id)})
            for event in payload.events
        ]
        results = ingest_outcomes(db, events)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return [
        IngestResult(status=status, execution_id=row.execution_id) for status, row in results
    ]


@router.get("/outcomes/query", response_model=list[OutcomeQueryResponse])
def get_outcomes(
    capability_id: str | None = None,
    implementation_id: str | None = None,
    outcome_type: str | None = None,
    start: datetime | None = Query(default=None),  # noqa: B008
    end: datetime | None = Query(default=None),  # noqa: B008
    db: ScopedSession = Depends(get_current_scoped_db),  # noqa: B008
    _user: UserClaims = Depends(  # noqa: B008
        require_roles("Admin", "Analyst", "Approver", "Viewer")  # noqa: B008
    ),
) -> list[OutcomeQueryResponse]:
    rows = query_outcomes(
        db,
        capability_id=capability_id,
        implementation_id=implementation_id,
        outcome_type=outcome_type,
        start=start,
        end=end,
    )
    return [_outcome_out(row) for row in rows]
