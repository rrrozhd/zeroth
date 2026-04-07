"""Tests for webhook models, signing utility, config settings, and SLA extensions."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError


class TestWebhookEventType:
    """WebhookEventType enum values."""

    def test_event_types(self):
        from zeroth.webhooks.models import WebhookEventType

        assert WebhookEventType.RUN_COMPLETED == "run.completed"
        assert WebhookEventType.RUN_FAILED == "run.failed"
        assert WebhookEventType.APPROVAL_REQUESTED == "approval.requested"
        assert WebhookEventType.APPROVAL_RESOLVED == "approval.resolved"
        assert WebhookEventType.APPROVAL_ESCALATED == "approval.escalated"


class TestDeliveryStatus:
    """DeliveryStatus enum values."""

    def test_delivery_status_values(self):
        from zeroth.webhooks.models import DeliveryStatus

        assert DeliveryStatus.PENDING == "pending"
        assert DeliveryStatus.DELIVERED == "delivered"
        assert DeliveryStatus.FAILED == "failed"
        assert DeliveryStatus.DEAD_LETTER == "dead_letter"


class TestEscalationAction:
    """EscalationAction enum values."""

    def test_escalation_action_values(self):
        from zeroth.webhooks.models import EscalationAction

        assert EscalationAction.DELEGATE == "delegate"
        assert EscalationAction.AUTO_REJECT == "auto_reject"
        assert EscalationAction.ALERT == "alert"


class TestWebhookSubscription:
    """WebhookSubscription model instantiation."""

    def test_instantiation_with_required_fields(self):
        from zeroth.webhooks.models import WebhookEventType, WebhookSubscription

        sub = WebhookSubscription(
            deployment_ref="deploy-1",
            target_url="https://example.com/hook",
            event_types=[WebhookEventType.RUN_COMPLETED],
        )
        assert sub.deployment_ref == "deploy-1"
        assert sub.target_url == "https://example.com/hook"
        assert sub.event_types == [WebhookEventType.RUN_COMPLETED]
        assert sub.subscription_id  # auto-generated
        assert sub.secret  # auto-generated
        assert sub.active is True
        assert sub.tenant_id == "default"

    def test_auto_generates_subscription_id_and_secret(self):
        from zeroth.webhooks.models import WebhookEventType, WebhookSubscription

        sub1 = WebhookSubscription(
            deployment_ref="d", target_url="https://x.com", event_types=[WebhookEventType.RUN_FAILED]
        )
        sub2 = WebhookSubscription(
            deployment_ref="d", target_url="https://x.com", event_types=[WebhookEventType.RUN_FAILED]
        )
        assert sub1.subscription_id != sub2.subscription_id
        assert sub1.secret != sub2.secret


class TestWebhookDelivery:
    """WebhookDelivery model instantiation."""

    def test_instantiation_defaults(self):
        from zeroth.webhooks.models import DeliveryStatus, WebhookDelivery, WebhookEventType

        delivery = WebhookDelivery(
            subscription_id="sub-1",
            event_type=WebhookEventType.RUN_COMPLETED,
            payload_json='{"key": "value"}',
        )
        assert delivery.status == DeliveryStatus.PENDING
        assert delivery.attempt_count == 0
        assert delivery.max_attempts == 5
        assert delivery.delivery_id  # auto-generated
        assert delivery.event_id  # auto-generated


class TestWebhookDeadLetter:
    """WebhookDeadLetter model instantiation."""

    def test_instantiation_from_delivery_fields(self):
        from zeroth.webhooks.models import WebhookDeadLetter, WebhookEventType

        dl = WebhookDeadLetter(
            delivery_id="del-1",
            subscription_id="sub-1",
            event_type=WebhookEventType.RUN_FAILED,
            event_id="evt-1",
            payload_json='{"err": true}',
            attempt_count=5,
            last_error="timeout",
            last_status_code=504,
        )
        assert dl.dead_letter_id  # auto-generated
        assert dl.delivery_id == "del-1"
        assert dl.attempt_count == 5


class TestSignPayload:
    """HMAC-SHA256 signing utility."""

    def test_returns_hex_string(self):
        from zeroth.webhooks.signing import sign_payload

        result = sign_payload(b"hello", "secret")
        assert isinstance(result, str)
        # hex string is 64 chars for sha256
        assert len(result) == 64

    def test_deterministic(self):
        from zeroth.webhooks.signing import sign_payload

        a = sign_payload(b"payload", "key")
        b = sign_payload(b"payload", "key")
        assert a == b

    def test_different_secret_different_output(self):
        from zeroth.webhooks.signing import sign_payload

        a = sign_payload(b"payload", "key1")
        b = sign_payload(b"payload", "key2")
        assert a != b

    def test_matches_manual_hmac(self):
        from zeroth.webhooks.signing import sign_payload

        payload = b"test-payload-data"
        secret = "my-secret-key"
        expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        assert sign_payload(payload, secret) == expected


class TestApprovalStatusEscalated:
    """ApprovalStatus ESCALATED value."""

    def test_escalated_value(self):
        from zeroth.approvals.models import ApprovalStatus

        assert ApprovalStatus.ESCALATED == "escalated"


class TestApprovalRecordSLAFields:
    """ApprovalRecord SLA fields backward compatibility."""

    def test_sla_fields_default_none(self):
        from zeroth.approvals.models import ApprovalRecord

        record = ApprovalRecord(
            run_id="run-1",
            node_id="node-1",
            graph_version_ref="gv-1",
            deployment_ref="dep-1",
            summary="test",
            rationale="test",
        )
        assert record.sla_deadline is None
        assert record.escalation_action is None
        assert record.escalated_from_id is None

    def test_sla_fields_with_values(self):
        from zeroth.approvals.models import ApprovalRecord

        deadline = datetime(2026, 1, 1, tzinfo=UTC)
        record = ApprovalRecord(
            run_id="run-1",
            node_id="node-1",
            graph_version_ref="gv-1",
            deployment_ref="dep-1",
            summary="test",
            rationale="test",
            sla_deadline=deadline,
            escalation_action="delegate",
            escalated_from_id="prev-approval-id",
        )
        assert record.sla_deadline == deadline
        assert record.escalation_action == "delegate"
        assert record.escalated_from_id == "prev-approval-id"


class TestHumanApprovalNodeDataSLA:
    """HumanApprovalNodeData SLA config fields."""

    def test_defaults_none(self):
        from zeroth.graph.models import HumanApprovalNodeData

        data = HumanApprovalNodeData()
        assert data.sla_timeout_seconds is None
        assert data.escalation_action is None
        assert data.delegate_identity is None

    def test_with_values(self):
        from zeroth.graph.models import HumanApprovalNodeData

        data = HumanApprovalNodeData(
            sla_timeout_seconds=300,
            escalation_action="delegate",
            delegate_identity={"actor_type": "user", "actor_id": "u-1"},
        )
        assert data.sla_timeout_seconds == 300
        assert data.escalation_action == "delegate"
        assert data.delegate_identity == {"actor_type": "user", "actor_id": "u-1"}


class TestWebhookSettings:
    """WebhookSettings defaults."""

    def test_defaults(self):
        from zeroth.config.settings import WebhookSettings

        ws = WebhookSettings()
        assert ws.enabled is True
        assert ws.delivery_poll_interval == 2.0
        assert ws.delivery_timeout == 10.0
        assert ws.max_delivery_concurrency == 16
        assert ws.default_max_retries == 5
        assert ws.retry_base_delay == 1.0
        assert ws.retry_max_delay == 300.0


class TestApprovalSLASettings:
    """ApprovalSLASettings defaults."""

    def test_defaults(self):
        from zeroth.config.settings import ApprovalSLASettings

        sla = ApprovalSLASettings()
        assert sla.enabled is True
        assert sla.checker_poll_interval == 10.0


class TestZerothSettingsSubModels:
    """ZerothSettings includes webhook and approval_sla."""

    def test_has_webhook_and_approval_sla(self):
        from zeroth.config.settings import ApprovalSLASettings, WebhookSettings, ZerothSettings

        settings = ZerothSettings()
        assert isinstance(settings.webhook, WebhookSettings)
        assert isinstance(settings.approval_sla, ApprovalSLASettings)
