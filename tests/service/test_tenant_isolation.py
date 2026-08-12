from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import content_capture
from tests.service.helpers import approval_resume_graph, deploy_service, wait_for
from zeroth.governance.identity import ServiceRole
from zeroth.platform.artifacts.store import FilesystemArtifactStore
from zeroth.service.api.authentication import ServiceAuthConfig, StaticApiKeyCredential
from zeroth.service.bootstrap.factory import bootstrap_scoped_app as bootstrap_app


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
            StaticApiKeyCredential(
                credential_id="tenant-c-operator",
                secret="tenant-c-operator-key",
                subject="tenant-c-operator",
                roles=[ServiceRole.OPERATOR],
                tenant_id="tenant-c",
            ),
            StaticApiKeyCredential(
                credential_id="tenant-c-reviewer",
                secret="tenant-c-reviewer-key",
                subject="tenant-c-reviewer",
                roles=[ServiceRole.REVIEWER],
                tenant_id="tenant-c",
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
        tenant_id=service.deployment.tenant_id,
        workspace_id=service.deployment.workspace_id,
        auth_config=auth_config,
    )
    app.state.bootstrap = service
    # The denial's reason is free-form ``error`` text, which the default
    # metadata-only capture replaces with a marker; asserting on it is asserting
    # about a deployment that classifies its audits into content.
    content_capture(service.audit_repository)

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
    assert response.json() == {"detail": "deployment not found"}
    denials = [
        record
        for record in await service.audit_repository.list_by_node("service.authorization")
        if record.error == "scope mismatch"
    ]
    assert denials


async def test_cross_tenant_permission_only_manifest_read_is_hidden_and_audited(sqlite_db) -> None:
    """Permission authorization alone must not disclose a served deployment."""
    auth_config = _scoped_auth_config()
    service, _ = await deploy_service(
        sqlite_db,
        approval_resume_graph(graph_id="graph-tenant-manifest"),
        auth_config=auth_config,
        tenant_id="tenant-a",
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        tenant_id=service.deployment.tenant_id,
        workspace_id=service.deployment.workspace_id,
        auth_config=auth_config,
    )
    app.state.bootstrap = service
    content_capture(service.audit_repository)

    with TestClient(app) as client:
        foreign = client.get("/manifests", headers=_headers("tenant-b-operator-key"))
        unknown = client.get(
            "/deployments/not-a-deployment/metadata", headers=_headers("tenant-a-operator-key")
        )

    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json() == {"detail": "deployment not found"}
    denials = [
        record
        for record in await service.audit_repository.list_by_node("service.authorization")
        if record.error == "scope mismatch"
    ]
    assert denials


async def test_cross_tenant_permission_routes_hide_every_deployment_surface(sqlite_db) -> None:
    """The shared permission gate scopes reads, writes, deletes, execution, and Studio."""
    auth_config = _scoped_auth_config()
    service, _ = await deploy_service(
        sqlite_db,
        approval_resume_graph(graph_id="graph-tenant-central-scope"),
        auth_config=auth_config,
        tenant_id="tenant-a",
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        tenant_id=service.deployment.tenant_id,
        workspace_id=service.deployment.workspace_id,
        auth_config=auth_config,
    )
    app.state.bootstrap = service

    requests = [
        ("get", "/connectors", None, "tenant-b-operator-key"),
        (
            "post",
            "/connectors",
            {"ref": "foreign", "backend_type": "ephemeral"},
            "tenant-b-operator-key",
        ),
        ("delete", "/connectors/foreign", None, "tenant-b-operator-key"),
        ("post", "/runs", {"input_payload": {"value": 1}}, "tenant-b-operator-key"),
        ("get", "/runs/not-a-run", None, "tenant-b-operator-key"),
        (
            "post",
            f"/deployments/{service.deployment.deployment_ref}/approvals/missing/resolve",
            {"decision": "approve"},
            "tenant-b-reviewer-key",
        ),
        ("get", "/api/studio/v1/workflows", None, "tenant-b-operator-key"),
    ]
    with TestClient(app) as client:
        unknown = client.get(
            "/deployments/not-a-deployment/metadata", headers=_headers("tenant-a-operator-key")
        )
        responses = [
            client.request(method, path, json=body, headers=_headers(secret))
            for method, path, body, secret in requests
        ]

    assert unknown.status_code == 404
    assert unknown.json() == {"detail": "deployment not found"}
    for response in responses:
        assert response.status_code == unknown.status_code
        assert response.json() == unknown.json()


async def test_same_tenant_other_workspace_is_hidden_by_central_permission_scope(sqlite_db) -> None:
    auth_config = ServiceAuthConfig(
        api_keys=[
            StaticApiKeyCredential(
                credential_id="tenant-a-workspace-a",
                secret="tenant-a-workspace-a-key",
                subject="tenant-a-workspace-a",
                roles=[ServiceRole.OPERATOR],
                tenant_id="tenant-a",
                workspace_id="workspace-a",
            ),
            StaticApiKeyCredential(
                credential_id="tenant-a-workspace-b",
                secret="tenant-a-workspace-b-key",
                subject="tenant-a-workspace-b",
                roles=[ServiceRole.OPERATOR],
                tenant_id="tenant-a",
                workspace_id="workspace-b",
            ),
        ]
    )
    service, _ = await deploy_service(
        sqlite_db,
        approval_resume_graph(graph_id="graph-workspace-scope"),
        auth_config=auth_config,
        tenant_id="tenant-a",
        workspace_id="workspace-a",
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        tenant_id=service.deployment.tenant_id,
        workspace_id=service.deployment.workspace_id,
        auth_config=auth_config,
    )
    app.state.bootstrap = service
    content_capture(service.audit_repository)

    with TestClient(app) as client:
        foreign = client.get("/manifests", headers=_headers("tenant-a-workspace-b-key"))
        unknown = client.get(
            "/deployments/not-a-deployment/metadata",
            headers=_headers("tenant-a-workspace-a-key"),
        )

    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json() == unknown.json() == {"detail": "deployment not found"}
    assert any(
        record.error == "scope mismatch"
        for record in await service.audit_repository.list_by_node("service.authorization")
    )


async def test_foreign_tenant_artifact_retrieval_is_hidden_before_store_lookup(
    sqlite_db, tmp_path, monkeypatch
) -> None:
    auth_config = _scoped_auth_config()
    service, _ = await deploy_service(
        sqlite_db,
        approval_resume_graph(graph_id="graph-artifact-scope"),
        auth_config=auth_config,
        tenant_id="tenant-a",
    )
    store = FilesystemArtifactStore(base_dir=str(tmp_path))
    await store.store("guessed-artifact-key", b"secret artifact", "application/octet-stream")
    retrieved: list[str] = []
    original_retrieve = store.retrieve

    async def observe_retrieve(artifact_id: str) -> bytes:
        retrieved.append(artifact_id)
        return await original_retrieve(artifact_id)

    monkeypatch.setattr(store, "retrieve", observe_retrieve)
    service.artifact_store = store
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        tenant_id=service.deployment.tenant_id,
        workspace_id=service.deployment.workspace_id,
        auth_config=auth_config,
    )
    app.state.bootstrap = service

    with TestClient(app) as client:
        known = client.get(
            "/v1/artifacts/guessed-artifact-key", headers=_headers("tenant-b-reviewer-key")
        )
        unknown = client.get(
            "/v1/artifacts/not-a-real-key", headers=_headers("tenant-b-reviewer-key")
        )

    assert known.status_code == unknown.status_code == 404
    assert known.json() == unknown.json() == {"detail": "deployment not found"}
    assert retrieved == []


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
        tenant_id=service.deployment.tenant_id,
        workspace_id=service.deployment.workspace_id,
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
