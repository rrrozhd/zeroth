from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from zeroth.econ.plane.costing.models import GroundTruthCost
from zeroth.econ.plane.costing.service import add_ground_truth_rows, compute_calibration_summary
from zeroth.econ.plane.database import get_db
from zeroth.econ.plane.reconciliation.schemas import GroundTruthImportRequest
from zeroth.econ.plane.statistics.schemas import CalibrationSummary

router = APIRouter(tags=["reconciliation"])


@router.post("/reconciliation/ground-truth-import")
def import_ground_truth(payload: GroundTruthImportRequest, db: Session = Depends(get_db)) -> dict[str, int]:
    rows = [GroundTruthCost(**row.model_dump()) for row in payload.rows]
    inserted = add_ground_truth_rows(db, rows)
    return {"inserted": inserted}


@router.get("/reconciliation/calibration-summary", response_model=list[CalibrationSummary])
def calibration_summary(db: Session = Depends(get_db)) -> list[CalibrationSummary]:
    rows = compute_calibration_summary(db)
    return [
        CalibrationSummary(
            capability_id=r.capability_id,
            mape=float(r.mape),
            mae=float(r.mae),
            rmse=float(r.rmse),
            interval_coverage=float(r.interval_coverage),
            bias=float(r.bias),
            sample_size=int(r.sample_size),
        )
        for r in rows
    ]
