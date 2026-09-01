from fastapi import APIRouter, Depends, HTTPException, Response

from zeroth.econ.plane.auth.deps import get_current_scoped_db, require_roles
from zeroth.econ.plane.auth.scoped import ScopedUserClaims as UserClaims
from zeroth.econ.plane.costing.models import GroundTruthCost
from zeroth.econ.plane.costing.service import add_ground_truth_rows, compute_calibration_summary
from zeroth.econ.plane.reconciliation.schemas import (
    GroundTruthImportRequest,
    ProviderBillImportRequest,
    ProviderBillOut,
    ProviderBillReport,
)
from zeroth.econ.plane.reconciliation.service import (
    import_provider_bill,
    provider_bill_buckets,
    provider_bill_report,
)
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.econ.plane.statistics.schemas import CalibrationSummary

router = APIRouter(tags=["reconciliation"])


def _provider_bill_out(db: ScopedSession, bill) -> ProviderBillOut:
    return ProviderBillOut(
        statement_id=bill.statement_id,
        provider=bill.provider,
        period_start=bill.period_start,
        period_end=bill.period_end,
        currency=bill.currency,
        billed_total_usd=bill.billed_total_usd,
        source_kind=bill.source_kind,
        bucket_count=len(provider_bill_buckets(db, provider_bill_id=bill.id)),
        statement_digest=bill.statement_digest,
        imported_at=bill.imported_at,
    )


@router.post("/reconciliation/ground-truth-import")
def import_ground_truth(
    payload: GroundTruthImportRequest,
    db: ScopedSession = Depends(get_current_scoped_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst")),
) -> dict[str, int]:
    rows = [GroundTruthCost(**row.model_dump()) for row in payload.rows]
    inserted = add_ground_truth_rows(db, rows)
    return {"inserted": inserted}


@router.get("/reconciliation/calibration-summary", response_model=list[CalibrationSummary])
def calibration_summary(
    db: ScopedSession = Depends(get_current_scoped_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),
) -> list[CalibrationSummary]:
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


@router.post(
    "/reconciliation/provider-bills",
    response_model=ProviderBillOut,
    status_code=201,
)
def post_provider_bill(
    payload: ProviderBillImportRequest,
    response: Response,
    db: ScopedSession = Depends(get_current_scoped_db),  # noqa: B008
    _user: UserClaims = Depends(require_roles("Admin")),  # noqa: B008
) -> ProviderBillOut:
    try:
        created, bill = import_provider_bill(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not created:
        response.status_code = 200
    return _provider_bill_out(db, bill)


@router.get(
    "/reconciliation/provider-bills/{provider}/{statement_id}/report",
    response_model=ProviderBillReport,
)
def get_provider_bill_report(
    provider: str,
    statement_id: str,
    db: ScopedSession = Depends(get_current_scoped_db),  # noqa: B008
    _user: UserClaims = Depends(  # noqa: B008
        require_roles("Admin", "Analyst", "Approver")  # noqa: B008
    ),
) -> ProviderBillReport:
    try:
        report = provider_bill_report(db, provider=provider, statement_id=statement_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if report is None:
        raise HTTPException(status_code=404, detail="Provider bill not found")
    return report
