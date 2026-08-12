"""Proactive approval notification behavior on the governance surface."""

from __future__ import annotations

import pytest

from zeroth.contracts.graph import HumanApprovalNode, HumanApprovalNodeData
from zeroth.governance.approvals import ApprovalRepository, ApprovalService
from zeroth.governance.approvals.notifications import (
    ApprovalNotification,
    CompositeNotifier,
    EmailNotifier,
    SlackNotifier,
    build_approval_notifier,
)
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.platform.config.settings import (
    ApprovalNotificationSettings,
    EmailNotificationSettings,
    SlackNotificationSettings,
)
from zeroth.runtime.runs import Run


class _Recorder:
    def __init__(self) -> None:
        self.events: list[ApprovalNotification] = []

    async def notify(self, event: ApprovalNotification) -> None:
        self.events.append(event)


class _Boom:
    async def notify(self, event: ApprovalNotification) -> None:
        raise RuntimeError("transport unavailable")


def _node(*, action: str = "alert") -> HumanApprovalNode:
    return HumanApprovalNode(
        node_id="approval",
        graph_version_ref="approval:v1",
        human_approval=HumanApprovalNodeData(
            resolution_schema_ref="schema://resolution",
            approval_policy_config={"allow_edits": True},
            sla_timeout_seconds=300,
            escalation_action=action,
        ),
    )


def _run() -> Run:
    return Run(
        run_id="run-notify",
        thread_id="thread-notify",
        graph_version_ref="approval:v1",
        deployment_ref="approval",
        pending_node_ids=["approval"],
    )


def test_notification_factory_is_opt_in_and_builds_complete_transports() -> None:
    assert build_approval_notifier(ApprovalNotificationSettings()) is None

    notifier = build_approval_notifier(
        ApprovalNotificationSettings(
            enabled=True,
            slack=SlackNotificationSettings(webhook_url="https://hooks.slack.test/x"),
            email=EmailNotificationSettings(
                smtp_host="smtp.test",
                from_address="zeroth@test",
                to_addresses=["reviewer@test"],
            ),
        )
    )

    assert isinstance(notifier, CompositeNotifier)
    assert [type(item).__name__ for item in notifier.transports] == [
        "SlackNotifier",
        "EmailNotifier",
    ]


@pytest.mark.asyncio
async def test_composite_isolates_transport_failure() -> None:
    recorder = _Recorder()
    event = ApprovalNotification(
        approval_id="a1",
        run_id="r1",
        node_id="n1",
        deployment_ref="d1",
        tenant_id="t1",
        summary="Review required",
    )

    await CompositeNotifier([_Boom(), recorder]).notify(event)

    assert recorder.events == [event]


@pytest.mark.asyncio
async def test_slack_posts_rendered_notification(monkeypatch) -> None:
    posted: dict[str, object] = {}

    class _Response:
        def raise_for_status(self) -> None:
            return None

    class _Client:
        def __init__(self, **kwargs) -> None:
            posted["timeout"] = kwargs["timeout"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, url: str, json: dict[str, str]) -> _Response:
            posted.update(url=url, json=json)
            return _Response()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await SlackNotifier("https://hooks.slack.test/x", timeout=3).notify(
        ApprovalNotification(
            approval_id="a1",
            run_id="r1",
            node_id="n1",
            deployment_ref="d1",
            tenant_id="t1",
            summary="Review required",
        )
    )

    assert posted["url"] == "https://hooks.slack.test/x"
    assert "Approval ID: a1" in posted["json"]["text"]


@pytest.mark.asyncio
async def test_email_sends_rendered_notification_off_loop(monkeypatch) -> None:
    sent: dict[str, object] = {}

    class _SMTP:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            sent.update(host=host, port=port, timeout=timeout)

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def starttls(self) -> None:
            sent["tls"] = True

        def login(self, username: str, password: str) -> None:
            sent["login"] = (username, password)

        def send_message(self, message) -> None:
            sent["message"] = message

    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", _SMTP)
    await EmailNotifier(
        smtp_host="smtp.test",
        smtp_port=587,
        from_address="zeroth@test",
        to_addresses=["reviewer@test"],
        username="user",
        password="password",
    ).notify(
        ApprovalNotification(
            approval_id="a1",
            run_id="r1",
            node_id="n1",
            deployment_ref="d1",
            tenant_id="t1",
            summary="Review required",
        )
    )

    assert sent["tls"] is True
    assert sent["login"] == ("user", "password")
    assert sent["message"]["To"] == "reviewer@test"


@pytest.mark.asyncio
async def test_request_and_escalation_notify_once_and_fail_open(sqlite_db) -> None:
    recorder = _Recorder()
    service = ApprovalService(
        repository=ApprovalRepository(sqlite_db),
        run_repository=RunRepository.for_default_compatibility(sqlite_db),
    )
    service.notifier = CompositeNotifier([_Boom(), recorder])
    run = await RunRepository.for_default_compatibility(sqlite_db).create(_run())

    record = await service.create_pending(run=run, node=_node(), input_payload={"secret": "x"})
    await service.escalate(record.approval_id)
    await service.escalate(record.approval_id)

    assert [event.approval_id for event in recorder.events] == [
        record.approval_id,
        record.approval_id,
    ]
    assert all("secret" not in event.model_dump_json() for event in recorder.events)
