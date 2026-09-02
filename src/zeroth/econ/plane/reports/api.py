from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status

from zeroth.econ.plane.auth.scoped import ScopedUserClaims as UserClaims
from zeroth.econ.plane.cloud.auth import get_cloud_scoped_db, require_cloud_roles
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.reports.mailer import (
    ConfiguredSmtpReportMailer,
    ReportMailer,
    ReportMailerUnavailable,
)
from zeroth.econ.plane.reports.schemas import (
    DecisionReportCreate,
    DecisionReportDeliveryCreate,
    DecisionReportDeliveryOut,
    DecisionReportOut,
)
from zeroth.econ.plane.reports.service import (
    create_decision_report,
    deliver_decision_report,
    get_decision_report,
    ReportDeliveryError,
    ReportConfigurationError,
)
from zeroth.econ.plane.scoped_session import ScopedSession

router = APIRouter(tags=["economic-decision-reports"])


def get_report_mailer() -> ReportMailer:
    return ConfiguredSmtpReportMailer()


def _report_out(record) -> DecisionReportOut:  # type: ignore[no-untyped-def]
    return DecisionReportOut(
        report_id=record.report_id,
        decision_id=record.decision_id,
        template_version=record.template_version,
        media_type=record.media_type,
        sha256=record.sha256,
        download_path=f"/v1/reports/{record.report_id}",
        created_at=record.created_at,
    )


@router.post(
    "/decisions/{decision_id}/reports",
    response_model=DecisionReportOut,
    status_code=status.HTTP_201_CREATED,
)
def create_report(
    decision_id: str,
    _payload: DecisionReportCreate,
    response: Response,
    db: ScopedSession = Depends(get_cloud_scoped_db),  # noqa: B008
    user: UserClaims = Depends(require_cloud_roles("Admin", "Analyst", "Approver")),  # noqa: B008
) -> DecisionReportOut:
    try:
        record, created = create_decision_report(db, decision_id, created_by=user.sub)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not created:
        response.status_code = status.HTTP_200_OK
    return _report_out(record)


@router.get("/reports/{report_id}")
def download_report(
    report_id: str,
    db: ScopedSession = Depends(get_cloud_scoped_db),  # noqa: B008
    _user: UserClaims = Depends(  # noqa: B008
        require_cloud_roles("Admin", "Analyst", "Approver", "Viewer")
    ),
) -> Response:
    record = get_decision_report(db, report_id)
    if record is None:
        raise HTTPException(status_code=404, detail="decision report not found")
    return Response(
        content=record.pdf_bytes,
        media_type=record.media_type,
        headers={
            "ETag": f'"{record.sha256}"',
            "Content-Disposition": f'attachment; filename="zeroth-{record.decision_id}.pdf"',
            "X-Zeroth-Decision-Id": record.decision_id,
        },
    )


@router.post(
    "/reports/{report_id}/deliveries",
    response_model=DecisionReportDeliveryOut,
    status_code=status.HTTP_201_CREATED,
)
def deliver_report(
    report_id: str,
    payload: DecisionReportDeliveryCreate,
    db: ScopedSession = Depends(get_cloud_scoped_db),  # noqa: B008
    user: UserClaims = Depends(require_cloud_roles("Admin", "Analyst", "Approver")),  # noqa: B008
    mailer: ReportMailer = Depends(get_report_mailer),  # noqa: B008
) -> DecisionReportDeliveryOut:
    try:
        row = deliver_decision_report(
            db,
            report_id,
            payload,
            mailer=mailer,
            created_by=user.sub,
            public_base_url=settings.report_public_base_url,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ReportMailerUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ReportConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ReportDeliveryError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return DecisionReportDeliveryOut(
        delivery_id=row.delivery_id,
        report_id=row.report_id,
        report_sha256=row.report_sha256,
        recipients=row.recipients_json,
        delivery_mode=row.delivery_mode,
        status=row.status,
        last_error=row.last_error,
        created_at=row.created_at,
        sent_at=row.sent_at,
    )
