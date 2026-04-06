from __future__ import annotations

from fastapi.testclient import TestClient

from tests.service.helpers import approval_resume_graph, deploy_service, wait_for
from zeroth.identity import ServiceRole
from zeroth.service.auth import ServiceAuthConfig, StaticApiKeyCredential
from zeroth.service.bootstrap import bootstrap_app


def _scoped_auth_config() -> ServiceAuthConfig:
    return ServiceAuthConfig(
        api_keys=[
            StaticApiKeyCredential(
                credential_id="tenant-a-operator",
                secret="tenant-a-operator-key",
                subject="tenant-a-operator",
                roles=[ServiceRole.OPERATOR],
                tenant_id="tenant-a",
            ),
            StaticApiKeyCredential(
                credential_id="tenant-b-operator",
                secret="tenant-b-operator-key",
                subject="tenant-b-operator",
                roles=[ServiceRole.OPERATOR],
                tenant_id="tenant-b",
            ),
            StaticApiKeyCredential(
                credential_id="tenant-a-reviewer",
                secret="tenant-a-reviewer-key",
                subject="tenant-a-reviewer",
                roles=[ServiceRole.REVIEWER],
                tenant_id="tenant-a",
            ),
            StaticApiKeyCredential(
                credential_id="tenant-b-reviewer",
                secret="tenant-b-reviewer-key",
                subject="tenant-b-reviewer",
                roles=[ServiceRole.REVIEWER],
                tenant_id="tenant-b",
            ),
        ]
    )


def _headers(secret: str) -> dict[str, str]:
    return {"X-API-Key": secret}


async def test_cross_tenant_run_read_returns_not_found_and_audits_denial(sqlite_db) -> None:
    auth_config = _scoped_auth_config()
    service, _ = await deploy_service(
        sqlite_db,
        approval_resume_graph(graph_id="graph-tenant-run-read"),
        auth_config=auth_config,
        tenant_id="tenant-a",
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        auth_config=auth_config,
    )
    app.state.bootstrap = service

    with TestClient(app) as client:
        create_response = client.post(
            "/runs",
            json={"input_payload": {"value": 3}},
            headers=_headers("tenant-a-operator-key"),
        )
        run_id = create_response.json()["run_id"]
        response = client.get(
            f"/runs/{run_id}",
            headers=_headers("tenant-b-operator-key"),
        )

    assert response.status_code == 404
    assert response.json() == {"detail": "run not found"}
    denials = [
        record
        for record in await service.audit_repository.list_by_node("service.authorization")
        if record.error == "scope mismatch"
    ]
    assert denials


async def test_cross_tenant_approval_resolution_is_hidden(sqlite_db) -> None:
    auth_config = _scoped_auth_config()
    service, _ = await deploy_service(
        sqlite_db,
        approval_resume_graph(graph_id="graph-tenant-approval"),
        auth_config=auth_config,
        tenant_id="tenant-a",
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        auth_config=auth_config,
    )
    app.state.bootstrap = service

    with TestClient(app) as client:
        create_response = client.post(
            "/runs",
            json={"input_payload": {"value": 3}},
            headers=_headers("tenant-a-operator-key"),
        )
        run_id = create_response.json()["run_id"]
        wait_for(
            lambda: (
                client.get(
                    f"/runs/{run_id}",
                    headers=_headers("tenant-a-operator-key"),
                ).json()["status"]
                == "paused_for_approval"
            )
        )
        approval_id = client.get(
            f"/runs/{run_id}",
            headers=_headers("tenant-a-operator-key"),
        ).json()["approval_paused_state"]["approval_id"]
        response = client.post(
            f"/deployments/{service.deployment.deployment_ref}/approvals/{approval_id}/resolve",
            json={"decision": "approve"},
            headers=_headers("tenant-b-reviewer-key"),
        )

    assert response.status_code == 404
    assert response.json() == {"detail": "deployment not found"}
