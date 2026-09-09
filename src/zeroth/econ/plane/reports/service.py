from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from urllib.parse import urlsplit

from sqlalchemy.exc import IntegrityError

from zeroth.econ.plane.decisioning.models import ProbabilisticMigrationDecisionRecord
from zeroth.econ.plane.reports.mailer import (
    ReportEmail,
    ReportMailer,
    ReportMailerUnavailable,
)
from zeroth.econ.plane.reports.models import DecisionReportDeliveryRecord, DecisionReportRecord
from zeroth.econ.plane.reports.pdf import TEMPLATE_VERSION, render_decision_report_pdf
from zeroth.econ.plane.reports.schemas import DecisionReportDeliveryCreate
from zeroth.econ.plane.scoped_session import ScopedSession


def _require_scope(db: object) -> ScopedSession:
    if type(db) is not ScopedSession or db.scope is None:
        raise TypeError("decision reports require a tenant-scoped session")
    return db


class ReportDeliveryError(RuntimeError):
    """The configured transport failed after the attempt was audited."""


class ReportConfigurationError(RuntimeError):
    """The deployment cannot perform the requested delivery mode safely."""


def create_decision_report(
    db: ScopedSession, decision_id: str, *, created_by: str
) -> tuple[DecisionReportRecord, bool]:
    db = _require_scope(db)
    decision = db.get(ProbabilisticMigrationDecisionRecord, decision_id)
    if decision is None:
        raise ValueError("probabilistic decision not found")
    report_id = "rpt_" + hashlib.sha256(
        f"{db.scope.tenant_id}:{decision_id}:{TEMPLATE_VERSION}".encode()
    ).hexdigest()[:24]
    existing = db.get(DecisionReportRecord, report_id)
    if existing is not None:
        return existing, False
    pdf = render_decision_report_pdf(decision)
    record = DecisionReportRecord(
        report_id=report_id,
        tenant_id=db.scope.tenant_id,
        decision_id=decision_id,
        template_version=TEMPLATE_VERSION,
        media_type="application/pdf",
        sha256=hashlib.sha256(pdf).hexdigest(),
        pdf_bytes=pdf,
        created_at=datetime.now(UTC),
        created_by=created_by,
    )
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        concurrent = db.get(DecisionReportRecord, report_id)
        if concurrent is None:
            raise
        return concurrent, False
    return record, True


def get_decision_report(db: ScopedSession, report_id: str) -> DecisionReportRecord | None:
    return _require_scope(db).get(DecisionReportRecord, report_id)


def deliver_decision_report(
    db: ScopedSession,
    report_id: str,
    request: DecisionReportDeliveryCreate,
    *,
    mailer: ReportMailer,
    created_by: str,
    public_base_url: str,
) -> DecisionReportDeliveryRecord:
    db = _require_scope(db)
    report = db.get(DecisionReportRecord, report_id)
    if report is None:
        raise ValueError("decision report not found")
    decision = db.get(ProbabilisticMigrationDecisionRecord, report.decision_id)
    if decision is None:
        raise ValueError("probabilistic decision not found")
    if request.delivery_mode == "link":
        parsed = urlsplit(public_base_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ReportConfigurationError(
                "report public base URL must be an HTTPS origin"
            )
    now = datetime.now(UTC)
    delivery_id = "dlv_" + hashlib.sha256(
        f"{db.scope.tenant_id}:{report_id}:{now.isoformat()}:{','.join(map(str, request.recipients))}".encode()
    ).hexdigest()[:24]
    row = DecisionReportDeliveryRecord(
        delivery_id=delivery_id,
        tenant_id=db.scope.tenant_id,
        report_id=report_id,
        report_sha256=report.sha256,
        recipients_json=[str(value) for value in request.recipients],
        delivery_mode=request.delivery_mode,
        status="failed",
        last_error=None,
        created_at=now,
        sent_at=None,
        created_by=created_by,
    )
    db.add(row)
    db.flush()
    share = round(float(decision.report_json.get("recommended_candidate_share", 0)) * 100)
    headline = (
        "Collect evidence"
        if decision.recommended_action == "collect_evidence" else "Review diagnostics"
    )
    subject = f"Zeroth experimental report: {headline} for {decision.workload}"
    download_url = f"{public_base_url.rstrip('/')}/v1/reports/{report_id}"
    body = (
        "Experimental scenario diagnostics. Predictive reliability is unvalidated; "
        "these results do not authorize rollout.\n"
        f"Recorded action: {decision.recommended_action.replace('_', ' ')}\n"
        f"Recorded candidate share: {share}%\n"
        f"Decision ID: {decision.decision_id}\n"
        f"Report SHA-256: {report.sha256}\n"
    )
    additional_cases = int(decision.report_json.get("additional_cases_required", 0))
    if additional_cases:
        body += f"Estimated additional cases: {additional_cases}\n"
    attachment = report.pdf_bytes if request.delivery_mode == "attachment" else None
    if request.delivery_mode == "link":
        body += f"Authenticated report: {download_url}\n"
    try:
        mailer.send(
            ReportEmail(
                recipients=tuple(str(value) for value in request.recipients),
                subject=subject,
                text_body=body,
                attachment=attachment,
                attachment_name=f"zeroth-{decision.workload}-{decision.decision_id}.pdf",
            )
        )
    except ReportMailerUnavailable as exc:
        row.last_error = str(exc)[:512]
        db.commit()
        raise
    except Exception as exc:
        row.last_error = str(exc)[:512]
        db.commit()
        raise ReportDeliveryError("decision report email delivery failed") from exc
    row.status = "sent"
    row.sent_at = datetime.now(UTC)
    db.commit()
    db.refresh(row)
    return row
