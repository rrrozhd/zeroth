from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from tests.conftest import content_capture
from tests.service.helpers import admin_headers, agent_graph, deploy_service, operator_headers
from zeroth.governance.audit import NodeAuditRecord
from zeroth.service.bootstrap import bootstrap_app
from zeroth.runtime.runs import Run


def _record(
    *,
    audit_id: str,
    run_id: str,
    deployment_ref: str,
    node_id: str = "node",
    thread_id: str = "thread-1",
    started_at: datetime | None = None,
) -> NodeAuditRecord:
    return NodeAuditRecord(
        tenant_id="default",
        workspace_id=None,
        audit_id=audit_id,
        run_id=run_id,
        thread_id=thread_id,
        node_id=node_id,
        node_version=1,
        graph_version_ref="graph-audit@1",
        deployment_ref=deployment_ref,
        attempt=1,
        status="completed",
        input_snapshot={"secret": "top-secret", "value": 1},
        output_snapshot={"token": "abc123", "value": 2},
        execution_metadata={"password": "hidden", "safe": True},
        started_at=started_at or datetime(2026, 3, 27, tzinfo=UTC),
        completed_at=datetime(2026, 3, 27, 0, 0, 1, tzinfo=UTC),
    )


async def test_run_and_deployment_metadata_expose_phase7_discoverability_refs(sqlite_db) -> None:
    service, deployment = await deploy_service(sqlite_db, agent_graph(graph_id="graph-audit-refs"))
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        auth_config=service.auth_config,
    )
    app.state.bootstrap = service

    with TestClient(app) as client:
        create = client.post(
            "/runs",
            json={"input_payload": {"value": 3}},
            headers=admin_headers(),
        )
        run_payload = create.json()
        metadata = client.get(
            f"/deployments/{deployment.deployment_ref}/metadata",
            headers=admin_headers(),
        ).json()

    assert create.status_code == 202
    assert run_payload["timeline_ref"] == f"/runs/{run_payload['run_id']}/timeline"
    assert run_payload["evidence_ref"] == f"/runs/{run_payload['run_id']}/evidence"
    assert metadata["audit_ref"] == f"/deployments/{deployment.deployment_ref}/audits"
    assert metadata["timeline_ref"] == f"/deployments/{deployment.deployment_ref}/timeline"
    assert metadata["evidence_ref"] == f"/deployments/{deployment.deployment_ref}/evidence"
    assert metadata["attestation_ref"] == f"/deployments/{deployment.deployment_ref}/attestation"


async def test_audit_api_lists_deployment_audits_with_redaction(sqlite_db) -> None:
    service, deployment = await deploy_service(sqlite_db, agent_graph(graph_id="graph-audit-list"))
    await content_capture(service.audit_repository).write(
        _record(
            audit_id="audit:1",
            run_id="run-1",
            deployment_ref=deployment.deployment_ref,
        )
    )
    await service.audit_repository.write(
        _record(
            audit_id="audit:2",
            run_id="run-2",
            deployment_ref="other-deployment",
        )
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=deployment.deployment_ref,
        auth_config=service.auth_config,
    )
    app.state.bootstrap = service

    with TestClient(app) as client:
        response = client.get(
            f"/deployments/{deployment.deployment_ref}/audits",
            params={"run_id": "run-1"},
            headers=admin_headers(),
        )

    assert response.status_code == 200
    payload = response.json()
    assert [record["audit_id"] for record in payload["records"]] == ["audit:1"]
    assert payload["records"][0]["input_snapshot"]["secret"] == "***REDACTED***"
    assert payload["records"][0]["output_snapshot"]["token"] == "***REDACTED***"
    assert payload["records"][0]["execution_metadata"]["password"] == "***REDACTED***"


async def test_audit_api_lists_tenant_wide_audits_across_deployments(sqlite_db) -> None:
    service, deployment = await deploy_service(sqlite_db, agent_graph(graph_id="graph-audit-all"))
    for audit_id, deployment_ref in (
        ("audit:serving", deployment.deployment_ref),
        ("audit:other", "other-deployment"),
    ):
        await service.audit_repository.write(
            _record(audit_id=audit_id, run_id=f"run-{audit_id}", deployment_ref=deployment_ref)
        )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=deployment.deployment_ref,
        auth_config=service.auth_config,
    )
    app.state.bootstrap = service

    with TestClient(app) as client:
        response = client.get("/admin/audits", headers=admin_headers())

    assert response.status_code == 200, response.text
    assert {record["audit_id"] for record in response.json()["records"]} == {
        "audit:serving",
        "audit:other",
    }
    assert response.json()["scope"] == "tenant:default"


async def test_audit_inspection_resolves_listed_non_serving_deployment(sqlite_db) -> None:
    """Audit detail uses registry scope rather than the process-bound ref."""
    service, serving = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-audit-serving"),
        deployment_ref="audit-serving",
    )
    inspected = await service.deployment_service.deploy(
        "audit-inspected",
        serving.graph_id,
        serving.graph_version,
        tenant_id=serving.tenant_id,
        workspace_id=serving.workspace_id,
    )
    await service.audit_repository.write(
        _record(
            audit_id="audit:inspected",
            run_id="run-inspected",
            deployment_ref=inspected.deployment_ref,
        )
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=serving.deployment_ref,
        auth_config=service.auth_config,
    )
    app.state.bootstrap = service

    paths = ("audits", "timeline", "evidence", "audit-verification")
    with TestClient(app) as client:
        responses = {
            path: client.get(
                f"/deployments/{inspected.deployment_ref}/{path}",
                headers=admin_headers(),
            )
            for path in paths
        }

    assert service.deployment.deployment_ref == serving.deployment_ref
    assert {path: response.status_code for path, response in responses.items()} == {
        path: 200 for path in paths
    }
    assert responses["audits"].json()["records"][0]["audit_id"] == "audit:inspected"
    assert responses["timeline"].json()["entries"][0]["audit_id"] == "audit:inspected"


async def test_audit_api_exposes_run_and_deployment_timelines_in_order(sqlite_db) -> None:
    service, deployment = await deploy_service(
        sqlite_db, agent_graph(graph_id="graph-audit-timeline")
    )
    await service.audit_repository.write(
        _record(
            audit_id="audit:late",
            run_id="run-1",
            deployment_ref=deployment.deployment_ref,
            node_id="finish",
            started_at=datetime(2026, 3, 27, 0, 0, 2, tzinfo=UTC),
        )
    )
    await service.audit_repository.write(
        _record(
            audit_id="audit:early",
            run_id="run-1",
            deployment_ref=deployment.deployment_ref,
            node_id="start",
            started_at=datetime(2026, 3, 27, 0, 0, 1, tzinfo=UTC),
        )
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=deployment.deployment_ref,
        auth_config=service.auth_config,
    )
    app.state.bootstrap = service

    with TestClient(app) as client:
        run_timeline = client.get("/runs/run-1/timeline", headers=admin_headers())
        deployment_timeline = client.get(
            f"/deployments/{deployment.deployment_ref}/timeline",
            headers=admin_headers(),
        )

    assert run_timeline.status_code == 200
    assert [entry["audit_id"] for entry in run_timeline.json()["entries"]] == [
        "audit:early",
        "audit:late",
    ]
    assert deployment_timeline.status_code == 200
    assert [entry["audit_id"] for entry in deployment_timeline.json()["entries"]] == [
        "audit:early",
        "audit:late",
    ]


async def test_run_evidence_and_verification_include_composed_deployment_records(
    sqlite_db,
) -> None:
    """A child run's chain may contain parent-scoped provider instrumentation."""
    service, parent = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-composed-audit"),
        deployment_ref="composed-parent",
    )
    await service.run_repository.create(
        Run(
            run_id="composed-child-run",
            graph_version_ref="composed-child@1",
            deployment_ref="composed-child",
            tenant_id="default",
            workspace_id=None,
        )
    )
    for offset, (audit_id, deployment_ref) in enumerate(
        (
            ("audit:child-entry", "composed-child"),
            ("audit:parent-provider", parent.deployment_ref),
            ("audit:child-agent", "composed-child"),
        )
    ):
        await service.audit_repository.write(
            _record(
                audit_id=audit_id,
                run_id="composed-child-run",
                deployment_ref=deployment_ref,
                started_at=datetime(2026, 3, 27, 0, 0, offset, tzinfo=UTC),
            )
        )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=parent.deployment_ref,
        auth_config=service.auth_config,
    )
    app.state.bootstrap = service

    with TestClient(app) as client:
        timeline = client.get(
            "/runs/composed-child-run/timeline", headers=admin_headers()
        )
        evidence = client.get(
            "/runs/composed-child-run/evidence", headers=admin_headers()
        )
        verification = client.post(
            "/runs/composed-child-run/verify-chain",
            json={},
            headers=admin_headers(),
        )

    assert timeline.status_code == 200, timeline.text
    assert evidence.status_code == 200, evidence.text
    assert verification.status_code == 200, verification.text
    expected = ["audit:child-entry", "audit:parent-provider", "audit:child-agent"]
    assert [entry["audit_id"] for entry in timeline.json()["entries"]] == expected
    assert [record["audit_id"] for record in evidence.json()["audits"]] == expected
    assert verification.json()["verified"] is True
    assert verification.json()["record_count"] == 3


async def test_parent_evidence_aggregates_signed_child_provider_costs_once(sqlite_db) -> None:
    """Parent evidence is a composed view; each run keeps its own signed chain."""
    from zeroth.platform.signing import EnvHmacSigner

    service, deployment = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-composed-parent-evidence"),
        deployment_ref="composed-parent-evidence",
    )
    signer = EnvHmacSigner(key_id="composed-k1", keys={"composed-k1": b"test-only-key"})
    service.signer = signer
    service.audit_repository._signer = signer

    parent = await service.run_repository.create(
        Run(
            run_id="composed-parent-run",
            graph_version_ref=deployment.graph_version_ref,
            deployment_ref=deployment.deployment_ref,
            tenant_id="default",
            workspace_id=None,
        )
    )
    children = []
    for index in range(2):
        children.append(
            await service.run_repository.create(
                Run(
                    run_id=f"composed-child-{index}",
                    graph_version_ref=f"composed-child-{index}@1",
                    deployment_ref=f"composed-child-{index}",
                    tenant_id=parent.tenant_id,
                    workspace_id=parent.workspace_id,
                    parent_run_id=parent.run_id,
                )
            )
        )

    await service.audit_repository.write(
        _record(
            audit_id="audit:parent",
            run_id=parent.run_id,
            deployment_ref=parent.deployment_ref,
        )
    )
    await service.audit_repository.write(
        _record(
            audit_id="audit:parent:branch-rollup",
            run_id=parent.run_id,
            deployment_ref=parent.deployment_ref,
        ).model_copy(
            update={
                "estimated_cost_usd": 0.001,
                "execution_metadata": {"branch_id": "parent:branch:0"},
            }
        )
    )
    for suffix in ("provider", "runtime"):
        await service.audit_repository.write(
            _record(
                audit_id=f"audit:child-0:{suffix}",
                run_id=children[0].run_id,
                deployment_ref=children[0].deployment_ref,
            ).model_copy(
                update={
                    "cost_event_id": "cost-event-child-0",
                    "cost_usd": 0.001,
                }
            )
        )
    await service.audit_repository.write(
        _record(
            audit_id="audit:child-1:provider",
            run_id=children[1].run_id,
            deployment_ref=children[1].deployment_ref,
        ).model_copy(
            update={
                "cost_event_id": "cost-event-child-1",
                "cost_usd": 0.002,
            }
        )
    )
    persisted = {
        record.audit_id: record
        for run in (parent, *children)
        for record in await service.audit_repository.list_by_run(run.run_id)
    }
    assert all(record.record_signature for record in persisted.values())

    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=deployment.deployment_ref,
        auth_config=service.auth_config,
    )
    app.state.bootstrap = service
    with TestClient(app) as client:
        evidence = client.get(f"/runs/{parent.run_id}/evidence", headers=admin_headers())
        verifications = {
            run.run_id: client.get(
                f"/runs/{run.run_id}/audit-verification", headers=admin_headers()
            )
            for run in (parent, *children)
        }

    assert evidence.status_code == 200, evidence.text
    payload = evidence.json()
    assert [row["audit_id"] for row in payload["audits"]] == [
        "audit:parent",
        "audit:parent:branch-rollup",
        "audit:child-0:provider",
        "audit:child-0:runtime",
        "audit:child-1:provider",
    ]
    assert {
        row["audit_id"]: row["record_signature"] for row in payload["audits"]
    } == {
        audit_id: record.record_signature for audit_id, record in persisted.items()
    }
    assert payload["summary"] == {
        "audit_count": 5,
        "approval_count": 0,
        "tool_call_count": 0,
        "memory_interaction_count": 0,
        "priced_call_count": 2,
        "cost_event_count": 2,
        "total_cost_usd": 0.003,
        "cost_identity_state": "correlated",
        "reconciliation_state": "reconciled",
    }
    assert all(response.status_code == 200 for response in verifications.values())
    assert all(response.json()["verified"] is True for response in verifications.values())
    assert all(response.json()["signature_verified"] is True for response in verifications.values())


async def test_parent_evidence_refuses_cross_scope_child_relationship(
    sqlite_db, monkeypatch
) -> None:
    service, deployment = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-composed-scope-refusal"),
        deployment_ref="composed-scope-refusal",
    )
    parent = await service.run_repository.create(
        Run(
            run_id="scope-parent",
            graph_version_ref=deployment.graph_version_ref,
            deployment_ref=deployment.deployment_ref,
            tenant_id="default",
            workspace_id=None,
        )
    )
    foreign = Run(
        run_id="scope-foreign-child",
        graph_version_ref="foreign@1",
        deployment_ref="foreign",
        tenant_id="other-tenant",
        workspace_id=None,
        parent_run_id=parent.run_id,
    )

    async def poisoned_children(parent_run_id: str):
        assert parent_run_id == parent.run_id
        return [foreign]

    monkeypatch.setattr(service.run_repository, "list_child_runs", poisoned_children)
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=deployment.deployment_ref,
        auth_config=service.auth_config,
    )
    app.state.bootstrap = service
    with TestClient(app) as client:
        response = client.get(f"/runs/{parent.run_id}/evidence", headers=admin_headers())

    assert response.status_code == 404
    assert response.json() == {"detail": "run not found"}


async def test_run_audit_routes_forward_tenant_and_workspace_to_run_query(
    sqlite_db, monkeypatch
) -> None:
    service, deployment = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-audit-query-scope"),
        tenant_id="default",
        workspace_id=None,
    )
    await service.run_repository.create(
        Run(
            run_id="query-scoped-run",
            thread_id="query-scoped-thread",
            graph_version_ref=deployment.graph_version_ref,
            deployment_ref=deployment.deployment_ref,
            tenant_id="default",
            workspace_id=None,
        )
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=deployment.deployment_ref,
        auth_config=service.auth_config,
    )
    app.state.bootstrap = service
    calls: list[str] = []
    original_get = service.run_repository.get

    async def recording_get(run_id: str):
        calls.append(run_id)
        return await original_get(run_id)

    monkeypatch.setattr(service.run_repository, "get", recording_get)
    with TestClient(app) as client:
        for path in (
            "/runs/query-scoped-run/timeline",
            "/runs/query-scoped-run/audit-verification",
            "/runs/query-scoped-run/evidence",
        ):
            calls.clear()
            assert client.get(path, headers=admin_headers()).status_code == 200
            assert calls
            assert set(calls) == {"query-scoped-run"}


async def test_reviewer_can_list_audits(sqlite_db) -> None:
    """AUDIT_READ belongs to the reviewer role: evidence review is its purpose."""
    from tests.service.helpers import reviewer_headers

    service, _ = await deploy_service(sqlite_db, agent_graph(graph_id="graph-audit-reviewer"))
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        auth_config=service.auth_config,
    )
    app.state.bootstrap = service

    with TestClient(app) as client:
        r = client.get(
            f"/deployments/{service.deployment.deployment_ref}/audits",
            headers=reviewer_headers(),
        )

    assert r.status_code == 200


async def test_audit_read_denied_for_operator_without_permission(sqlite_db) -> None:
    """Authentication alone does not grant access to governance evidence."""
    service, deployment = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-audit-operator-denied"),
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=deployment.deployment_ref,
        auth_config=service.auth_config,
    )
    app.state.bootstrap = service

    with TestClient(app) as client:
        response = client.get(
            f"/deployments/{deployment.deployment_ref}/audits",
            headers=operator_headers(),
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "forbidden"}


async def test_audit_verification_endpoint_reports_intact_and_tampered_chains(sqlite_db) -> None:
    """GET /deployments/{ref}/audit-verification exposes the continuity verifier:.

    an intact chain verifies; an in-place record edit flips it to failed.
    """
    import json

    service, deployment = await deploy_service(sqlite_db, agent_graph(graph_id="graph-verify"))
    await service.audit_repository.write(
        _record(audit_id="audit:v1", run_id="run-v", deployment_ref=deployment.deployment_ref)
    )
    await service.audit_repository.write(
        _record(
            audit_id="audit:v2",
            run_id="run-v",
            deployment_ref=deployment.deployment_ref,
            node_id="finish",
        )
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=deployment.deployment_ref,
        auth_config=service.auth_config,
    )
    app.state.bootstrap = service

    with TestClient(app) as client:
        ok = client.get(
            f"/deployments/{deployment.deployment_ref}/audit-verification",
            headers=admin_headers(),
        )
        assert ok.status_code == 200, ok.text
        payload = ok.json()
        assert payload["verified"] is True
        assert payload["record_count"] == 2

        tampered = await service.audit_repository.get("audit:v2")
        tampered_payload = tampered.model_copy(update={"status": "tampered"}).model_dump(
            mode="json"
        )
        async with sqlite_db.transaction() as connection:
            await connection.execute(
                "UPDATE node_audits SET record_json = ? WHERE audit_id = ?",
                (json.dumps(tampered_payload, sort_keys=True), "audit:v2"),
            )

        bad = client.get(
            f"/deployments/{deployment.deployment_ref}/audit-verification",
            headers=admin_headers(),
        )
        assert bad.status_code == 200
        payload = bad.json()
        assert payload["verified"] is False
        assert payload["failed_audit_id"] == "audit:v2"
