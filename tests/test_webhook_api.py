"""Tests for webhook REST API endpoints."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from zeroth.service.webhook_api import register_webhook_routes
from zeroth.webhooks.models import (
    DeliveryStatus,
    WebhookDeadLetter,
    WebhookDelivery,
    WebhookEventType,
    WebhookSubscription,
)
from zeroth.webhooks.service import WebhookService


def _make_app(webhook_service: WebhookService | None = None) -> FastAPI:
    """Build a minimal FastAPI app with webhook routes and a fake auth middleware."""
    app = FastAPI()

    # Fake authentication middleware that always sets an admin principal.
    from zeroth.identity import AuthMethod, AuthenticatedPrincipal, ServiceRole

    @app.middleware("http")
    async def fake_auth(request, call_next):
        request.state.principal = AuthenticatedPrincipal(
            subject="admin-1",
            roles=[ServiceRole.ADMIN],
            tenant_id="default",
            workspace_id=None,
            auth_method=AuthMethod.API_KEY,
        )
        return await call_next(request)

    bootstrap = SimpleNamespace(
        webhook_service=webhook_service,
        audit_repository=None,
        deployment=SimpleNamespace(
            deployment_ref="deploy-1",
            tenant_id="default",
            workspace_id=None,
        ),
    )
    app.state.bootstrap = bootstrap
    register_webhook_routes(app)
    return app


@pytest.fixture
def mock_webhook_service():
    svc = AsyncMock(spec=WebhookService)
    return svc


@pytest.fixture
def client(mock_webhook_service):
    app = _make_app(mock_webhook_service)
    return TestClient(app)


class TestCreateSubscription:
    """POST /webhooks/subscriptions."""

    def test_creates_subscription(self, client, mock_webhook_service):
        sub = WebhookSubscription(
            subscription_id="sub-1",
            deployment_ref="deploy-1",
            tenant_id="default",
            target_url="https://example.com/hook",
            secret="secret-123",
            event_types=[WebhookEventType.RUN_COMPLETED],
        )
        mock_webhook_service.create_subscription.return_value = sub

        resp = client.post(
            "/webhooks/subscriptions",
            json={
                "deployment_ref": "deploy-1",
                "target_url": "https://example.com/hook",
                "event_types": ["run.completed"],
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["subscription_id"] == "sub-1"
        assert data["secret"] == "secret-123"
        assert data["event_types"] == ["run.completed"]
        assert data["active"] is True


class TestListSubscriptions:
    """GET /webhooks/subscriptions."""

    def test_lists_subscriptions(self, client, mock_webhook_service):
        sub = WebhookSubscription(
            deployment_ref="deploy-1",
            target_url="https://example.com/hook",
            event_types=[WebhookEventType.RUN_COMPLETED],
        )
        mock_webhook_service.list_subscriptions.return_value = [sub]

        resp = client.get("/webhooks/subscriptions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert len(data["subscriptions"]) == 1

    def test_filters_by_deployment_ref(self, client, mock_webhook_service):
        mock_webhook_service.list_subscriptions.return_value = []
        resp = client.get("/webhooks/subscriptions?deployment_ref=deploy-1")
        assert resp.status_code == 200
        mock_webhook_service.list_subscriptions.assert_called_once_with(
            deployment_ref="deploy-1", tenant_id=None
        )


class TestGetSubscription:
    """GET /webhooks/subscriptions/{subscription_id}."""

    def test_returns_subscription(self, client, mock_webhook_service):
        sub = WebhookSubscription(
            subscription_id="sub-1",
            deployment_ref="deploy-1",
            target_url="https://example.com/hook",
            event_types=[WebhookEventType.RUN_COMPLETED],
        )
        mock_webhook_service.get_subscription.return_value = sub

        resp = client.get("/webhooks/subscriptions/sub-1")
        assert resp.status_code == 200
        assert resp.json()["subscription_id"] == "sub-1"

    def test_404_when_not_found(self, client, mock_webhook_service):
        mock_webhook_service.get_subscription.return_value = None
        resp = client.get("/webhooks/subscriptions/nonexistent")
        assert resp.status_code == 404


class TestDeactivateSubscription:
    """DELETE /webhooks/subscriptions/{subscription_id}."""

    def test_deactivates_subscription(self, client, mock_webhook_service):
        mock_webhook_service.deactivate_subscription.return_value = None
        resp = client.delete("/webhooks/subscriptions/sub-1")
        assert resp.status_code == 204
        mock_webhook_service.deactivate_subscription.assert_called_once_with("sub-1")


class TestListDeadLetters:
    """GET /webhooks/dead-letters."""

    def test_lists_dead_letters(self, client, mock_webhook_service):
        dl = WebhookDeadLetter(
            dead_letter_id="dl-1",
            delivery_id="del-1",
            subscription_id="sub-1",
            event_type=WebhookEventType.RUN_COMPLETED,
            event_id="evt-1",
            payload_json="{}",
            attempt_count=5,
            last_error="HTTP 500",
            last_status_code=500,
        )
        mock_webhook_service.list_dead_letters.return_value = [dl]

        resp = client.get("/webhooks/dead-letters")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["dead_letters"][0]["dead_letter_id"] == "dl-1"


class TestReplayDeadLetter:
    """POST /webhooks/dead-letters/{dead_letter_id}/replay."""

    def test_replays_dead_letter(self, client, mock_webhook_service):
        delivery = WebhookDelivery(
            delivery_id="del-new",
            subscription_id="sub-1",
            event_type=WebhookEventType.RUN_COMPLETED,
            event_id="evt-1",
            payload_json="{}",
        )
        mock_webhook_service.replay_dead_letter.return_value = delivery

        resp = client.post("/webhooks/dead-letters/dl-1/replay")
        assert resp.status_code == 201
        data = resp.json()
        assert data["delivery_id"] == "del-new"
        assert data["status"] == "pending"

    def test_404_when_not_found(self, client, mock_webhook_service):
        mock_webhook_service.replay_dead_letter.side_effect = KeyError("dl-x")
        resp = client.post("/webhooks/dead-letters/dl-x/replay")
        assert resp.status_code == 404


class TestPermissionEnforcement:
    """Webhook endpoints require WEBHOOK_ADMIN permission."""

    def test_operator_cannot_access_webhooks(self):
        """Non-admin roles should get 403."""
        from zeroth.identity import AuthMethod, AuthenticatedPrincipal, ServiceRole

        app = FastAPI()

        @app.middleware("http")
        async def fake_auth(request, call_next):
            request.state.principal = AuthenticatedPrincipal(
                subject="operator-1",
                roles=[ServiceRole.OPERATOR],
                tenant_id="default",
                workspace_id=None,
                auth_method=AuthMethod.API_KEY,
            )
            return await call_next(request)

        bootstrap = SimpleNamespace(
            webhook_service=AsyncMock(spec=WebhookService),
            audit_repository=None,
            deployment=SimpleNamespace(
                deployment_ref="deploy-1",
                tenant_id="default",
                workspace_id=None,
            ),
        )
        app.state.bootstrap = bootstrap
        register_webhook_routes(app)

        client = TestClient(app)
        resp = client.get("/webhooks/subscriptions")
        assert resp.status_code == 403
