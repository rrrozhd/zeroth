from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from zeroth.econ.plane.auth.deps import require_roles
from zeroth.econ.plane.auth.schemas import UserClaims
from zeroth.econ.plane.database import get_db
from zeroth.econ.plane.dashboard.schemas import (
    CapabilityRankingRow,
    CapabilityValueRow,
    ConfidenceGateStatus,
    DataQualityMix,
    ImplementationCompareRow,
    KPIResponse,
    PolicyTimelineRow,
    TrendPoint,
)
from zeroth.econ.plane.dashboard.service import (
    action_suppression,
    calibration_trend,
    capital_destroyers,
    capability_ranking,
    confidence_gate_status,
    confidence_trend,
    data_quality_mix,
    drift_timeline,
    efficiency_trend,
    estimated_vs_ground_truth_cost,
    implementation_compare,
    kpis,
    policy_timeline,
    top_creators,
)

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard/kpis", response_model=KPIResponse)
def get_kpis(
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),
) -> KPIResponse:
    return KPIResponse(**kpis(db))


@router.get("/dashboard/top-creators", response_model=list[CapabilityValueRow])
def get_top_creators(
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),
) -> list[CapabilityValueRow]:
    return [CapabilityValueRow(**r) for r in top_creators(db)]


@router.get("/dashboard/capital-destroyers", response_model=list[CapabilityValueRow])
def get_destroyers(
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),
) -> list[CapabilityValueRow]:
    return [CapabilityValueRow(**r) for r in capital_destroyers(db)]


@router.get("/dashboard/capability-ranking", response_model=list[CapabilityRankingRow])
def get_capability_ranking(
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),
) -> list[CapabilityRankingRow]:
    return [CapabilityRankingRow(**r) for r in capability_ranking(db)]


@router.get("/dashboard/implementation-compare/{capability_id}", response_model=list[ImplementationCompareRow])
def get_implementation_compare(
    capability_id: str,
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),
) -> list[ImplementationCompareRow]:
    return [ImplementationCompareRow(**r) for r in implementation_compare(db, capability_id)]


@router.get("/dashboard/policy-timeline", response_model=list[PolicyTimelineRow])
def get_policy_timeline(
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),
) -> list[PolicyTimelineRow]:
    return [PolicyTimelineRow(**r) for r in policy_timeline(db)]


@router.get("/dashboard/drift-timeline/{capability_id}", response_model=list[TrendPoint])
def get_drift_timeline(
    capability_id: str,
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),
) -> list[TrendPoint]:
    return [TrendPoint(**p) for p in drift_timeline(db, capability_id)]


@router.get("/dashboard/confidence-trend", response_model=list[TrendPoint])
def get_conf_trend(
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),
) -> list[TrendPoint]:
    return [TrendPoint(**p) for p in confidence_trend(db)]


@router.get("/dashboard/efficiency-trend", response_model=list[TrendPoint])
def get_eff_trend(
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),
) -> list[TrendPoint]:
    return [TrendPoint(**p) for p in efficiency_trend(db)]


@router.get("/dashboard/estimated-vs-ground-truth")
def get_estimated_vs_ground_truth(
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),
) -> list[dict]:
    return estimated_vs_ground_truth_cost(db)


@router.get("/dashboard/confidence-gate-status", response_model=ConfidenceGateStatus)
def get_conf_gate_status(
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),
) -> ConfidenceGateStatus:
    return ConfidenceGateStatus(**confidence_gate_status(db))


@router.get("/dashboard/data-quality-mix", response_model=DataQualityMix)
def get_data_quality_mix(
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),
) -> DataQualityMix:
    return DataQualityMix(**data_quality_mix(db))


@router.get("/dashboard/calibration-trend", response_model=list[TrendPoint])
def get_calibration_trend(
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),
) -> list[TrendPoint]:
    return [TrendPoint(**p) for p in calibration_trend(db)]


@router.get("/dashboard/action-suppression", response_model=list[TrendPoint])
def get_action_suppression(
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),
) -> list[TrendPoint]:
    return [TrendPoint(**p) for p in action_suppression(db)]
