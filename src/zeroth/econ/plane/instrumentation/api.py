from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.exc import IntegrityError

from zeroth.econ.plane.auth.deps import (
    get_current_scoped_db,
    require_claimed_tenant,
    require_roles,
)
from zeroth.econ.plane.auth.scoped import ScopedUserClaims as UserClaims
from zeroth.econ.plane.debugger.schemas import (
    BreakagePoint,
    CohortPoint,
    EconomicDiagnosticReport,
    OutcomeDefinitionCreate,
    OutcomeDefinitionOut,
    TimelinePoint,
)
from zeroth.econ.plane.debugger.models import OutcomeDefinition
from zeroth.econ.plane.debugger.service import (
    breakage,
    cohorts,
    create_outcome_definition,
    diagnostic_report,
    list_outcome_definitions,
    timeline,
)
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


def _definition_out(row: OutcomeDefinition) -> OutcomeDefinitionOut:
    return OutcomeDefinitionOut(
        workflow_id=row.workflow_id,
        workflow_version=row.workflow_version,
        outcome_type=row.outcome_type,
        operator=row.operator,
        target=row.target_json,
        definition_digest=row.definition_digest,
        created_at=row.created_at,
    )

#: Last resort for an identity conflict the service could not resolve into a
#: reported duplicate -- a concurrent writer that took the identity and then
#: rolled back, or a batch that lost the race twice running.  It is deliberately
#: not a 422: the payload is not what is wrong, and telling a caller its request
#: was invalid when the server lost a race sends it to fix the wrong thing.  A
#: 409 says what happened and that retrying is the remedy.
_IDENTITY_CONFLICT = "outcome identity conflicted with a concurrent write; retry the request"

#: The same last resort for an execution identity: a concurrent writer that took
#: ``(tenant_id, execution_id)`` and then rolled back, so the service's re-query
#: found nothing to report as a duplicate.  Kept as its own message because the
#: two endpoints conflict on different identities and a caller reading the detail
#: should not have to guess which.
_EXECUTION_IDENTITY_CONFLICT = (
    "execution identity conflicted with a concurrent write; retry the request"
)


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
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=_EXECUTION_IDENTITY_CONFLICT) from exc
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
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=_IDENTITY_CONFLICT) from exc
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
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=_IDENTITY_CONFLICT) from exc
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


@router.get("/debugger/timeline", response_model=list[TimelinePoint])
def get_debugger_timeline(
    workflow_id: str,
    start: datetime | None = Query(default=None),  # noqa: B008
    end: datetime | None = Query(default=None),  # noqa: B008
    db: ScopedSession = Depends(get_current_scoped_db),  # noqa: B008
    _user: UserClaims = Depends(  # noqa: B008
        require_roles("Admin", "Analyst", "Approver", "Viewer")  # noqa: B008
    ),
) -> list[TimelinePoint]:
    return timeline(db, workflow_id=workflow_id, start=start, end=end)


@router.get("/debugger/cohorts", response_model=list[CohortPoint])
def get_debugger_cohorts(
    workflow_id: str,
    group_by: Literal["subject_id", "dimension"] = "subject_id",
    dimension: str | None = None,
    start: datetime | None = Query(default=None),  # noqa: B008
    end: datetime | None = Query(default=None),  # noqa: B008
    db: ScopedSession = Depends(get_current_scoped_db),  # noqa: B008
    _user: UserClaims = Depends(  # noqa: B008
        require_roles("Admin", "Analyst", "Approver", "Viewer")  # noqa: B008
    ),
) -> list[CohortPoint]:
    try:
        return cohorts(
            db,
            workflow_id=workflow_id,
            group_by=group_by,
            dimension=dimension,
            start=start,
            end=end,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/debugger/breakage", response_model=list[BreakagePoint])
def get_debugger_breakage(
    workflow_id: str,
    start: datetime | None = Query(default=None),  # noqa: B008
    end: datetime | None = Query(default=None),  # noqa: B008
    db: ScopedSession = Depends(get_current_scoped_db),  # noqa: B008
    _user: UserClaims = Depends(  # noqa: B008
        require_roles("Admin", "Analyst", "Approver", "Viewer")  # noqa: B008
    ),
) -> list[BreakagePoint]:
    return breakage(db, workflow_id=workflow_id, start=start, end=end)


@router.post(
    "/debugger/outcome-definitions",
    response_model=OutcomeDefinitionOut,
    status_code=201,
)
def post_outcome_definition(
    payload: OutcomeDefinitionCreate,
    response: Response,
    db: ScopedSession = Depends(get_current_scoped_db),  # noqa: B008
    _user: UserClaims = Depends(require_roles("Admin")),  # noqa: B008
) -> OutcomeDefinitionOut:
    try:
        created, row = create_outcome_definition(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not created:
        response.status_code = 200
    return _definition_out(row)


@router.get("/debugger/outcome-definitions", response_model=list[OutcomeDefinitionOut])
def get_outcome_definitions(
    workflow_id: str | None = None,
    db: ScopedSession = Depends(get_current_scoped_db),  # noqa: B008
    _user: UserClaims = Depends(  # noqa: B008
        require_roles("Admin", "Analyst", "Approver", "Viewer")  # noqa: B008
    ),
) -> list[OutcomeDefinitionOut]:
    return [
        _definition_out(row)
        for row in list_outcome_definitions(db, workflow_id=workflow_id)
    ]


@router.get("/debugger/report", response_model=EconomicDiagnosticReport)
def get_debugger_report(
    workflow_id: str,
    cohort_dimension: str | None = None,
    start: datetime | None = Query(default=None),  # noqa: B008
    end: datetime | None = Query(default=None),  # noqa: B008
    db: ScopedSession = Depends(get_current_scoped_db),  # noqa: B008
    _user: UserClaims = Depends(  # noqa: B008
        require_roles("Admin", "Analyst", "Approver", "Viewer")  # noqa: B008
    ),
) -> EconomicDiagnosticReport:
    report = diagnostic_report(
        db,
        workflow_id=workflow_id,
        start=start,
        end=end,
        cohort_dimension=cohort_dimension,
    )
    if report is None:
        raise HTTPException(
            status_code=404,
            detail="No economic evidence found for this workflow and window",
        )
    return report
