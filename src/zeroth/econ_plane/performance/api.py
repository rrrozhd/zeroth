from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from zeroth.econ_plane.auth.deps import require_roles
from zeroth.econ_plane.auth.schemas import UserClaims
from zeroth.econ_plane.database import get_db
from zeroth.econ_plane.performance.schemas import CapabilityPerformance, PerformanceSummary
from zeroth.econ_plane.performance.service import calculate_snapshots, latest_snapshots

router = APIRouter(tags=["performance"])


@router.get("/performance/capabilities", response_model=list[CapabilityPerformance])
def capabilities(
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst")),
) -> list[CapabilityPerformance]:
    snapshots = latest_snapshots(db)
    if not snapshots:
        snapshots = calculate_snapshots(db)
    return [CapabilityPerformance.model_validate(s) for s in snapshots]


@router.get("/performance/summary", response_model=PerformanceSummary)
def summary(
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst")),
) -> PerformanceSummary:
    snapshots = latest_snapshots(db)
    if not snapshots:
        snapshots = calculate_snapshots(db)
    if not snapshots:
        return PerformanceSummary(capabilities=0, avg_aer=0.0, avg_risk_adjusted_return=0.0, avg_operational_drag=0.0)

    n = len(snapshots)
    return PerformanceSummary(
        capabilities=n,
        avg_aer=sum(float(s.aer) for s in snapshots) / n,
        avg_risk_adjusted_return=sum(float(s.risk_adjusted_return) for s in snapshots) / n,
        avg_operational_drag=sum(float(s.operational_drag) for s in snapshots) / n,
    )
