"""Opt-in, fail-open Slack and email notifications for approvals."""

from __future__ import annotations

import asyncio
import logging
import smtplib
from collections.abc import Sequence
from email.message import EmailMessage
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from zeroth.platform.config.settings import ApprovalNotificationSettings

logger = logging.getLogger(__name__)


class ApprovalNotification(BaseModel):
    """Minimal notification metadata; approval payloads are intentionally excluded."""

    model_config = ConfigDict(extra="forbid")

    approval_id: str
    run_id: str
    node_id: str
    deployment_ref: str
    tenant_id: str
    summary: str
    sla_deadline: str | None = None

    def as_text(self) -> str:
        lines = [
            f"Approval required: {self.summary}",
            f"Approval ID: {self.approval_id}",
            f"Run: {self.run_id}  Node: {self.node_id}",
            f"Deployment: {self.deployment_ref}  Tenant: {self.tenant_id}",
        ]
        if self.sla_deadline:
            lines.append(f"SLA deadline: {self.sla_deadline}")
        return "\n".join(lines)


@runtime_checkable
class Notifier(Protocol):
    async def notify(self, event: ApprovalNotification) -> None: ...


class SlackNotifier:
    def __init__(self, webhook_url: str, *, timeout: float = 10.0) -> None:
        self._webhook_url = webhook_url
        self._timeout = timeout

    async def notify(self, event: ApprovalNotification) -> None:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(self._webhook_url, json={"text": event.as_text()})
            response.raise_for_status()


class EmailNotifier:
    def __init__(
        self,
        *,
        smtp_host: str,
        smtp_port: int,
        from_address: str,
        to_addresses: Sequence[str],
        username: str | None = None,
        password: str | None = None,
        use_tls: bool = True,
        timeout: float = 10.0,
    ) -> None:
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._from_address = from_address
        self._to_addresses = list(to_addresses)
        self._username = username
        self._password = password
        self._use_tls = use_tls
        self._timeout = timeout

    def _build_message(self, event: ApprovalNotification) -> EmailMessage:
        message = EmailMessage()
        message["Subject"] = f"[Zeroth] Approval required: {event.summary}"
        message["From"] = self._from_address
        message["To"] = ", ".join(self._to_addresses)
        message.set_content(event.as_text())
        return message

    def _send_sync(self, message: EmailMessage) -> None:
        with smtplib.SMTP(self._smtp_host, self._smtp_port, timeout=self._timeout) as server:
            if self._use_tls:
                server.starttls()
            if self._username and self._password:
                server.login(self._username, self._password)
            server.send_message(message)

    async def notify(self, event: ApprovalNotification) -> None:
        if self._to_addresses:
            await asyncio.to_thread(self._send_sync, self._build_message(event))


class CompositeNotifier:
    """Notify each transport independently so one outage cannot suppress another."""

    def __init__(self, notifiers: Sequence[Notifier]) -> None:
        self._notifiers = list(notifiers)

    @property
    def transports(self) -> list[Notifier]:
        return list(self._notifiers)

    async def notify(self, event: ApprovalNotification) -> None:
        for notifier in self._notifiers:
            try:
                await notifier.notify(event)
            except Exception:
                logger.exception(
                    "approval notification transport %s failed for approval %s",
                    type(notifier).__name__,
                    event.approval_id,
                )


def build_approval_notifier(
    settings: ApprovalNotificationSettings,
) -> CompositeNotifier | None:
    if not settings.enabled:
        return None
    notifiers: list[Notifier] = []
    if settings.slack.webhook_url:
        notifiers.append(SlackNotifier(settings.slack.webhook_url, timeout=settings.slack.timeout))
    email = settings.email
    if email.smtp_host and email.from_address and email.to_addresses:
        notifiers.append(
            EmailNotifier(
                smtp_host=email.smtp_host,
                smtp_port=email.smtp_port,
                from_address=email.from_address,
                to_addresses=email.to_addresses,
                username=email.username,
                password=(email.password.get_secret_value() if email.password else None),
                use_tls=email.use_tls,
                timeout=email.timeout,
            )
        )
    return CompositeNotifier(notifiers) if notifiers else None
