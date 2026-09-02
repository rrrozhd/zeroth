from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
import pytest

from zeroth.econ.analytics.service_auth import mint_econ_service_token
from zeroth.econ.plane.auth.deps import get_current_scoped_db
from zeroth.econ.plane.cloud.auth import get_cloud_scoped_db
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.database import Base
from zeroth.econ.plane.decisioning.models import ProbabilisticMigrationDecisionRecord
from zeroth.econ.plane.reports.api import get_report_mailer, router as reports_router
from zeroth.econ.plane.reports.mailer import ReportEmail
from zeroth.econ.plane.reports.models import DecisionReportDeliveryRecord, DecisionReportRecord
from zeroth.econ.plane.reports.pdf import render_decision_report_pdf
from zeroth.econ.plane.reports.schemas import DecisionReportDeliveryCreate
from zeroth.econ.plane.reports.service import (
    ReportConfigurationError,
    create_decision_report,
    deliver_decision_report,
)
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext


def _stored_decision(tenant_id: str = "tenant-a") -> ProbabilisticMigrationDecisionRecord:
    return ProbabilisticMigrationDecisionRecord(
        decision_id="pdec_report_example",
        tenant_id=tenant_id,
        request_digest="a" * 64,
        workload="support-triage",
        incumbent_model="model-a",
        candidate_model="model-b",
        verdict="recommend",
        recommended_action="hybrid_route",
        evidence_json={
            "incumbent": [{"case_id": "case-1"}],
            "candidate": [{"case_id": "case-1"}],
            "period_request_counts": [100_000],
        },
        evidence_lineage_json={"kind": "telemetry", "paired_cases": 1842},
        policy_json={
            "max_constraint_breach_probability": 0.05,
            "cvar_confidence": 0.95,
            "max_cvar_loss_usd": "2000.00",
        },
        report_json={
            "workload": "support-triage",
            "incumbent_model": "model-a",
            "candidate_model": "model-b",
            "verdict": "recommend",
            "recommended_action": "hybrid_route",
            "recommended_candidate_share": 0.25,
            "recommended_routing": {},
            "reason_codes": ["risk_constraints_satisfied"],
            "additional_cases_required": 0,
            "simulations": 10_000,
            "seed": 17,
            "actions": [
                {
                    "action_id": "global",
                    "candidate_share": 0.25,
                    "cohort_candidate_shares": {},
                    "expected_monthly_cost_usd": "81600.00",
                    "monthly_cost_p05_usd": "75300.00",
                    "monthly_cost_p95_usd": "88800.00",
                    "expected_monthly_savings_usd": "18400.00",
                    "monthly_savings_p05_usd": "11200.00",
                    "monthly_savings_p95_usd": "24700.00",
                    "probability_negative_savings": 0.01,
                    "expected_success_rate": 0.97,
                    "success_rate_p05": 0.96,
                    "success_rate_p95": 0.98,
                    "expected_p95_latency_ms": 1050.0,
                    "p95_latency_p05_ms": 980.0,
                    "p95_latency_p95_ms": 1180.0,
                    "expected_critical_error_rate": 0.01,
                    "critical_error_rate_p05": 0.005,
                    "critical_error_rate_p95": 0.02,
                    "probability_quality_breach": 0.021,
                    "probability_latency_breach": 0.008,
                    "probability_critical_error_breach": 0.014,
                    "value_at_risk_usd": "900.00",
                    "cvar_loss_usd": "1200.00",
                    "minimum_quality_drop_tolerance": 0.01,
                    "minimum_p95_latency_limit_ms": 1180,
                    "minimum_critical_error_rate_limit": 0.02,
                    "minimum_cvar_loss_limit_usd": "1200.00",
                    "feasible": True,
                    "violated_constraints": [],
                }
            ],
            "forecast_readiness": {
                "calibration_state": "calibrated",
                "drift_state": "stable",
                "interval_coverage": 0.93,
                "relative_bias": 0.02,
                "relative_residual_shift": 0.04,
                "calibration_periods": 8,
                "metrics": [],
                "missing_metrics": [],
            },
            "evidence_lineage": {"kind": "telemetry", "paired_cases": 1842},
        },
        evaluated_at=datetime(2026, 9, 2, 14, 30, tzinfo=UTC),
        evaluated_by="analyst@example.com",
    )


def test_pdf_renderer_produces_a_versioned_decision_artifact() -> None:
    pdf = render_decision_report_pdf(_stored_decision())

    assert pdf.startswith(b"%PDF-")
    assert len(pdf) > 2_000
    assert b"/Count 1" in pdf


class _RecordingMailer:
    def __init__(self) -> None:
        self.messages: list[ReportEmail] = []

    def send(self, message: ReportEmail) -> None:
        self.messages.append(message)


class _FailingMailer:
    def send(self, _message: ReportEmail) -> None:
        raise OSError("mail relay unavailable")


def test_report_api_creates_downloads_and_emails_one_immutable_artifact(
    tmp_path: Path, monkeypatch
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'reports.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(_stored_decision())
        db.commit()

    app = FastAPI()
    app.include_router(reports_router, prefix="/v1")

    def scoped_db():
        with Session(engine) as db:
            yield ScopedSession(db, TenantWideScopeContext(tenant_id="tenant-a"))

    mailer = _RecordingMailer()
    app.dependency_overrides[get_current_scoped_db] = scoped_db
    app.dependency_overrides[get_cloud_scoped_db] = scoped_db
    app.dependency_overrides[get_report_mailer] = lambda: mailer
    monkeypatch.setattr(settings, "service_principal_tenant_id", "tenant-a")
    monkeypatch.setattr(settings, "cloud_entitlements_enabled", False)
    token = mint_econ_service_token()
    assert token is not None
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)

    created = client.post("/v1/decisions/pdec_report_example/reports", headers=headers, json={})
    repeated = client.post("/v1/decisions/pdec_report_example/reports", headers=headers, json={})

    assert created.status_code == 201, created.text
    assert repeated.status_code == 200, repeated.text
    report = created.json()
    assert repeated.json()["report_id"] == report["report_id"]
    assert report["decision_id"] == "pdec_report_example"
    assert report["media_type"] == "application/pdf"
    assert len(report["sha256"]) == 64
    assert report["download_path"] == f"/v1/reports/{report['report_id']}"

    downloaded = client.get(report["download_path"], headers=headers)
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"] == "application/pdf"
    assert downloaded.headers["etag"] == f'"{report["sha256"]}"'
    assert downloaded.content.startswith(b"%PDF-")

    delivered = client.post(
        f"/v1/reports/{report['report_id']}/deliveries",
        headers=headers,
        json={
            "recipients": ["owner@example.com"],
            "delivery_mode": "attachment",
        },
    )
    assert delivered.status_code == 201, delivered.text
    assert delivered.json()["status"] == "sent"
    assert delivered.json()["report_sha256"] == report["sha256"]
    assert len(mailer.messages) == 1
    assert mailer.messages[0].attachment == downloaded.content
    assert mailer.messages[0].recipients == ("owner@example.com",)
    assert "Route 25%" in mailer.messages[0].subject

    monkeypatch.setattr(settings, "report_public_base_url", "")
    invalid_link = client.post(
        f"/v1/reports/{report['report_id']}/deliveries",
        headers=headers,
        json={"recipients": ["owner@example.com"], "delivery_mode": "link"},
    )
    assert invalid_link.status_code == 503
    assert invalid_link.json()["detail"] == "report public base URL must be an HTTPS origin"

    with Session(engine) as db:
        stored_report = db.scalar(select(DecisionReportRecord))
        stored_delivery = db.scalar(select(DecisionReportDeliveryRecord))
        assert stored_report is not None
        assert stored_report.pdf_bytes == downloaded.content
        assert stored_delivery is not None
        assert stored_delivery.report_sha256 == stored_report.sha256


def test_report_api_does_not_reveal_another_tenants_decision(tmp_path: Path, monkeypatch) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'reports-isolation.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(_stored_decision(tenant_id="tenant-b"))
        db.commit()

    app = FastAPI()
    app.include_router(reports_router, prefix="/v1")

    def scoped_db():
        with Session(engine) as db:
            yield ScopedSession(db, TenantWideScopeContext(tenant_id="tenant-a"))

    app.dependency_overrides[get_current_scoped_db] = scoped_db
    app.dependency_overrides[get_cloud_scoped_db] = scoped_db
    monkeypatch.setattr(settings, "service_principal_tenant_id", "tenant-a")
    token = mint_econ_service_token()
    assert token is not None

    response = TestClient(app).post(
        "/v1/decisions/pdec_report_example/reports",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )

    assert response.status_code == 404


def test_report_delivery_failure_is_audited_and_returns_bad_gateway(
    tmp_path: Path, monkeypatch
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'reports-failure.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(_stored_decision())
        db.commit()

    app = FastAPI()
    app.include_router(reports_router, prefix="/v1")

    def scoped_db():
        with Session(engine) as db:
            yield ScopedSession(db, TenantWideScopeContext(tenant_id="tenant-a"))

    app.dependency_overrides[get_current_scoped_db] = scoped_db
    app.dependency_overrides[get_cloud_scoped_db] = scoped_db
    app.dependency_overrides[get_report_mailer] = lambda: _FailingMailer()
    monkeypatch.setattr(settings, "service_principal_tenant_id", "tenant-a")
    token = mint_econ_service_token()
    assert token is not None
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app, raise_server_exceptions=False)
    report = client.post(
        "/v1/decisions/pdec_report_example/reports", headers=headers, json={}
    ).json()

    response = client.post(
        f"/v1/reports/{report['report_id']}/deliveries",
        headers=headers,
        json={"recipients": ["owner@example.com"], "delivery_mode": "attachment"},
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "decision report email delivery failed"
    with Session(engine) as db:
        delivery = db.scalar(select(DecisionReportDeliveryRecord))
        assert delivery is not None
        assert delivery.status == "failed"
        assert delivery.last_error == "mail relay unavailable"


def test_link_delivery_rejects_an_insecure_public_origin(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'reports-link.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as raw:
        raw.add(_stored_decision())
        raw.commit()
        db = ScopedSession(raw, TenantWideScopeContext(tenant_id="tenant-a"))
        report, _created = create_decision_report(
            db, "pdec_report_example", created_by="analyst@example.com"
        )

        with pytest.raises(ReportConfigurationError, match="HTTPS origin"):
            deliver_decision_report(
                db,
                report.report_id,
                DecisionReportDeliveryCreate(
                    recipients=["owner@example.com"], delivery_mode="link"
                ),
                mailer=_RecordingMailer(),
                created_by="analyst@example.com",
                public_base_url="http://reports.example.com",
            )


def test_abstention_email_asks_for_evidence_instead_of_routing_zero_percent(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'reports-abstain.db'}")
    Base.metadata.create_all(engine)
    decision = _stored_decision()
    decision.verdict = "abstain"
    decision.recommended_action = "collect_evidence"
    decision.report_json = {
        **decision.report_json,
        "verdict": "abstain",
        "recommended_action": "collect_evidence",
        "recommended_candidate_share": 0,
        "actions": [],
        "reason_codes": ["insufficient_paired_cases"],
        "additional_cases_required": 380,
    }
    decision_id = decision.decision_id
    mailer = _RecordingMailer()
    with Session(engine) as raw:
        raw.add(decision)
        raw.commit()
    with Session(engine) as raw:
        db = ScopedSession(raw, TenantWideScopeContext(tenant_id="tenant-a"))
        report, _created = create_decision_report(db, decision_id, created_by="analyst@example.com")
        deliver_decision_report(
            db,
            report.report_id,
            DecisionReportDeliveryCreate(
                recipients=["owner@example.com"], delivery_mode="attachment"
            ),
            mailer=mailer,
            created_by="analyst@example.com",
            public_base_url="",
        )

    assert mailer.messages[0].subject == ("Zeroth decision: Collect evidence for support-triage")
    assert "Additional cases required: 380" in mailer.messages[0].text_body
