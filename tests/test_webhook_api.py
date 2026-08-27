"""Tests for webhook REST API endpoints."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from zeroth.service.api.webhook_api import register_webhook_routes
from zeroth.service.webhooks.models import (
    WebhookDeadLetter,
    WebhookDelivery,
    WebhookEventType,
    WebhookSubscription,
)
from zeroth.service.webhooks.service import WebhookService


def _make_app(webhook_service: WebhookService | None = None) -> FastAPI:
    """Build a minimal FastAPI app with webhook routes and a fake auth middleware."""
    app = FastAPI()

    # Fake authentication middleware that always sets an admin principal.
    from zeroth.governance.identity import AuthenticatedPrincipal, AuthMethod, ServiceRole

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

    audit_repository = AsyncMock()
    audit_repository._signer = object()
    audit_repository.write.side_effect = lambda record: record.model_copy(
        update={"record_signature": "signed"}
    )
    bootstrap = SimpleNamespace(
        webhook_service=webhook_service,
        audit_repository=audit_repository,
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
        assert mock_webhook_service.create_subscription.await_args.kwargs["actor"].subject == (
            "admin-1"
        )
        persisted = mock_webhook_service.create_subscription.await_args.args[0]
        assert persisted.target_url == "https://example.com/hook"


class TestCreateSubscriptionTargetBounds:
    """A02-6: target_url is an outbound destination and declares its bounds."""

    @pytest.mark.parametrize(
        "target_url",
        [
            "http://127.0.0.1:6379/hook",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.5/hook",
            "https://localhost/hook",
            "https://user:password@example.com/hook",
            "file:///etc/passwd",
            "not-a-url",
        ],
    )
    def test_internal_or_malformed_target_is_refused(
        self, client, mock_webhook_service, target_url
    ):
        resp = client.post(
            "/webhooks/subscriptions",
            json={
                "deployment_ref": "deploy-1",
                "target_url": target_url,
                "event_types": ["run.completed"],
            },
        )

        assert resp.status_code == 400
        # Refused BEFORE persistence: the AC is "before any socket is opened",
        # and a persisted row is what later opens one.
        mock_webhook_service.create_subscription.assert_not_called()

    def test_public_target_is_still_accepted(self, client, mock_webhook_service):
        sub = WebhookSubscription(
            subscription_id="sub-ok",
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

    @pytest.mark.parametrize(
        "event_types",
        [[], ["run.completed", "run.completed"]],
    )
    def test_event_selection_requires_a_nonempty_unique_set(
        self, client, mock_webhook_service, event_types
    ):
        resp = client.post(
            "/webhooks/subscriptions",
            json={
                "deployment_ref": "deploy-1",
                "target_url": "https://example.com/hook",
                "event_types": event_types,
            },
        )

        assert resp.status_code == 422
        mock_webhook_service.create_subscription.assert_not_called()

    def test_target_url_length_is_bounded(self, client, mock_webhook_service):
        resp = client.post(
            "/webhooks/subscriptions",
            json={
                "deployment_ref": "deploy-1",
                "target_url": "https://example.com/" + "x" * 2049,
                "event_types": ["run.completed"],
            },
        )

        assert resp.status_code == 422
        mock_webhook_service.create_subscription.assert_not_called()


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

    def test_list_is_scoped_to_served_deployment_tenant(self, client, mock_webhook_service):
        # F8: client-supplied filters are ignored; the list is forced to the
        # served deployment's ref + tenant so one tenant can't enumerate another's.
        mock_webhook_service.list_subscriptions.return_value = []
        resp = client.get("/webhooks/subscriptions?deployment_ref=other&tenant_id=other")
        assert resp.status_code == 200
        mock_webhook_service.list_subscriptions.assert_called_once_with(deployment_ref="deploy-1")


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
        # Deactivate first resolves the sub to enforce the tenant guard (F8).
        mock_webhook_service.get_subscription.return_value = WebhookSubscription(
            subscription_id="sub-1",
            deployment_ref="deploy-1",
            tenant_id="default",
            target_url="https://example.com/hook",
            event_types=[WebhookEventType.RUN_COMPLETED],
        )
        mock_webhook_service.deactivate_subscription.return_value = None
        resp = client.delete("/webhooks/subscriptions/sub-1")
        assert resp.status_code == 204
        call = mock_webhook_service.deactivate_subscription.await_args
        assert call.args == ("sub-1",)
        assert call.kwargs["actor"].subject == "admin-1"


def _served_sub(subscription_id: str = "sub-1") -> WebhookSubscription:
    """A subscription owned by the served deployment (deploy-1 / default)."""
    return WebhookSubscription(
        subscription_id=subscription_id,
        deployment_ref="deploy-1",
        tenant_id="default",
        target_url="https://example.com/hook",
        event_types=[WebhookEventType.RUN_COMPLETED],
    )


def _dead_letter(dead_letter_id: str, subscription_id: str) -> WebhookDeadLetter:
    return WebhookDeadLetter(
        dead_letter_id=dead_letter_id,
        delivery_id="del-1",
        subscription_id=subscription_id,
        event_type=WebhookEventType.RUN_COMPLETED,
        event_id="evt-1",
        payload_json="{}",
        attempt_count=5,
        last_error="HTTP 500",
        last_status_code=500,
    )


class TestListDeadLetters:
    """GET /webhooks/dead-letters."""

    def test_lists_dead_letters(self, client, mock_webhook_service):
        mock_webhook_service.list_subscriptions.return_value = [_served_sub("sub-1")]
        dead_letter = _dead_letter("dl-1", "sub-1")
        dead_letter.payload_json = (
            '{"data":{"run_id":"run-1","approval_id":"approval-1","private":"not returned"}}'
        )
        mock_webhook_service.list_dead_letters.return_value = [dead_letter]

        resp = client.get("/webhooks/dead-letters")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["dead_letters"][0]["dead_letter_id"] == "dl-1"
        assert data["dead_letters"][0]["run_id"] == "run-1"
        assert data["dead_letters"][0]["approval_id"] == "approval-1"
        assert "payload_json" not in data["dead_letters"][0]

    def test_list_delegates_scoping_to_query(self, client, mock_webhook_service):
        # F8 re-audit^2: scoping is delegated to the query via subscription_ids so
        # the LIMIT applies AFTER the tenant filter (a Python post-filter after a
        # global LIMIT would hide the deployment's own rows behind newer foreign ones).
        mock_webhook_service.list_subscriptions.return_value = [_served_sub("sub-1")]
        mock_webhook_service.list_dead_letters.return_value = [_dead_letter("dl-own", "sub-1")]
        resp = client.get("/webhooks/dead-letters")
        assert resp.status_code == 200
        mock_webhook_service.list_dead_letters.assert_called_once_with(
            subscription_ids=["sub-1"], limit=50
        )
        assert [d["dead_letter_id"] for d in resp.json()["dead_letters"]] == ["dl-own"]

    def test_foreign_subscription_id_filter_is_404(self, client, mock_webhook_service):
        mock_webhook_service.list_subscriptions.return_value = [_served_sub("sub-1")]
        resp = client.get("/webhooks/dead-letters?subscription_id=sub-globex")
        assert resp.status_code == 404

    def test_limit_above_bound_is_rejected(self, client, mock_webhook_service):
        # A02-12: every paginated route declares bounds instead of accepting an
        # unbounded caller-supplied limit.
        mock_webhook_service.list_subscriptions.return_value = [_served_sub("sub-1")]
        resp = client.get("/webhooks/dead-letters?limit=1000000")
        assert resp.status_code == 422

    def test_limit_zero_is_rejected(self, client, mock_webhook_service):
        mock_webhook_service.list_subscriptions.return_value = [_served_sub("sub-1")]
        resp = client.get("/webhooks/dead-letters?limit=0")
        assert resp.status_code == 422


class TestListDeliveries:
    """GET /webhooks/deliveries exposes safe, scoped delivery state."""

    def test_lists_delivery_metadata_without_payload_or_secret(self, client, mock_webhook_service):
        mock_webhook_service.list_subscriptions.return_value = [_served_sub("sub-1")]
        mock_webhook_service.list_deliveries.return_value = [
            WebhookDelivery(
                delivery_id="delivery-1",
                subscription_id="sub-1",
                event_type=WebhookEventType.RUN_COMPLETED,
                event_id="event-1",
                payload_json=(
                    '{"data":{"run_id":"run-1","approval_id":"approval-1",'
                    '"sensitive":"not returned"}}'
                ),
                status="delivered",
                attempt_count=1,
            )
        ]

        resp = client.get("/webhooks/deliveries")

        assert resp.status_code == 200
        assert resp.json()["total"] == 1
        item = resp.json()["deliveries"][0]
        assert item["delivery_id"] == "delivery-1"
        assert item["status"] == "delivered"
        assert item["run_id"] == "run-1"
        assert item["approval_id"] == "approval-1"
        assert "payload_json" not in item
        assert "secret" not in item
        mock_webhook_service.list_deliveries.assert_called_once_with(
            subscription_ids=["sub-1"], limit=50
        )

    def test_malformed_payload_never_leaks_and_has_no_correlation(
        self, client, mock_webhook_service
    ):
        mock_webhook_service.list_subscriptions.return_value = [_served_sub("sub-1")]
        mock_webhook_service.list_deliveries.return_value = [
            WebhookDelivery(
                delivery_id="delivery-bad",
                subscription_id="sub-1",
                event_type=WebhookEventType.RUN_FAILED,
                event_id="event-bad",
                payload_json="not-json",
            )
        ]

        item = client.get("/webhooks/deliveries").json()["deliveries"][0]

        assert item["run_id"] is None
        assert item["approval_id"] is None
        assert "payload_json" not in item

    def test_foreign_subscription_filter_is_not_found(self, client, mock_webhook_service):
        mock_webhook_service.list_subscriptions.return_value = [_served_sub("sub-1")]

        resp = client.get("/webhooks/deliveries?subscription_id=sub-foreign")

        assert resp.status_code == 404
        mock_webhook_service.list_deliveries.assert_not_called()


class TestReplayDeadLetter:
    """POST /webhooks/dead-letters/{dead_letter_id}/replay."""

    def test_replays_dead_letter(self, client, mock_webhook_service):
        mock_webhook_service.list_subscriptions.return_value = [_served_sub("sub-1")]
        mock_webhook_service.get_dead_letter.return_value = _dead_letter("dl-1", "sub-1")
        mock_webhook_service.replay_dead_letter.return_value = WebhookDelivery(
            delivery_id="del-new",
            subscription_id="sub-1",
            event_type=WebhookEventType.RUN_COMPLETED,
            event_id="evt-1",
            payload_json="{}",
        )

        resp = client.post("/webhooks/dead-letters/dl-1/replay")
        assert resp.status_code == 201
        data = resp.json()
        assert data["delivery_id"] == "del-new"
        assert data["status"] == "pending"
        audit = client.app.state.bootstrap.audit_repository.write.await_args.args[0]
        assert audit.node_id == "webhook.dead-letter.replay"
        assert audit.execution_metadata["webhook_dead_letter_id"] == "dl-1"

    def test_foreign_dead_letter_replay_is_404(self, client, mock_webhook_service):
        # F8 re-audit: replaying another tenant's dead-letter must 404, not force
        # a cross-tenant redelivery (and must not reach replay_dead_letter).
        mock_webhook_service.list_subscriptions.return_value = [_served_sub("sub-1")]
        mock_webhook_service.get_dead_letter.return_value = _dead_letter("dl-x", "sub-globex")
        resp = client.post("/webhooks/dead-letters/dl-x/replay")
        assert resp.status_code == 404
        mock_webhook_service.replay_dead_letter.assert_not_called()

    def test_404_when_not_found(self, client, mock_webhook_service):
        mock_webhook_service.list_subscriptions.return_value = [_served_sub("sub-1")]
        mock_webhook_service.get_dead_letter.return_value = None
        mock_webhook_service.replay_dead_letter.side_effect = KeyError("dl-x")
        resp = client.post("/webhooks/dead-letters/dl-x/replay")
        assert resp.status_code == 404


class TestPermissionEnforcement:
    """Webhook endpoints require WEBHOOK_ADMIN permission."""

    @staticmethod
    def _client_for_role(role):
        from zeroth.governance.identity import AuthenticatedPrincipal, AuthMethod, ServiceRole

        app = FastAPI()

        @app.middleware("http")
        async def fake_auth(request, call_next):
            request.state.principal = AuthenticatedPrincipal(
                subject=f"{role.value}-1",
                roles=[role],
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
        return TestClient(app)

    @pytest.mark.parametrize("role_name", ["operator", "reviewer"])
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", "/webhooks/subscriptions"),
            ("get", "/webhooks/subscriptions/sub-1"),
            ("delete", "/webhooks/subscriptions/sub-1"),
            ("get", "/webhooks/deliveries"),
            ("get", "/webhooks/dead-letters"),
            ("post", "/webhooks/dead-letters/dead-1/replay"),
        ],
    )
    def test_non_admin_roles_cannot_read_or_mutate_webhooks(self, role_name, method, path):
        """Operator and reviewer credentials fail before repository access."""
        from zeroth.governance.identity import ServiceRole

        client = self._client_for_role(ServiceRole(role_name))

        response = getattr(client, method)(path)

        assert response.status_code == 403
        service = client.app.state.bootstrap.webhook_service
        for operation in (
            "list_subscriptions",
            "get_subscription",
            "deactivate_subscription",
            "list_deliveries",
            "list_dead_letters",
            "get_dead_letter",
            "replay_dead_letter",
        ):
            getattr(service, operation).assert_not_called()

    @pytest.mark.parametrize("role_name", ["admin", "platform_admin"])
    def test_admin_roles_can_list_the_served_scope(self, role_name):
        from zeroth.governance.identity import ServiceRole

        client = self._client_for_role(ServiceRole(role_name))
        client.app.state.bootstrap.webhook_service.list_subscriptions.return_value = []

        response = client.get("/webhooks/subscriptions")

        assert response.status_code == 200
        client.app.state.bootstrap.webhook_service.list_subscriptions.assert_awaited_once_with(
            deployment_ref="deploy-1"
        )


class TestTenantIsolation:
    """F8 regression: webhook routes are scoped to the served deployment's tenant."""

    @staticmethod
    def _app_with_principal_tenant(tenant: str, service: WebhookService) -> FastAPI:
        from zeroth.governance.identity import AuthenticatedPrincipal, AuthMethod, ServiceRole

        app = FastAPI()

        @app.middleware("http")
        async def fake_auth(request, call_next):
            request.state.principal = AuthenticatedPrincipal(
                subject="admin-x",
                roles=[ServiceRole.ADMIN],
                tenant_id=tenant,
                workspace_id=None,
                auth_method=AuthMethod.API_KEY,
            )
            return await call_next(request)

        # The service serves a deployment owned by tenant "default".
        app.state.bootstrap = SimpleNamespace(
            webhook_service=service,
            audit_repository=None,
            deployment=SimpleNamespace(
                deployment_ref="deploy-1", tenant_id="default", workspace_id=None
            ),
        )
        register_webhook_routes(app)
        return app

    def test_foreign_tenant_admin_cannot_create(self):
        svc = AsyncMock(spec=WebhookService)
        app = self._app_with_principal_tenant("acme", svc)
        client = TestClient(app)
        resp = client.post(
            "/webhooks/subscriptions",
            json={
                "deployment_ref": "deploy-1",
                "target_url": "https://evil.example/hook",
                "event_types": ["run.completed"],
            },
        )
        # Caller's tenant != served deployment's tenant -> 404 (scope mismatch),
        # and the subscription is never created.
        assert resp.status_code == 404
        svc.create_subscription.assert_not_called()

    @pytest.mark.parametrize("role_name", ["admin", "platform_admin"])
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", "/webhooks/subscriptions"),
            ("get", "/webhooks/subscriptions/sub-1"),
            ("delete", "/webhooks/subscriptions/sub-1"),
            ("get", "/webhooks/deliveries"),
            ("get", "/webhooks/dead-letters"),
            ("post", "/webhooks/dead-letters/dead-1/replay"),
        ],
    )
    def test_foreign_admin_roles_cannot_read_or_mutate_any_webhook_route(
        self, role_name, method, path
    ):
        from zeroth.governance.identity import ServiceRole

        svc = AsyncMock(spec=WebhookService)
        app = self._app_with_principal_tenant("acme", svc)
        # Exercise both admin tiers without changing the scope mismatch.
        principal_role = ServiceRole(role_name)

        @app.middleware("http")
        async def selected_role(request, call_next):
            from zeroth.governance.identity import AuthenticatedPrincipal, AuthMethod

            request.state.principal = AuthenticatedPrincipal(
                subject=f"{role_name}-foreign",
                roles=[principal_role],
                tenant_id="acme",
                workspace_id=None,
                auth_method=AuthMethod.API_KEY,
            )
            return await call_next(request)

        response = getattr(TestClient(app), method)(path)

        assert response.status_code == 404
        for operation in (
            "list_subscriptions",
            "get_subscription",
            "deactivate_subscription",
            "list_deliveries",
            "list_dead_letters",
            "get_dead_letter",
            "replay_dead_letter",
        ):
            getattr(svc, operation).assert_not_called()

    def test_foreign_tenant_subscription_reads_as_404(self):
        svc = AsyncMock(spec=WebhookService)
        # Same-tenant caller, but the fetched subscription belongs to another tenant.
        svc.get_subscription.return_value = WebhookSubscription(
            subscription_id="sub-x",
            deployment_ref="deploy-1",
            tenant_id="globex",
            target_url="https://example.com/hook",
            event_types=[WebhookEventType.RUN_COMPLETED],
        )
        app = self._app_with_principal_tenant("default", svc)
        client = TestClient(app)
        resp = client.get("/webhooks/subscriptions/sub-x")
        assert resp.status_code == 404

    def test_foreign_deployment_subscription_reads_as_404(self):
        # F8 re-audit: same tenant, but a subscription bound to a DIFFERENT
        # deployment_ref than the served one must read as absent (the list route
        # already scopes by deployment_ref; the by-id routes must match).
        svc = AsyncMock(spec=WebhookService)
        svc.get_subscription.return_value = WebhookSubscription(
            subscription_id="sub-y",
            deployment_ref="deploy-2",
            tenant_id="default",
            target_url="https://example.com/hook",
            event_types=[WebhookEventType.RUN_COMPLETED],
        )
        app = self._app_with_principal_tenant("default", svc)  # serves deploy-1
        client = TestClient(app)
        assert client.get("/webhooks/subscriptions/sub-y").status_code == 404
        assert client.delete("/webhooks/subscriptions/sub-y").status_code == 404


@pytest.mark.asyncio
async def test_repo_list_dead_letters_scoped_by_subscription_ids(sqlite_db) -> None:
    """F8 re-audit^2 (real DB): the subscription_ids filter is applied in the
    query, so only the given subscriptions' dead-letters are returned and the
    LIMIT is applied after the filter."""
    from zeroth.service.webhooks.models import WebhookDelivery
    from zeroth.service.webhooks.repository import WebhookRepository

    repo = WebhookRepository.for_default_compatibility(sqlite_db)
    for sub_id, dep in [("own", "d1"), ("other", "d2")]:
        await repo.create_subscription(
            WebhookSubscription(
                subscription_id=sub_id,
                deployment_ref=dep,
                tenant_id="default",
                target_url="https://example.com/hook",
                event_types=[WebhookEventType.RUN_COMPLETED],
            )
        )
        delivery = await repo.enqueue_delivery(
            WebhookDelivery(
                subscription_id=sub_id,
                event_type=WebhookEventType.RUN_COMPLETED,
                event_id="evt",
                payload_json="{}",
            )
        )
        claim = await repo.claim_pending_delivery()
        assert claim is not None
        await repo.dead_letter(delivery.delivery_id, claim.generation)

    scoped = await repo.list_dead_letters(subscription_ids=["own"], limit=50)
    assert [dl.subscription_id for dl in scoped] == ["own"]
    # Empty set returns nothing (not "all").
    assert await repo.list_dead_letters(subscription_ids=[], limit=50) == []
