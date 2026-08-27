"""Separately addressable API masking nodes for matrix-bound tenant surfaces."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from tests.service.helpers import approval_resume_graph, deploy_service
from tests.service.test_tenant_isolation import _headers, _scoped_auth_config
from zeroth.governance.approvals.models import ApprovalRecord
from zeroth.governance.audit import NodeAuditRecord
from zeroth.runtime.runs import Run, RunStatus
from zeroth.service.bootstrap.factory import bootstrap_scoped_app


async def _app(sqlite_db, suffix: str):
    auth = _scoped_auth_config()
    service, deployment = await deploy_service(
        sqlite_db,
        approval_resume_graph(graph_id=f"api-isolation-{suffix}"),
        deployment_ref=f"api-isolation-{suffix}",
        auth_config=auth,
        tenant_id="tenant-a",
    )
    app = await bootstrap_scoped_app(
        sqlite_db,
        deployment_ref=deployment.deployment_ref,
        tenant_id=deployment.tenant_id,
        workspace_id=deployment.workspace_id,
        auth_config=auth,
    )
    app.state.bootstrap = service
    return app, service, deployment


def _assert_same_404(foreign, unknown) -> None:
    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json() == unknown.json() == {"detail": "deployment not found"}


async def test_run_execute_api_foreign_matches_unknown_tenant(sqlite_db) -> None:
    app, _, _ = await _app(sqlite_db, "run-execute")
    with TestClient(app) as client:
        foreign = client.post(
            "/runs", json={"input_payload": {"value": 1}}, headers=_headers("tenant-b-operator-key")
        )
        unknown = client.post(
            "/runs", json={"input_payload": {"value": 1}}, headers=_headers("tenant-c-operator-key")
        )
    _assert_same_404(foreign, unknown)


async def test_run_read_api_foreign_id_matches_unknown(sqlite_db) -> None:
    app, service, deployment = await _app(sqlite_db, "run-read")
    await service.run_repository.create(
        Run(
            run_id="owner-run-read",
            thread_id="owner-thread-read",
            graph_version_ref=deployment.graph_version_ref,
            deployment_ref=deployment.deployment_ref,
            tenant_id="tenant-a",
        )
    )
    with TestClient(app) as client:
        foreign = client.get("/runs/owner-run-read", headers=_headers("tenant-b-operator-key"))
        unknown = client.get("/runs/unknown-run", headers=_headers("tenant-b-operator-key"))
    _assert_same_404(foreign, unknown)


async def test_execution_result_api_foreign_id_matches_unknown(sqlite_db) -> None:
    app, service, deployment = await _app(sqlite_db, "run-result")
    run = Run(
        run_id="owner-run-result",
        thread_id="owner-thread-result",
        graph_version_ref=deployment.graph_version_ref,
        deployment_ref=deployment.deployment_ref,
        tenant_id="tenant-a",
        status=RunStatus.COMPLETED,
        final_output={"secret": "owner-only"},
    )
    await service.run_repository.create(run)
    with TestClient(app) as client:
        foreign = client.get("/runs/owner-run-result", headers=_headers("tenant-b-operator-key"))
        unknown = client.get("/runs/unknown-result", headers=_headers("tenant-b-operator-key"))
    _assert_same_404(foreign, unknown)


async def test_connector_enumerate_api_foreign_matches_unknown_tenant(sqlite_db) -> None:
    app, _, _ = await _app(sqlite_db, "connector-enumerate")
    with TestClient(app) as client:
        foreign = client.get("/v1/connectors", headers=_headers("tenant-b-operator-key"))
        unknown = client.get("/v1/connectors", headers=_headers("tenant-c-operator-key"))
    _assert_same_404(foreign, unknown)


async def test_connector_write_api_foreign_matches_unknown_tenant(sqlite_db) -> None:
    app, _, _ = await _app(sqlite_db, "connector-write")
    payload = {"ref": "foreign-write", "backend_type": "ephemeral", "params": {}}
    with TestClient(app) as client:
        foreign = client.post(
            "/v1/connectors", json=payload, headers=_headers("tenant-b-operator-key")
        )
        unknown = client.post(
            "/v1/connectors", json=payload, headers=_headers("tenant-c-operator-key")
        )
    _assert_same_404(foreign, unknown)


async def test_connector_delete_api_foreign_id_matches_unknown(sqlite_db) -> None:
    app, service, _ = await _app(sqlite_db, "connector-delete")
    await service.memory_connector_config_repository.upsert(
        "owner-connector", "ephemeral", {}, tenant_id="tenant-a"
    )
    with TestClient(app) as client:
        foreign = client.delete(
            "/v1/connectors/owner-connector", headers=_headers("tenant-b-operator-key")
        )
        unknown = client.delete(
            "/v1/connectors/unknown-connector", headers=_headers("tenant-b-operator-key")
        )
    _assert_same_404(foreign, unknown)


async def _approval(sqlite_db, suffix: str):
    app, service, deployment = await _app(sqlite_db, suffix)
    record = await service.approval_service.repository.write(
        ApprovalRecord(
            approval_id=f"owner-approval-{suffix}",
            run_id=f"owner-run-{suffix}",
            node_id="approval",
            graph_version_ref=deployment.graph_version_ref,
            deployment_ref=deployment.deployment_ref,
            tenant_id="tenant-a",
            summary="owner",
            rationale="owner",
        )
    )
    await service.run_repository.create(
        Run(
            run_id=record.run_id,
            thread_id=f"owner-approval-thread-{suffix}",
            graph_version_ref=deployment.graph_version_ref,
            deployment_ref=deployment.deployment_ref,
            tenant_id="tenant-a",
        )
    )
    return app, service, deployment, record


async def test_approval_enumerate_api_foreign_matches_unknown(sqlite_db, monkeypatch) -> None:
    app, service, deployment, _ = await _approval(sqlite_db, "approval-enumerate")
    calls = []
    # Record the method the route actually calls. Deployment and graph scoping
    # moved out of the repository query into `_filter_visible_records`, so
    # `list_pending` now legitimately receives only tenant/workspace and pinning
    # it there asserted an internal shape rather than the API's scoping.
    original = service.approval_service.list_pending_visible_to_deployment

    async def recording_list_pending(**scope):
        calls.append(scope)
        return await original(**scope)

    monkeypatch.setattr(
        service.approval_service,
        "list_pending_visible_to_deployment",
        recording_list_pending,
    )
    with TestClient(app) as client:
        assert (
            client.get(
                f"/deployments/{deployment.deployment_ref}/approvals",
                headers=_headers("tenant-a-reviewer-key"),
            ).status_code
            == 200
        )
        foreign = client.get(
            f"/deployments/{deployment.deployment_ref}/approvals",
            headers=_headers("tenant-b-reviewer-key"),
        )
        unknown = client.get(
            "/deployments/unknown-deployment/approvals",
            headers=_headers("tenant-a-reviewer-key"),
        )
    _assert_same_404(foreign, unknown)
    assert calls == [
        {
            "run_id": None,
            "thread_id": None,
            "deployment_ref": deployment.deployment_ref,
            "graph_version_ref": deployment.graph_version_ref,
            "tenant_id": "tenant-a",
            "workspace_id": None,
        }
    ]


async def test_approval_retrieve_api_foreign_id_matches_unknown(sqlite_db, monkeypatch) -> None:
    app, service, deployment, record = await _approval(sqlite_db, "approval-retrieve")
    calls = []
    # Same move as the enumerate case: the route reads through
    # `get_visible_to_deployment`, which is where the deployment and graph scope
    # is applied, so that is the call whose scope this test is about.
    original = service.approval_service.get_visible_to_deployment

    async def recording_get(approval_id, **scope):
        calls.append(scope)
        return await original(approval_id, **scope)

    monkeypatch.setattr(
        service.approval_service, "get_visible_to_deployment", recording_get
    )
    base = f"/deployments/{deployment.deployment_ref}/approvals"
    with TestClient(app) as client:
        assert (
            client.get(
                f"{base}/{record.approval_id}", headers=_headers("tenant-a-reviewer-key")
            ).status_code
            == 200
        )
        foreign = client.get(
            f"{base}/{record.approval_id}", headers=_headers("tenant-b-reviewer-key")
        )
        unknown = client.get(f"{base}/unknown-approval", headers=_headers("tenant-b-reviewer-key"))
    _assert_same_404(foreign, unknown)
    assert calls == [
        {
            "tenant_id": "tenant-a",
            "workspace_id": None,
            "deployment_ref": deployment.deployment_ref,
            "graph_version_ref": deployment.graph_version_ref,
        }
    ]


async def test_approval_resolve_api_foreign_id_matches_unknown(sqlite_db, monkeypatch) -> None:
    app, service, deployment, record = await _approval(sqlite_db, "approval-resolve")
    calls = []
    original = service.approval_service.resolve

    async def recording_resolve(approval_id, **scope):
        calls.append(scope)
        return await original(approval_id, **scope)

    monkeypatch.setattr(service.approval_service, "resolve", recording_resolve)
    base = f"/deployments/{deployment.deployment_ref}/approvals"
    with TestClient(app) as client:
        assert (
            client.post(
                f"{base}/{record.approval_id}/resolve",
                json={"decision": "approve"},
                headers=_headers("tenant-a-reviewer-key"),
            ).status_code
            == 409
        )
        foreign = client.post(
            f"{base}/{record.approval_id}/resolve",
            json={"decision": "approve"},
            headers=_headers("tenant-b-reviewer-key"),
        )
        unknown = client.post(
            f"{base}/unknown-approval/resolve",
            json={"decision": "approve"},
            headers=_headers("tenant-b-reviewer-key"),
        )
    _assert_same_404(foreign, unknown)
    assert len(calls) == 1
    assert {
        key: calls[0][key]
        for key in ("tenant_id", "workspace_id", "deployment_ref", "graph_version_ref")
    } == {
        "tenant_id": "tenant-a",
        "workspace_id": None,
        "deployment_ref": deployment.deployment_ref,
        "graph_version_ref": deployment.graph_version_ref,
    }


async def _audit(sqlite_db, suffix: str):
    app, service, deployment = await _app(sqlite_db, suffix)
    run_id = f"owner-audit-run-{suffix}"
    await service.run_repository.create(
        Run(
            run_id=run_id,
            thread_id=f"owner-audit-thread-{suffix}",
            graph_version_ref=deployment.graph_version_ref,
            deployment_ref=deployment.deployment_ref,
            tenant_id="tenant-a",
        )
    )
    await service.audit_repository.write(
        NodeAuditRecord(
            audit_id=f"owner-audit-{suffix}",
            run_id=run_id,
            node_id="node",
            graph_version_ref=deployment.graph_version_ref,
            deployment_ref=deployment.deployment_ref,
            tenant_id="tenant-a",
            status="completed",
            started_at=datetime(2026, 8, 9, tzinfo=UTC),
            completed_at=datetime(2026, 8, 9, 0, 0, 1, tzinfo=UTC),
        )
    )
    return app, service, deployment, run_id


async def test_audit_enumerate_api_foreign_matches_unknown(sqlite_db, monkeypatch) -> None:
    app, service, deployment, _ = await _audit(sqlite_db, "audit-enumerate")
    queries = []
    original = service.audit_repository.list

    async def recording_list(query):
        queries.append(query)
        return await original(query)

    monkeypatch.setattr(service.audit_repository, "list", recording_list)
    with TestClient(app) as client:
        assert (
            client.get(
                f"/deployments/{deployment.deployment_ref}/audits",
                headers=_headers("tenant-a-reviewer-key"),
            ).status_code
            == 200
        )
        foreign = client.get(
            f"/deployments/{deployment.deployment_ref}/audits",
            headers=_headers("tenant-b-reviewer-key"),
        )
        unknown = client.get(
            "/deployments/unknown-deployment/audits",
            headers=_headers("tenant-a-reviewer-key"),
        )
    _assert_same_404(foreign, unknown)
    assert len(queries) == 1
    assert queries[0].tenant_id == "tenant-a"
    assert queries[0].workspace_id is None
    assert queries[0].workspace_scoped is True


async def test_audit_retrieve_api_foreign_run_matches_unknown(sqlite_db, monkeypatch) -> None:
    app, service, deployment, run_id = await _audit(sqlite_db, "audit-retrieve")
    calls = []
    original = service.audit_repository.list_by_run

    async def recording_list_by_run(target_run_id, **scope):
        calls.append(scope)
        return await original(target_run_id, **scope)

    monkeypatch.setattr(service.audit_repository, "list_by_run", recording_list_by_run)
    with TestClient(app) as client:
        assert (
            client.get(
                f"/runs/{run_id}/timeline", headers=_headers("tenant-a-reviewer-key")
            ).status_code
            == 200
        )
        foreign = client.get(f"/runs/{run_id}/timeline", headers=_headers("tenant-b-reviewer-key"))
        unknown = client.get(
            "/runs/unknown-run/timeline", headers=_headers("tenant-b-reviewer-key")
        )
    _assert_same_404(foreign, unknown)
    assert calls == [
        {
            "tenant_id": "tenant-a",
            "workspace_id": None,
            "workspace_scoped": True,
            "deployment_ref": deployment.deployment_ref,
        }
    ]
