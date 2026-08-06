"""Tests for the GET /v1/connectors listing endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.service.helpers import agent_graph, deploy_service, operator_headers
from zeroth.service.bootstrap import bootstrap_app

DEPLOYMENT = "connectors-test"


async def test_list_connectors_returns_builtin_refs(sqlite_db) -> None:
    service, _ = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-connectors"),
        deployment_ref=DEPLOYMENT,
    )
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        r = client.get("/v1/connectors", headers=operator_headers())

    assert r.status_code == 200
    body = r.json()
    refs = {c["ref"] for c in body}
    # The three in-memory connectors are always registered at bootstrap.
    assert {"ephemeral", "key_value", "thread"} <= refs
    by_ref = {c["ref"]: c for c in body}
    assert by_ref["key_value"]["scope"] == "shared"
    assert by_ref["thread"]["scope"] == "thread"
    assert by_ref["ephemeral"]["scope"] == "run"
    assert by_ref["key_value"]["backend"] == "KeyValueMemoryConnector"


async def test_list_connectors_requires_auth(sqlite_db) -> None:
    service, _ = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-connectors-auth"),
        deployment_ref=DEPLOYMENT + "-auth",
    )
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        r = client.get("/v1/connectors")

    assert r.status_code == 401
