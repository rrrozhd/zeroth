from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from tests.service.helpers import admin_headers, agent_graph, deploy_service
from zeroth.audit import NodeAuditRecord
from zeroth.service.bootstrap import bootstrap_app


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


def test_run_and_deployment_metadata_expose_phase7_discoverability_refs(sqlite_db) -> None:
    service, deployment = deploy_service(sqlite_db, agent_graph(graph_id="graph-audit-refs"))
    app = bootstrap_app(
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


def test_audit_api_lists_deployment_audits_with_redaction(sqlite_db) -> None:
    service, deployment = deploy_service(sqlite_db, agent_graph(graph_id="graph-audit-list"))
    service.audit_repository.write(
        _record(
            audit_id="audit:1",
            run_id="run-1",
            deployment_ref=deployment.deployment_ref,
        )
    )
    service.audit_repository.write(
        _record(
            audit_id="audit:2",
            run_id="run-2",
            deployment_ref="other-deployment",
        )
    )
    app = bootstrap_app(
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


def test_audit_api_exposes_run_and_deployment_timelines_in_order(sqlite_db) -> None:
    service, deployment = deploy_service(sqlite_db, agent_graph(graph_id="graph-audit-timeline"))
    service.audit_repository.write(
        _record(
            audit_id="audit:late",
            run_id="run-1",
            deployment_ref=deployment.deployment_ref,
            node_id="finish",
            started_at=datetime(2026, 3, 27, 0, 0, 2, tzinfo=UTC),
        )
    )
    service.audit_repository.write(
        _record(
            audit_id="audit:early",
            run_id="run-1",
            deployment_ref=deployment.deployment_ref,
            node_id="start",
            started_at=datetime(2026, 3, 27, 0, 0, 1, tzinfo=UTC),
        )
    )
    app = bootstrap_app(
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
