"""Tests for the console-facing econ dashboard proxy (F7).

The proxy exposes the bundled Regulus dashboard suite under /v1/econ/dashboard/*
so the console can reach it (the /regulus mount's JWT issuer is gated off). These
lock the wiring the audit was about: the routes are registered, sit behind
METRICS_READ, and — with no Regulus backend configured in the test fixture —
reach the handler's guard and return 503 rather than 404.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.service.helpers import admin_headers, agent_graph, deploy_service
from zeroth.service.bootstrap import bootstrap_app

DEPLOYMENT = "econ-dash-test"


async def _make_app(sqlite_db, graph_id: str, deployment_ref: str):
    service, _ = await deploy_service(
        sqlite_db,
        agent_graph(graph_id=graph_id),
        deployment_ref=deployment_ref,
    )
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service
    return app


async def test_econ_dashboard_requires_auth(sqlite_db) -> None:
    """The proxy is never anonymous — no API key yields 401 (not 404)."""
    app = await _make_app(sqlite_db, "graph-econ-dash-auth", DEPLOYMENT + "-auth")
    with TestClient(app) as client:
        r = client.get("/v1/econ/dashboard/kpis")
    assert r.status_code == 401


async def test_econ_dashboard_registered_503_without_regulus(sqlite_db) -> None:
    """Admin (has METRICS_READ) reaches the handler; without Regulus it 503s, not 404."""
    app = await _make_app(sqlite_db, "graph-econ-dash-503", DEPLOYMENT + "-503")
    with TestClient(app) as client:
        r = client.get("/v1/econ/dashboard/kpis", headers=admin_headers())
    assert r.status_code == 503
    assert "Regulus" in r.json()["detail"]


async def test_econ_dashboard_compat_alias_registered(sqlite_db) -> None:
    """The no-/v1 compat alias is registered too (parity with the other econ routes)."""
    app = await _make_app(sqlite_db, "graph-econ-dash-compat", DEPLOYMENT + "-compat")
    with TestClient(app) as client:
        r = client.get("/econ/dashboard/top-creators", headers=admin_headers())
    assert r.status_code == 503
