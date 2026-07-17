"""Proactive approval notifications: factory, transports, and service wiring."""

from __future__ import annotations

import pytest

from zeroth.core.approvals import ApprovalRepository, ApprovalService
from zeroth.core.approvals.notifications import (
    ApprovalNotification,
    CompositeNotifier,
    EmailNotifier,
    SlackNotifier,
    build_approval_notifier,
)
from zeroth.core.config.settings import (
    ApprovalNotificationSettings,
    EmailNotificationSettings,
    SlackNotificationSettings,
)
from zeroth.core.graph import HumanApprovalNode, HumanApprovalNodeData
from zeroth.core.runs import Run, RunRepository

# --- Factory logic ---------------------------------------------------------------


def test_disabled_by_default_yields_no_notifier() -> None:
    assert build_approval_notifier(ApprovalNotificationSettings()) is None


def test_enabled_but_unconfigured_yields_no_notifier() -> None:
    # Enabled with no complete transport degrades to no-op rather than erroring.
    assert build_approval_notifier(ApprovalNotificationSettings(enabled=True)) is None


def test_slack_only_builds_single_transport() -> None:
    notifier = build_approval_notifier(
        ApprovalNotificationSettings(
            enabled=True,
            slack=SlackNotificationSettings(webhook_url="https://hooks.slack.com/x"),
        )
    )
    assert isinstance(notifier, CompositeNotifier)
    assert [type(t).__name__ for t in notifier.transports] == ["SlackNotifier"]


def test_email_and_slack_build_both_transports() -> None:
    notifier = build_approval_notifier(
        ApprovalNotificationSettings(
            enabled=True,
            slack=SlackNotificationSettings(webhook_url="https://hooks.slack.com/x"),
            email=EmailNotificationSettings(
                smtp_host="smtp.example.com",
                from_address="zeroth@example.com",
                to_addresses=["reviewer@example.com"],
            ),
        )
    )
    assert isinstance(notifier, CompositeNotifier)
    assert [type(t).__name__ for t in notifier.transports] == ["SlackNotifier", "EmailNotifier"]


def test_partial_email_config_is_skipped() -> None:
    # Missing to_addresses => the email transport is not built.
    notifier = build_approval_notifier(
        ApprovalNotificationSettings(
            enabled=True,
            email=EmailNotificationSettings(
                smtp_host="smtp.example.com", from_address="zeroth@example.com"
            ),
        )
    )
    assert notifier is None


# --- Event rendering -------------------------------------------------------------


def test_notification_text_includes_key_fields_and_sla() -> None:
    text = ApprovalNotification(
        approval_id="a1",
        run_id="r1",
        node_id="n1",
        deployment_ref="dep",
        tenant_id="acme",
        summary="Approval required for node n1",
        sla_deadline="2026-07-17T18:00:00+00:00",
    ).as_text()
    assert "a1" in text
    assert "acme" in text
    assert "SLA deadline: 2026-07-17T18:00:00+00:00" in text


# --- Composite fail-isolation ----------------------------------------------------


class _Boom:
    async def notify(self, event: ApprovalNotification) -> None:
        raise RuntimeError("transport down")


class _Recorder:
    def __init__(self) -> None:
        self.seen: list[str] = []

    async def notify(self, event: ApprovalNotification) -> None:
        self.seen.append(event.approval_id)


def _event() -> ApprovalNotification:
    return ApprovalNotification(
        approval_id="a1",
        run_id="r1",
        node_id="n1",
        deployment_ref="dep",
        tenant_id="acme",
        summary="Approval required",
    )


@pytest.mark.asyncio
async def test_composite_isolates_a_failing_transport() -> None:
    recorder = _Recorder()
    await CompositeNotifier([_Boom(), recorder]).notify(_event())
    # The healthy transport still fired despite the broken one raising.
    assert recorder.seen == ["a1"]


# --- Transport internals (I/O monkeypatched) -------------------------------------


@pytest.mark.asyncio
async def test_slack_notifier_posts_text_payload(monkeypatch) -> None:
    posted: dict[str, object] = {}

    class _FakeResponse:
        def raise_for_status(self) -> None:  # noqa: D401 - trivial
            return None

    class _FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> None:
            return None

        async def post(self, url: str, json: dict) -> _FakeResponse:
            posted["url"] = url
            posted["json"] = json
            return _FakeResponse()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    await SlackNotifier("https://hooks.slack.com/services/xxx").notify(_event())

    assert posted["url"] == "https://hooks.slack.com/services/xxx"
    assert "Approval required" in posted["json"]["text"]


@pytest.mark.asyncio
async def test_email_notifier_sends_message(monkeypatch) -> None:
    sent: dict[str, object] = {}

    class _FakeSMTP:
        def __init__(self, host, port, timeout=None) -> None:
            sent["host"] = host
            sent["port"] = port

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            return None

        def starttls(self) -> None:
            sent["tls"] = True

        def login(self, user, password) -> None:
            sent["login"] = (user, password)

        def send_message(self, message) -> None:
            sent["subject"] = message["Subject"]
            sent["to"] = message["To"]
            sent["body"] = message.get_content()

    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    await EmailNotifier(
        smtp_host="smtp.example.com",
        smtp_port=587,
        from_address="zeroth@example.com",
        to_addresses=["reviewer@example.com"],
        username="u",
        password="p",
    ).notify(_event())

    assert sent["host"] == "smtp.example.com"
    assert sent["tls"] is True
    assert sent["login"] == ("u", "p")
    assert sent["to"] == "reviewer@example.com"
    assert "Approval required" in str(sent["subject"])
    assert "Approval required" in str(sent["body"])


# --- Service integration ---------------------------------------------------------


def _node() -> HumanApprovalNode:
    return HumanApprovalNode(
        node_id="approval",
        graph_version_ref="graph-approval:v1",
        human_approval=HumanApprovalNodeData(
            resolution_schema_ref="schema://resolution",
            approval_policy_config={"allow_edits": True},
        ),
    )


def _run() -> Run:
    return Run(
        run_id="run-notify-1",
        thread_id="thread-1",
        graph_version_ref="graph-approval:v1",
        deployment_ref="graph-approval",
        pending_node_ids=["approval"],
    )


@pytest.mark.asyncio
async def test_create_pending_pings_attached_notifier(sqlite_db) -> None:
    recorder = _Recorder()
    service = ApprovalService(
        repository=ApprovalRepository(sqlite_db),
        run_repository=RunRepository(sqlite_db),
    )
    service.notifier = recorder
    run = await RunRepository(sqlite_db).create(_run())

    record = await service.create_pending(run=run, node=_node(), input_payload={"value": 1})

    assert recorder.seen == [record.approval_id]


@pytest.mark.asyncio
async def test_notifier_failure_does_not_break_create_pending(sqlite_db) -> None:
    service = ApprovalService(
        repository=ApprovalRepository(sqlite_db),
        run_repository=RunRepository(sqlite_db),
    )
    service.notifier = _Boom()
    run = await RunRepository(sqlite_db).create(_run())

    # Fail-open: the approval is still created even though the notifier raised.
    record = await service.create_pending(run=run, node=_node(), input_payload={"value": 1})
    assert await service.get(record.approval_id) is not None


def _sla_node(action: str) -> HumanApprovalNode:
    node = _node()
    node.human_approval.sla_timeout_seconds = 300
    node.human_approval.escalation_action = action
    return node


@pytest.mark.asyncio
async def test_escalation_pings_a_human(sqlite_db) -> None:
    # The SLA-breach ping is the one that matters most: escalate() must notify,
    # not just create_pending().
    recorder = _Recorder()
    service = ApprovalService(
        repository=ApprovalRepository(sqlite_db),
        run_repository=RunRepository(sqlite_db),
    )
    service.notifier = recorder
    run = await RunRepository(sqlite_db).create(_run())
    record = await service.create_pending(run=run, node=_sla_node("alert"), input_payload={})
    recorder.seen.clear()  # isolate the escalation ping from the request ping

    await service.escalate(record.approval_id)

    assert recorder.seen == [record.approval_id]


@pytest.mark.asyncio
async def test_escalation_ping_fires_at_most_once(sqlite_db) -> None:
    # escalate() is a no-op once ESCALATED, so a re-poll must not re-ping.
    recorder = _Recorder()
    service = ApprovalService(
        repository=ApprovalRepository(sqlite_db),
        run_repository=RunRepository(sqlite_db),
    )
    service.notifier = recorder
    run = await RunRepository(sqlite_db).create(_run())
    record = await service.create_pending(run=run, node=_sla_node("alert"), input_payload={})
    recorder.seen.clear()

    await service.escalate(record.approval_id)
    await service.escalate(record.approval_id)  # second poll — already ESCALATED

    assert recorder.seen == [record.approval_id]
