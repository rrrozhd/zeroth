"""Boundary mutation detectors, not a substitute for parsed-PDF end-to-end tests."""

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch


class NamedBoundaryFaultTests(unittest.TestCase):
    def test_report_tree_uses_selected_fixture_not_substituted_action(self):
        from zeroth.econ.plane.reports import pdf

        selected = dict(
            action_id="K02",
            candidate_share=1,
            feasible=True,
            expected_monthly_savings_usd="400",
            monthly_savings_p05_usd="400",
            monthly_savings_p95_usd="400",
        )
        other = dict(
            action_id="other",
            candidate_share=0.25,
            feasible=True,
            expected_monthly_savings_usd="100",
            monthly_savings_p05_usd="100",
            monthly_savings_p95_usd="100",
        )
        record = SimpleNamespace(
            report_json=dict(
                actions=[selected, other],
                recommended_candidate_share=1,
                recommended_action="ship_candidate",
                verdict="recommend",
            ),
            workload="K02",
            incumbent_model="incumbent",
            candidate_model="candidate",
            verdict="recommend",
            policy_json={},
            evidence_lineage_json={},
            decision_id="fixture-K02",
            evaluated_at=datetime(2026, 9, 2, tzinfo=UTC),
            evaluated_by="test",
            request_digest="a" * 64,
        )
        story = []

        def capture(document, elements, **kwargs):
            story.extend(elements)

        with patch.object(pdf.SimpleDocTemplate, "build", capture):
            pdf.render_decision_report_pdf(record)
        values = [
            row
            for element in story
            if isinstance(element, pdf.Table)
            for row in element._cellvalues
        ]
        self.assertIn(["Expected monthly savings", "$400.00"], values)
        self.assertNotIn(["Expected monthly savings", "$100.00"], values)

    def test_unscoped_report_access_cannot_bypass_tenant_ownership(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        from zeroth.econ.plane.reports.models import DecisionReportRecord
        from zeroth.econ.plane.reports.service import get_decision_report

        engine = create_engine("sqlite:///:memory:")
        DecisionReportRecord.__table__.create(engine)
        with Session(engine) as db:
            db.add(
                DecisionReportRecord(
                    report_id="tenant-b-report",
                    tenant_id="tenant-b",
                    decision_id="tenant-b-decision",
                    template_version="fixture",
                    media_type="application/pdf",
                    sha256="a" * 64,
                    pdf_bytes=b"tenant-b-private",
                    created_at=datetime.now(UTC),
                    created_by="test",
                )
            )
            db.commit()
            with self.assertRaises(TypeError):
                get_decision_report(db, "tenant-b-report")
        engine.dispose()
