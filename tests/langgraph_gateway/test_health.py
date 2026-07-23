from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from zeroth.core.langgraph_gateway.capabilities import CapabilityReporter
from zeroth.core.langgraph_gateway.models import (
    CompatibilityResult,
    CompatibilityStatus,
)
from zeroth.core.langgraph_gateway.routes import GatewayWebSocketEndpoint
from zeroth.core.service.app import create_app
from zeroth.core.service.health import DependencyStatus, register_health_routes


def compatibility(status: CompatibilityStatus = CompatibilityStatus.SUPPORTED):
    return CompatibilityResult(
        tested_langgraph_versions=("1.2.9",),
        tested_agent_server_versions=("0.11.1",),
        detected_agent_server_version="0.11.1",
        openapi_fingerprint="sha256:fixture",
        status=status,
        reason=None if status is CompatibilityStatus.SUPPORTED else "safe probe status",
    )


class _WebSocket:
    def __init__(self) -> None:
        self.headers = Headers()
        self.state = SimpleNamespace()
        self.closed: tuple[int, str] | None = None

    async def close(self, code: int, reason: str) -> None:
        self.closed = (code, reason)


class _Authenticator:
    def authenticate_headers(self, _headers):
        return object()


class _RecordingWebSocketHandler:
    def __init__(self) -> None:
        self.calls = 0

    async def handle(self, _websocket) -> None:
        self.calls += 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected_close"),
    [
        (CompatibilityStatus.UNSUPPORTED, (4501, "zeroth.unsupported_upstream")),
        (CompatibilityStatus.UNAVAILABLE, (4502, "zeroth.upstream_unavailable")),
    ],
)
async def test_websocket_compatibility_rejects_before_handler_or_transport(
    status: CompatibilityStatus,
    expected_close: tuple[int, str],
) -> None:
    handler = _RecordingWebSocketHandler()
    endpoint = GatewayWebSocketEndpoint(
        authenticator=_Authenticator(),
        handler=handler,
        compatibility=compatibility(status),
    )
    websocket = _WebSocket()

    await endpoint(websocket)

    assert websocket.closed == expected_close
    assert handler.calls == 0


@pytest.mark.asyncio
async def test_supported_websocket_compatibility_preserves_handler_path() -> None:
    handler = _RecordingWebSocketHandler()
    endpoint = GatewayWebSocketEndpoint(
        authenticator=_Authenticator(),
        handler=handler,
        compatibility=compatibility(),
    )
    websocket = _WebSocket()

    await endpoint(websocket)

    assert websocket.closed is None
    assert handler.calls == 1


def test_deployment_health_reports_exact_gateway_capability_and_compatibility() -> None:
    deployment = SimpleNamespace(
        deployment_ref="external-agent",
        version=7,
        graph_version_ref="graph:external@7",
    )
    app = create_app(
        SimpleNamespace(
            deployment=deployment,
            regulus_client=None,
            langgraph_gateway_proxy=None,
            langgraph_gateway_websocket_handler=None,
            langgraph_gateway_transport=None,
            langgraph_gateway_compatibility=compatibility(),
            langgraph_gateway_capability_reporter=CapabilityReporter(),
        )
    )

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["langgraph_gateway"] == {
        "enabled": True,
        "governance_level": "admission",
        "limitation": "internal tool calls are not enforced in gateway-only mode",
        "compatibility": {
            "tested_langgraph": ["1.2.9"],
            "tested_agent_server": ["0.11.1"],
            "detected_agent_server": "0.11.1",
            "status": "supported",
            "openapi_fingerprint": "sha256:fixture",
        },
    }


def test_readiness_adds_agent_server_from_bounded_startup_detection(monkeypatch) -> None:
    app = FastAPI()
    app.state.bootstrap = SimpleNamespace(
        database=object(),
        regulus_client=None,
        langgraph_gateway_compatibility=compatibility(CompatibilityStatus.UNSUPPORTED),
    )
    monkeypatch.setattr(
        "zeroth.service.api.health.check_database",
        AsyncMock(return_value=DependencyStatus(status="ok")),
    )
    monkeypatch.setattr(
        "zeroth.service.api.health.check_redis",
        AsyncMock(return_value=DependencyStatus(status="unavailable")),
    )
    monkeypatch.setattr(
        "zeroth.service.api.health.check_regulus",
        AsyncMock(return_value=DependencyStatus(status="unavailable")),
    )
    register_health_routes(app)

    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["checks"]["agent_server"] == {
        "status": "unsupported",
        "latency_ms": None,
        "detail": "safe probe status",
    }
