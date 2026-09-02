"""Required red gates: local relay failures and durable ambiguous-attempt audit."""

import hashlib
import smtplib
import tempfile
import unittest
from contextlib import ExitStack
from datetime import UTC, datetime
from email import policy as email_policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch

from release.economic_evaluation.local_smtp import receiver


class LocalSmtpTests(unittest.TestCase):
    def configured(self, stack, server):
        from zeroth.econ.plane.config import settings
        from zeroth.econ.plane.reports.mailer import ConfiguredSmtpReportMailer

        values = dict(
            report_email_enabled=True,
            report_email_from="sender@local.test",
            report_smtp_host="127.0.0.1",
            report_smtp_port=server.server_address[1],
            report_smtp_starttls=False,
            report_smtp_username=None,
            report_smtp_timeout_seconds=0.2,
        )
        for key, value in values.items():
            stack.enter_context(patch.object(settings, key, value))
        return ConfiguredSmtpReportMailer()

    def message(self):
        from zeroth.econ.plane.reports.mailer import ReportEmail

        return ReportEmail(
            recipients=("accepted@local.test", "rejected@local.test"),
            subject="Frozen local fixture",
            text_body="No external delivery.",
            attachment=b"frozen-artifact-bytes",
            attachment_name="fixture.pdf",
        )

    def test_local_mime_preserves_bytes(self):
        with receiver() as server, ExitStack() as stack:
            self.configured(stack, server).send(self.message())
            mime = BytesParser(policy=email_policy.default).parsebytes(server.messages[0])
            self.assertEqual(mime["Subject"], "Frozen local fixture")
            self.assertEqual(
                next(mime.iter_attachments()).get_payload(decode=True), b"frozen-artifact-bytes"
            )

    def test_total_rejection_raises(self):
        with receiver("reject") as server, ExitStack() as stack:
            with self.assertRaises(smtplib.SMTPRecipientsRefused):
                self.configured(stack, server).send(self.message())
            self.assertEqual(server.messages, [])

    def test_timeout_is_not_success(self):
        with receiver("timeout") as server, ExitStack() as stack:
            with self.assertRaises((TimeoutError, smtplib.SMTPServerDisconnected)):
                self.configured(stack, server).send(self.message())
            self.assertEqual(len(server.messages), 1)

    def test_partial_recipient_rejection_is_not_silent_success(self):
        with receiver("partial") as server, ExitStack() as stack:
            with self.assertRaises(smtplib.SMTPRecipientsRefused):
                self.configured(stack, server).send(self.message())
            self.assertEqual(len(server.messages), 1)

    def test_crash_after_acceptance_retains_attempt_for_investigation(self):
        from sqlalchemy import create_engine, select
        from sqlalchemy.orm import Session

        from zeroth.econ.plane.decisioning.models import ProbabilisticMigrationDecisionRecord
        from zeroth.econ.plane.reports.models import (
            DecisionReportDeliveryRecord,
            DecisionReportRecord,
        )
        from zeroth.econ.plane.reports.schemas import DecisionReportDeliveryCreate
        from zeroth.econ.plane.reports.service import deliver_decision_report
        from zeroth.econ.plane.scoped_session import ScopedSession
        from zeroth.platform.storage.scoping import TenantWideScopeContext

        with tempfile.TemporaryDirectory(prefix="zeroth-local-delivery-") as directory:
            engine = create_engine(f"sqlite:///{Path(directory) / 'audit.db'}")
            for model in (
                ProbabilisticMigrationDecisionRecord,
                DecisionReportRecord,
                DecisionReportDeliveryRecord,
            ):
                model.__table__.create(engine)
            now = datetime(2026, 9, 2, tzinfo=UTC)
            with Session(engine) as db:
                db.add(
                    ProbabilisticMigrationDecisionRecord(
                        decision_id="decision-local",
                        tenant_id="local-tenant",
                        request_digest="a" * 64,
                        workload="local",
                        incumbent_model="i",
                        candidate_model="c",
                        verdict="hold",
                        recommended_action="keep_incumbent",
                        evidence_json={},
                        evidence_lineage_json={},
                        policy_json={},
                        report_json={"recommended_candidate_share": 0},
                        evaluated_at=now,
                        evaluated_by="test",
                    )
                )
                db.add(
                    DecisionReportRecord(
                        report_id="report-local",
                        tenant_id="local-tenant",
                        decision_id="decision-local",
                        template_version="fixture",
                        media_type="application/pdf",
                        sha256=hashlib.sha256(b"fixture").hexdigest(),
                        pdf_bytes=b"fixture",
                        created_at=now,
                        created_by="test",
                    )
                )
                db.commit()
            with receiver() as server, ExitStack() as stack:
                real_mailer = self.configured(stack, server)

                class CrashAfterAcceptance:
                    def send(self, message):
                        real_mailer.send(message)
                        raise SystemExit("fault: process dies after relay acceptance")

                with Session(engine) as db:
                    scoped = ScopedSession(db, TenantWideScopeContext(tenant_id="local-tenant"))
                    with self.assertRaises(SystemExit):
                        deliver_decision_report(
                            scoped,
                            "report-local",
                            DecisionReportDeliveryCreate(
                                recipients=["accepted@example.com"], delivery_mode="attachment"
                            ),
                            mailer=CrashAfterAcceptance(),
                            created_by="test",
                            public_base_url="https://local.test",
                        )
                self.assertEqual(len(server.messages), 1)
            with Session(engine) as db:
                attempts = list(db.scalars(select(DecisionReportDeliveryRecord)))
                self.assertEqual(
                    len(attempts), 1, "relay accepted bytes but durable audit attempt vanished"
                )
                self.assertNotEqual(attempts[0].status, "sent")
            engine.dispose()
