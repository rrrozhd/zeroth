"""Tests for the GET /metrics endpoint."""
from __future__ import annotations

from fastapi.testclient import TestClient

from tests.service.helpers import (
    admin_headers,
    agent_graph,
    api_key_headers,
    deploy_service,
    operator_headers,
    scoped_auth_config,
)
from zeroth.identity import ServiceRole
from zeroth.service.bootstrap import bootstrap_app

DEPLOYMENT = "metrics-test"


def test_metrics_returns_200_with_prometheus_content_type(sqlite_db) -> None:
    service, _ = deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-metrics"),
        deployment_ref=DEPLOYMENT,
    )
    app = bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        r = client.get("/metrics", headers=admin_headers())

    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]


def test_metrics_contains_queue_depth_gauge(sqlite_db) -> None:
    service, _ = deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-metrics-names"),
        deployment_ref=DEPLOYMENT + "-names",
    )
    app = bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    # Manually set a gauge to verify it appears in output.
    service.metrics_collector.gauge_set("zeroth_queue_depth", 0)

    with TestClient(app) as client:
        r = client.get("/metrics", headers=admin_headers())

    text = r.text
    assert "zeroth_queue_depth" in text


def test_metrics_counter_increments_are_visible(sqlite_db) -> None:
    service, _ = deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-metrics-run"),
        deployment_ref=DEPLOYMENT + "-run",
    )
    app = bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    # Manually increment a counter to verify it appears in output.
    service.metrics_collector.increment("zeroth_runs_started_total")

    with TestClient(app) as client:
        r = client.get("/metrics", headers=admin_headers())

    assert r.status_code == 200
    assert "zeroth_runs_started_total 1" in r.text


def test_metrics_requires_metrics_permission(sqlite_db) -> None:
    service, _ = deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-metrics-operator"),
        deployment_ref=DEPLOYMENT + "-operator",
    )
    app = bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        r = client.get("/metrics", headers=operator_headers())

    assert r.status_code == 403


def test_metrics_hides_service_from_foreign_tenant_admin(sqlite_db) -> None:
    auth_config = scoped_auth_config(
        ("tenant-a-admin", "tenant-a-admin-key", ServiceRole.ADMIN, "tenant-a", None),
        ("tenant-b-admin", "tenant-b-admin-key", ServiceRole.ADMIN, "tenant-b", None),
    )
    service, _ = deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-metrics-tenant"),
        deployment_ref=DEPLOYMENT + "-tenant",
        auth_config=auth_config,
        tenant_id="tenant-a",
    )
    app = bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        auth_config=auth_config,
    )
    app.state.bootstrap = service

    with TestClient(app) as client:
        response = client.get("/metrics", headers=api_key_headers("tenant-b-admin-key"))

    assert response.status_code == 404
