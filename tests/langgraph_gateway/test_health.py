from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from zeroth.core.langgraph_gateway.capabilities import CapabilityReporter
from zeroth.core.langgraph_gateway.models import (
    CompatibilityResult,
    CompatibilityStatus,
)
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
        "zeroth.core.service.health.check_database",
        AsyncMock(return_value=DependencyStatus(status="ok")),
    )
    monkeypatch.setattr(
        "zeroth.core.service.health.check_redis",
        AsyncMock(return_value=DependencyStatus(status="unavailable")),
    )
    monkeypatch.setattr(
        "zeroth.core.service.health.check_regulus",
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
