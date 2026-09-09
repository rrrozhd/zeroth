from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

from zeroth.econ.plane.config import settings


@dataclass(frozen=True)
class ReportEmail:
    recipients: tuple[str, ...]
    subject: str
    text_body: str
    attachment: bytes | None = None
    attachment_name: str | None = None


class ReportMailer(Protocol):
    def send(self, message: ReportEmail) -> None: ...


class ReportMailerUnavailable(RuntimeError):
    """The deployment has no usable report mail transport."""


class ConfiguredSmtpReportMailer:
    """SMTP transport whose credentials come only from deployment settings."""

    def send(self, message: ReportEmail) -> None:
        if not settings.report_email_enabled:
            raise ReportMailerUnavailable("decision report email delivery is disabled")
        if settings.report_smtp_username and not settings.report_smtp_starttls:
            raise ReportMailerUnavailable("authenticated report SMTP requires TLS")
        email = EmailMessage()
        email["From"] = settings.report_email_from
        email["To"] = ", ".join(message.recipients)
        email["Subject"] = message.subject
        email.set_content(message.text_body)
        if message.attachment is not None:
            email.add_attachment(
                message.attachment,
                maintype="application",
                subtype="pdf",
                filename=message.attachment_name or "zeroth-decision-report.pdf",
            )
        with smtplib.SMTP(
            settings.report_smtp_host,
            settings.report_smtp_port,
            timeout=settings.report_smtp_timeout_seconds,
        ) as smtp:
            if settings.report_smtp_starttls:
                smtp.starttls(context=ssl.create_default_context())
            if settings.report_smtp_username:
                smtp.login(
                    settings.report_smtp_username,
                    settings.report_smtp_password.get_secret_value(),
                )
            smtp.send_message(email)
