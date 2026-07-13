from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from zeroth.econ_plane.auth.deps import require_roles
from zeroth.econ_plane.auth.schemas import UserClaims
from zeroth.econ_plane.common.schemas import APIMessage
from zeroth.econ_plane.counterfactual.schemas import EvaluationRunRequest, ValueEstimateOut
from zeroth.econ_plane.counterfactual.tasks import run_evaluation_async
from zeroth.econ_plane.counterfactual.service import estimate_history, latest_estimate, run_evaluation
from zeroth.econ_plane.database import get_db

router = APIRouter(tags=["counterfactual"])


@router.post("/evaluations/run", response_model=ValueEstimateOut)
def run(
    payload: EvaluationRunRequest,
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst")),
) -> ValueEstimateOut:
    estimate = run_evaluation(db, payload)
    return ValueEstimateOut.model_validate(estimate)


@router.post("/evaluations/run/async", response_model=APIMessage)
def run_async(
    payload: EvaluationRunRequest,
    _user: UserClaims = Depends(require_roles("Admin", "Analyst")),
) -> APIMessage:
    run_evaluation_async.send(payload.model_dump(mode="json"))
    return APIMessage(message="queued")


@router.get("/evaluations/{capability_id}/latest", response_model=ValueEstimateOut)
def latest(
    capability_id: str,
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),
) -> ValueEstimateOut:
    estimate = latest_estimate(db, capability_id)
    if estimate is None:
        raise HTTPException(status_code=404, detail="No estimate found")
    return ValueEstimateOut.model_validate(estimate)


@router.get("/evaluations/{capability_id}/history", response_model=list[ValueEstimateOut])
def history(
    capability_id: str,
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),
) -> list[ValueEstimateOut]:
    return [ValueEstimateOut.model_validate(e) for e in estimate_history(db, capability_id)]
