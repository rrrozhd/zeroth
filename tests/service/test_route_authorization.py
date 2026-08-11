"""Router-level authorization declarations and fail-closed behavior."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from tests.service.helpers import default_service_auth_config, reviewer_headers
from zeroth.service.api.authentication import ServiceAuthenticator
from zeroth.service.app import create_app


def _bootstrap() -> SimpleNamespace:
    return SimpleNamespace(
        audit_repository=None,
        authenticator=ServiceAuthenticator(default_service_auth_config()),
        deployment=None,
        langgraph_gateway_proxy=None,
        langgraph_gateway_websocket_handler=None,
        regulus_client=None,
    )


def test_an_undeclared_route_fails_closed_before_its_handler_runs() -> None:
    """A developer cannot add an authenticated-but-unauthorized route by accident."""
    app = create_app(_bootstrap())
    handler_called = False

    @app.get("/__test_undeclared", name="test-undeclared")
    async def undeclared() -> dict[str, bool]:
        nonlocal handler_called
        handler_called = True
        return {"called": True}

    with TestClient(app) as client:
        response = client.get("/__test_undeclared", headers=reviewer_headers())

    assert response.status_code == 403
    assert response.json() == {"detail": "forbidden"}
    assert handler_called is False
