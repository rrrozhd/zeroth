"""Authenticated API for evidence-gated workflow-version decisions."""

from fastapi import APIRouter, Depends, HTTPException

from zeroth.econ.decisioning import EconomicDecision
from zeroth.econ.probabilistic import ProbabilisticMigrationDecision
from zeroth.econ.rollout_verification import RolloutVerification
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
    ProbabilisticMigrationRequest,
    MigrationEvidenceRefreshRequest,
    ProbabilisticDecisionScheduleCreate,
    ProbabilisticDecisionScheduleOut,
    RandomizedRolloutAssignmentCreate,
    RandomizedRolloutAssignmentOut,
    RandomizedRolloutCreate,
    RandomizedRolloutOut,
    RandomizedRolloutVerify,
    VersionComparisonRequest,
)
from zeroth.econ.plane.decisioning.service import compare_versions_from_store
from zeroth.econ.plane.decisioning.service import (
    RandomizedRolloutInactiveError,
    create_decision_schedule,
    deactivate_decision_schedule,
    deactivate_probabilistic_decision_schedule,
    evaluate_and_retain_probabilistic_migration,
    list_decision_schedules,
    list_probabilistic_migration_decisions,
    list_retained_decisions,
    retain_decision,
    assign_randomized_rollout,
    create_probabilistic_decision_schedule,
    create_randomized_rollout,
    evaluate_probabilistic_migration_from_store,
    list_probabilistic_decision_schedules,
    stop_randomized_rollout,
    verify_retained_randomized_rollout,
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


@router.post(
    "/decisions/model-migration",
    response_model=ProbabilisticMigrationDecision,
)
def evaluate_model_migration(
    payload: ProbabilisticMigrationRequest,
    db: ScopedSession = Depends(get_cloud_scoped_db),  # noqa: B008
    user: UserClaims = Depends(  # noqa: B008
        require_cloud_roles("Admin", "Analyst", "Approver", "Viewer")
    ),
) -> ProbabilisticMigrationDecision:
    try:
        reserved = reserve_usage(db, "decision_scans")
    except EntitlementError as exc:
        raise HTTPException(status_code=402, detail=exc.detail) from exc
    try:
        return evaluate_and_retain_probabilistic_migration(
            db, payload, evaluated_by=user.sub
        )
    except Exception:
        if reserved:
            db.rollback()
            release_usage(db, "decision_scans")
        raise


@router.get(
    "/decisions/model-migrations",
    response_model=list[ProbabilisticMigrationDecision],
)
def probabilistic_migration_history(
    workload: str | None = None,
    limit: int = 50,
    db: ScopedSession = Depends(get_cloud_scoped_db),  # noqa: B008
    _user: UserClaims = Depends(  # noqa: B008
        require_cloud_roles("Admin", "Analyst", "Approver", "Viewer")
    ),
) -> list[ProbabilisticMigrationDecision]:
    return list_probabilistic_migration_decisions(db, workload=workload, limit=limit)


@router.post(
    "/decisions/model-migration/refresh",
    response_model=ProbabilisticMigrationDecision,
)
def refresh_model_migration(
    payload: MigrationEvidenceRefreshRequest,
    db: ScopedSession = Depends(get_cloud_scoped_db),  # noqa: B008
    user: UserClaims = Depends(  # noqa: B008
        require_cloud_roles("Admin", "Analyst", "Approver", "Viewer")
    ),
) -> ProbabilisticMigrationDecision:
    try:
        reserved = reserve_usage(db, "decision_scans")
    except EntitlementError as exc:
        raise HTTPException(status_code=402, detail=exc.detail) from exc
    try:
        return evaluate_probabilistic_migration_from_store(
            db, payload, evaluated_by=user.sub
        )
    except Exception:
        if reserved:
            db.rollback()
            release_usage(db, "decision_scans")
        raise


@router.post(
    "/probabilistic-decision-schedules",
    response_model=ProbabilisticDecisionScheduleOut,
)
def create_probabilistic_schedule(
    payload: ProbabilisticDecisionScheduleCreate,
    db: ScopedSession = Depends(get_cloud_scoped_db),  # noqa: B008
    user: UserClaims = Depends(require_cloud_roles("Admin", "Analyst")),  # noqa: B008
) -> ProbabilisticDecisionScheduleOut:
    try:
        assert_schedule_allowed(db, payload.interval_minutes)
    except EntitlementError as exc:
        raise HTTPException(status_code=402, detail=exc.detail) from exc
    return create_probabilistic_decision_schedule(db, payload, created_by=user.sub)


@router.get(
    "/probabilistic-decision-schedules",
    response_model=list[ProbabilisticDecisionScheduleOut],
)
def probabilistic_schedules(
    db: ScopedSession = Depends(get_cloud_scoped_db),  # noqa: B008
    _user: UserClaims = Depends(  # noqa: B008
        require_cloud_roles("Admin", "Analyst", "Approver", "Viewer")
    ),
) -> list[ProbabilisticDecisionScheduleOut]:
    return list_probabilistic_decision_schedules(db)


@router.post(
    "/probabilistic-decision-schedules/{schedule_id}/deactivate",
    response_model=ProbabilisticDecisionScheduleOut,
)
def deactivate_probabilistic_schedule(
    schedule_id: str,
    db: ScopedSession = Depends(get_cloud_scoped_db),  # noqa: B008
    _user: UserClaims = Depends(require_cloud_roles("Admin", "Analyst")),  # noqa: B008
) -> ProbabilisticDecisionScheduleOut:
    try:
        return deactivate_probabilistic_decision_schedule(db, schedule_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/randomized-rollouts", response_model=RandomizedRolloutOut)
def create_rollout(
    payload: RandomizedRolloutCreate,
    db: ScopedSession = Depends(get_cloud_scoped_db),  # noqa: B008
    user: UserClaims = Depends(require_cloud_roles("Admin", "Analyst")),  # noqa: B008
) -> RandomizedRolloutOut:
    try:
        return create_randomized_rollout(db, payload, created_by=user.sub)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/randomized-rollouts/{rollout_id}/stop",
    response_model=RandomizedRolloutOut,
)
def stop_rollout(
    rollout_id: str,
    db: ScopedSession = Depends(get_cloud_scoped_db),  # noqa: B008
    _user: UserClaims = Depends(require_cloud_roles("Admin", "Analyst")),  # noqa: B008
) -> RandomizedRolloutOut:
    try:
        return stop_randomized_rollout(db, rollout_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/randomized-rollouts/{rollout_id}/assignments",
    response_model=RandomizedRolloutAssignmentOut,
)
def assign_rollout(
    rollout_id: str,
    payload: RandomizedRolloutAssignmentCreate,
    db: ScopedSession = Depends(get_cloud_scoped_db),  # noqa: B008
    _user: UserClaims = Depends(require_cloud_roles("Admin", "Analyst")),  # noqa: B008
) -> RandomizedRolloutAssignmentOut:
    try:
        return assign_randomized_rollout(
            db,
            rollout_id,
            subject_id=payload.subject_id,
            cohort=payload.cohort,
        )
    except RandomizedRolloutInactiveError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/randomized-rollouts/{rollout_id}/verify",
    response_model=RolloutVerification,
)
def verify_rollout(
    rollout_id: str,
    payload: RandomizedRolloutVerify,
    db: ScopedSession = Depends(get_cloud_scoped_db),  # noqa: B008
    user: UserClaims = Depends(require_cloud_roles("Admin", "Analyst", "Approver")),  # noqa: B008
) -> RolloutVerification:
    try:
        return verify_retained_randomized_rollout(
            db,
            rollout_id,
            outcome_type=payload.outcome_type,
            bootstrap_samples=payload.bootstrap_samples,
            seed=payload.seed,
            verified_by=user.sub,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


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


@router.post(
    "/decision-schedules/{schedule_id}/deactivate",
    response_model=DecisionScheduleOut,
)
def deactivate_schedule(
    schedule_id: str,
    db: ScopedSession = Depends(get_cloud_scoped_db),  # noqa: B008
    _user: UserClaims = Depends(require_cloud_roles("Admin", "Analyst")),  # noqa: B008
) -> DecisionScheduleOut:
    try:
        return deactivate_decision_schedule(db, schedule_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
