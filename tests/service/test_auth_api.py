from __future__ import annotations

from fastapi.testclient import TestClient

from tests.service.helpers import (
    approval_resume_graph,
    deploy_service,
    operator_headers,
    reviewer_headers,
    wait_for,
)
from zeroth.service.bootstrap import bootstrap_app


async def test_service_health_bypasses_authentication(sqlite_db) -> None:
    """Health endpoints should be accessible without authentication."""
    service, _ = await deploy_service(
        sqlite_db, approval_resume_graph(graph_id="graph-auth-health")
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        auth_config=service.auth_config,
    )
    app.state.bootstrap = service

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_service_health_accepts_api_key_authentication(sqlite_db) -> None:
    service, _ = await deploy_service(
        sqlite_db, approval_resume_graph(graph_id="graph-auth-health-key")
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        auth_config=service.auth_config,
    )
    app.state.bootstrap = service

    with TestClient(app) as client:
        response = client.get("/health", headers=operator_headers())

    assert response.status_code == 200
    assert response.json()["deployment_ref"] == service.deployment.deployment_ref


async def test_browser_session_exchanges_api_key_for_secure_httponly_cookie(sqlite_db) -> None:
    service, _ = await deploy_service(
        sqlite_db, approval_resume_graph(graph_id="graph-browser-session")
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        auth_config=service.auth_config,
    )
    app.state.bootstrap = service

    with TestClient(app, base_url="https://testserver") as client:
        created = client.post("/v1/auth/session", headers=operator_headers())
        identity = client.get("/v1/identity")

    assert created.status_code == 204, created.text
    cookie = created.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=none" in cookie
    assert operator_headers()["X-API-Key"] not in cookie
    assert identity.status_code == 200, identity.text
    assert identity.json()["subject"] == "operator-1"


async def test_browser_session_cookie_cannot_authorize_cross_origin_mutation(sqlite_db) -> None:
    service, _ = await deploy_service(
        sqlite_db, approval_resume_graph(graph_id="graph-browser-session-origin")
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        auth_config=service.auth_config,
    )
    app.state.bootstrap = service

    with TestClient(app, base_url="https://testserver") as client:
        assert client.post("/v1/auth/session", headers=operator_headers()).status_code == 204
        response = client.post(
            "/runs",
            json={"input_payload": {"value": 3}},
            headers={"Origin": "https://attacker.invalid"},
        )

    assert response.status_code == 403


async def test_browser_session_exchange_rejects_unconfigured_origin(sqlite_db) -> None:
    service, _ = await deploy_service(
        sqlite_db, approval_resume_graph(graph_id="graph-browser-session-exchange-origin")
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        auth_config=service.auth_config,
    )
    app.state.bootstrap = service

    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(
            "/v1/auth/session",
            headers={**operator_headers(), "Origin": "https://attacker.invalid"},
        )

    assert response.status_code == 403
    assert "set-cookie" not in response.headers


async def test_csp_connect_src_does_not_allow_arbitrary_network_origins(sqlite_db) -> None:
    service, _ = await deploy_service(
        sqlite_db, approval_resume_graph(graph_id="graph-browser-session-csp")
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        auth_config=service.auth_config,
    )
    app.state.bootstrap = service

    with TestClient(app) as client:
        response = client.get("/health")

    csp = response.headers["content-security-policy"]
    connect_src = next(part for part in csp.split(";") if "connect-src" in part)
    assert connect_src.strip() == "connect-src 'self'"


async def test_service_denial_audit_run_identity_is_deployment_scoped(sqlite_db) -> None:
    service, deployment = await deploy_service(
        sqlite_db, approval_resume_graph(graph_id="graph-auth-denial-scope")
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=deployment.deployment_ref,
        auth_config=service.auth_config,
    )
    app.state.bootstrap = service

    with TestClient(app) as client:
        response = client.get("/v1/deployments")

    records = await service.audit_repository.list_by_node("service.auth")
    assert response.status_code == 401
    assert records[-1].run_id == (
        f"service:{deployment.deployment_ref}:GET:/v1/deployments"
    )


async def test_approval_resolution_uses_authenticated_principal(sqlite_db) -> None:
    service, _ = await deploy_service(
        sqlite_db,
        approval_resume_graph(graph_id="graph-auth-approval"),
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        auth_config=service.auth_config,
    )
    app.state.bootstrap = service

    with TestClient(app) as client:
        create_response = client.post(
            "/runs",
            json={"input_payload": {"value": 3}},
            headers=operator_headers(),
        )
        run_id = create_response.json()["run_id"]
        wait_for(
            lambda: (
                client.get(
                    f"/runs/{run_id}",
                    headers=operator_headers(),
                ).json()["status"]
                == "paused_for_approval"
            )
        )
        approval_id = client.get(
            f"/runs/{run_id}",
            headers=operator_headers(),
        ).json()["approval_paused_state"]["approval_id"]

        response = client.post(
            f"/deployments/{service.deployment.deployment_ref}/approvals/{approval_id}/resolve",
            json={"decision": "approve"},
            headers=reviewer_headers(),
        )

    assert response.status_code == 200
    assert response.json()["approval"]["resolution"]["actor"]["subject"] == "reviewer-1"
