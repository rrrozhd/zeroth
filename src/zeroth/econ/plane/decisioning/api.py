"""Authenticated API for evidence-gated workflow-version decisions."""

from fastapi import APIRouter, Depends, HTTPException

from zeroth.econ.decisioning import EconomicDecision
from zeroth.econ.plane.auth.scoped import ScopedUserClaims as UserClaims
from zeroth.econ.plane.cloud.auth import get_cloud_scoped_db, require_cloud_roles
from zeroth.econ.plane.cloud.entitlements import (
    EntitlementError,
    assert_schedule_allowed,
    release_usage,
    reserve_usage,
)
from zeroth.econ.plane.decisioning.schemas import (
    DecisionScheduleCreate,
    DecisionScheduleOut,
    VersionComparisonRequest,
)
from zeroth.econ.plane.decisioning.service import compare_versions_from_store
from zeroth.econ.plane.decisioning.service import (
    create_decision_schedule,
    list_decision_schedules,
    list_retained_decisions,
    retain_decision,
)
from zeroth.econ.plane.scoped_session import ScopedSession

router = APIRouter(tags=["economic-change-control"])


@router.post("/decisions/compare", response_model=EconomicDecision)
def compare_versions(
    payload: VersionComparisonRequest,
    db: ScopedSession = Depends(get_cloud_scoped_db),  # noqa: B008
    user: UserClaims = Depends(  # noqa: B008
        require_cloud_roles("Admin", "Analyst", "Approver", "Viewer")
    ),
) -> EconomicDecision:
    try:
        reserved = reserve_usage(db, "decision_scans")
    except EntitlementError as exc:
        raise HTTPException(status_code=402, detail=exc.detail) from exc
    try:
        decision = compare_versions_from_store(db, payload)
        return retain_decision(db, payload, decision, evaluated_by=user.sub)
    except Exception:
        if reserved:
            db.rollback()
            release_usage(db, "decision_scans")
        raise


@router.get("/decisions", response_model=list[EconomicDecision])
def decision_history(
    workflow: str | None = None,
    limit: int = 50,
    db: ScopedSession = Depends(get_cloud_scoped_db),  # noqa: B008
    _user: UserClaims = Depends(  # noqa: B008
        require_cloud_roles("Admin", "Analyst", "Approver", "Viewer")
    ),
) -> list[EconomicDecision]:
    return list_retained_decisions(db, workflow=workflow, limit=limit)


@router.post("/decision-schedules", response_model=DecisionScheduleOut)
def create_schedule(
    payload: DecisionScheduleCreate,
    db: ScopedSession = Depends(get_cloud_scoped_db),  # noqa: B008
    user: UserClaims = Depends(require_cloud_roles("Admin", "Analyst")),  # noqa: B008
) -> DecisionScheduleOut:
    try:
        assert_schedule_allowed(db, payload.interval_minutes)
    except EntitlementError as exc:
        raise HTTPException(status_code=402, detail=exc.detail) from exc
    return create_decision_schedule(db, payload, created_by=user.sub)


@router.get("/decision-schedules", response_model=list[DecisionScheduleOut])
def schedules(
    db: ScopedSession = Depends(get_cloud_scoped_db),  # noqa: B008
    _user: UserClaims = Depends(  # noqa: B008
        require_cloud_roles("Admin", "Analyst", "Approver", "Viewer")
    ),
) -> list[DecisionScheduleOut]:
    return list_decision_schedules(db)
