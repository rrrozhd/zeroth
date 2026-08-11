from fastapi import APIRouter, Depends

from zeroth.econ.plane.auth.deps import get_current_scoped_db, require_roles
from zeroth.econ.plane.auth.scoped import ScopedUserClaims as UserClaims
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.econ.plane.performance.schemas import CapabilityPerformance, PerformanceSummary
from zeroth.econ.plane.performance.service import calculate_snapshots, latest_snapshots

router = APIRouter(tags=["performance"])


@router.get("/performance/capabilities", response_model=list[CapabilityPerformance])
def capabilities(
    db: ScopedSession = Depends(get_current_scoped_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst")),
) -> list[CapabilityPerformance]:
    snapshots = latest_snapshots(db)
    if not snapshots:
        snapshots = calculate_snapshots(db)
    return [CapabilityPerformance.model_validate(s) for s in snapshots]


@router.get("/performance/summary", response_model=PerformanceSummary)
def summary(
    db: ScopedSession = Depends(get_current_scoped_db),
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
