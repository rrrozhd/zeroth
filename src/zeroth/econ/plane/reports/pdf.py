from __future__ import annotations

from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from zeroth.econ.plane.decisioning.models import ProbabilisticMigrationDecisionRecord

TEMPLATE_VERSION = "model-migration-v1"


def _money(value: object) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "Unavailable"


def _percent(value: object) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "Unavailable"


def _page(canvas, document) -> None:  # type: ignore[no-untyped-def]
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#596273"))
    canvas.drawString(0.65 * inch, 0.42 * inch, "Zeroth - Advisory decision report")
    canvas.drawRightString(7.85 * inch, 0.42 * inch, f"Page {document.page}")
    canvas.restoreState()


def render_decision_report_pdf(record: ProbabilisticMigrationDecisionRecord) -> bytes:
    """Render a deterministic report from a retained decision snapshot."""

    report = record.report_json
    actions = report.get("actions") or []
    selected = next(
        (
            action
            for action in actions
            if float(action.get("candidate_share", -1))
            == float(report.get("recommended_candidate_share", 0))
            and action.get("feasible")
        ),
        actions[0] if actions else {},
    )
    share = _percent(report.get("recommended_candidate_share", 0))
    recommendation = str(report.get("recommended_action", "collect_evidence")).replace("_", " ")
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="ReportTitle",
            parent=styles["Title"],
            fontName="Helvetica-Bold",
            fontSize=22,
            leading=27,
            textColor=colors.HexColor("#12233F"),
            alignment=TA_CENTER,
            spaceAfter=8,
        )
    )
    styles.add(
        ParagraphStyle(
            name="Section",
            parent=styles["Heading2"],
            textColor=colors.HexColor("#12233F"),
            spaceBefore=10,
            spaceAfter=4,
        )
    )
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=0.65 * inch,
        leftMargin=0.65 * inch,
        topMargin=0.65 * inch,
        bottomMargin=0.65 * inch,
        title=f"Zeroth model migration decision {record.decision_id}",
        author="Zeroth",
    )
    story = [
        Paragraph("Model Migration Decision", styles["ReportTitle"]),
        Paragraph(
            f"<b>{record.workload}</b><br/>{record.incumbent_model} to {record.candidate_model}",
            styles["Heading2"],
        ),
        Paragraph(
            "Advisory recommendation only. Zeroth does not alter production routing. "
            "Review the evidence, constraints, and operational context before rollout.",
            styles["BodyText"],
        ),
        Spacer(1, 8),
        Table(
            [
                ["Recommendation", recommendation.title()],
                ["Candidate traffic", share],
                ["Verdict", str(report.get("verdict", record.verdict)).title()],
                ["Expected monthly savings", _money(selected.get("expected_monthly_savings_usd"))],
                [
                    "Likely monthly savings range",
                    f"{_money(selected.get('monthly_savings_p05_usd'))} to "
                    f"{_money(selected.get('monthly_savings_p95_usd'))}",
                ],
            ],
            colWidths=[2.35 * inch, 4.55 * inch],
            style=TableStyle(
                [
                    ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#E9EEF5")),
                    ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#12233F")),
                    ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#C3CBD7")),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]
            ),
        ),
        Paragraph("Risk assessment", styles["Section"]),
        Table(
            [
                ["Measure", "Forecast", "Customer limit"],
                [
                    "Quality breach probability",
                    _percent(selected.get("probability_quality_breach")),
                    _percent(record.policy_json.get("max_constraint_breach_probability")),
                ],
                [
                    "Latency breach probability",
                    _percent(selected.get("probability_latency_breach")),
                    _percent(record.policy_json.get("max_constraint_breach_probability")),
                ],
                [
                    "Reliability breach probability",
                    _percent(selected.get("probability_critical_error_breach")),
                    _percent(record.policy_json.get("max_constraint_breach_probability")),
                ],
                [
                    f"CVaR {_percent(record.policy_json.get('cvar_confidence'))}",
                    _money(selected.get("cvar_loss_usd")),
                    _money(record.policy_json.get("max_cvar_loss_usd")),
                ],
            ],
            colWidths=[3.0 * inch, 1.95 * inch, 1.95 * inch],
            style=TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#12233F")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#C3CBD7")),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            ),
        ),
        Paragraph("Evidence and calibration", styles["Section"]),
        Paragraph(
            f"Paired cases: <b>{record.evidence_lineage_json.get('paired_cases', 'Unavailable')}</b>. "
            f"Calibration: <b>{report.get('forecast_readiness', {}).get('calibration_state', 'unknown')}</b>. "
            f"Drift: <b>{report.get('forecast_readiness', {}).get('drift_state', 'unknown')}</b>. "
            f"Monte Carlo scenarios: <b>{report.get('simulations', 'Unavailable')}</b>.",
            styles["BodyText"],
        ),
        Paragraph("Decision rationale", styles["Section"]),
        Paragraph(
            ", ".join(str(code).replace("_", " ") for code in report.get("reason_codes", []))
            or "No reason codes were recorded.",
            styles["BodyText"],
        ),
        Paragraph("Audit record", styles["Section"]),
        Table(
            [
                ["Decision ID", record.decision_id],
                ["Evaluated at", record.evaluated_at.isoformat()],
                ["Evaluated by", record.evaluated_by],
                ["Request digest", record.request_digest],
                ["Template version", TEMPLATE_VERSION],
            ],
            colWidths=[1.4 * inch, 5.5 * inch],
            style=TableStyle(
                [
                    ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#C3CBD7")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]
            ),
        ),
    ]
    document.build(story, onFirstPage=_page, onLaterPages=_page)
    return buffer.getvalue()
