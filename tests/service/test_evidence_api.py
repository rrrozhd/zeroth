from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from tests.service.helpers import admin_headers, approval_graph, deploy_service
from zeroth.core.audit import MemoryAccessRecord, NodeAuditRecord, ToolCallRecord
from zeroth.core.runs import Run
from zeroth.core.service.bootstrap import bootstrap_app


async def _seed_run_evidence(service) -> Run:
    run = await service.run_repository.create(
        Run(
            run_id="run-evidence",
            thread_id="thread-evidence",
            graph_version_ref=service.deployment.graph_version_ref,
            deployment_ref=service.deployment.deployment_ref,
            tenant_id=service.deployment.tenant_id,
            workspace_id=service.deployment.workspace_id,
        )
    )
    await service.approval_service.create_pending(
        run=run,
        node=service.graph.nodes[0],
        input_payload={"secret": "hidden", "value": 7},
    )
    await service.audit_repository.write(
        NodeAuditRecord(
            audit_id="audit:evidence:1",
            run_id=run.run_id,
            thread_id=run.thread_id,
            node_id="approval-step",
            node_version=1,
            graph_version_ref=run.graph_version_ref,
            deployment_ref=run.deployment_ref,
            tenant_id=run.tenant_id,
            workspace_id=run.workspace_id,
            attempt=1,
            status="completed",
            input_snapshot={"value": 7},
            output_snapshot={"value": 8},
            tool_calls=[
                ToolCallRecord(
                    tool_ref="tool://search",
                    alias="search",
                    arguments={"query": "bootstrap_service"},
                    outcome={"result": "ok"},
                )
            ],
            memory_interactions=[
                MemoryAccessRecord(
                    memory_ref="memory://thread",
                    connector_type="thread",
                    scope="thread",
                    operation="read",
                    key="latest",
                    value={"value": 7},
                )
            ],
            started_at=datetime(2026, 3, 27, tzinfo=UTC),
            completed_at=datetime(2026, 3, 27, 0, 0, 1, tzinfo=UTC),
        )
    )
    await service.audit_repository.write(
        NodeAuditRecord(
            audit_id="audit:evidence:2",
            run_id=run.run_id,
            thread_id=run.thread_id,
            node_id="service.authorization",
            node_version=1,
            graph_version_ref=run.graph_version_ref,
            deployment_ref=run.deployment_ref,
            tenant_id=run.tenant_id,
            workspace_id=run.workspace_id,
            attempt=1,
            status="rejected",
            error="capability denied: secret_access",
            started_at=datetime(2026, 3, 27, 0, 0, 2, tzinfo=UTC),
            completed_at=datetime(2026, 3, 27, 0, 0, 3, tzinfo=UTC),
        )
    )
    return run


async def test_run_and_deployment_evidence_bundles_include_governance_lineage(sqlite_db) -> None:
    service, deployment = await deploy_service(sqlite_db, approval_graph(graph_id="graph-evidence"))
    run = await _seed_run_evidence(service)
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=deployment.deployment_ref,
        auth_config=service.auth_config,
    )
    app.state.bootstrap = service

    with TestClient(app) as client:
        run_response = client.get(f"/runs/{run.run_id}/evidence", headers=admin_headers())
        deployment_response = client.get(
            f"/deployments/{deployment.deployment_ref}/evidence",
            headers=admin_headers(),
        )

    assert run_response.status_code == 200
    run_payload = run_response.json()
    assert run_payload["run"]["run_id"] == run.run_id
    assert run_payload["run"]["evidence_ref"] == f"/runs/{run.run_id}/evidence"
    assert run_payload["summary"]["tool_call_count"] == 1
    assert run_payload["summary"]["memory_interaction_count"] == 1
    assert run_payload["summary"]["approval_count"] == 1
    assert run_payload["policy_events"] == ["capability denied: secret_access"]
    assert run_payload["approvals"][0]["run_id"] == run.run_id

    assert deployment_response.status_code == 200
    deployment_payload = deployment_response.json()
    assert deployment_payload["deployment"]["deployment_ref"] == deployment.deployment_ref
    assert deployment_payload["deployment"]["evidence_ref"] == (
        f"/deployments/{deployment.deployment_ref}/evidence"
    )
    assert deployment_payload["summary"]["audit_count"] == 2
    assert deployment_payload["summary"]["approval_count"] == 1
    assert deployment_payload["run_ids"] == [run.run_id]


async def test_deployment_attestation_verification_detects_snapshot_tampering(sqlite_db) -> None:
    service, deployment = await deploy_service(
        sqlite_db, approval_graph(graph_id="graph-attestation")
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=deployment.deployment_ref,
        auth_config=service.auth_config,
    )
    app.state.bootstrap = service

    with TestClient(app) as client:
        attestation_response = client.get(
            f"/deployments/{deployment.deployment_ref}/attestation",
            headers=admin_headers(),
        )

    assert attestation_response.status_code == 200
    attestation = attestation_response.json()
    assert attestation["deployment_ref"] == deployment.deployment_ref
    assert attestation["graph_snapshot_digest"]
    assert attestation["contract_snapshot_digest"]
    assert attestation["settings_snapshot_digest"]
    assert attestation["attestation_digest"]

    with TestClient(app) as client:
        verify_response = client.post(
            f"/deployments/{deployment.deployment_ref}/verify-attestation",
            json=attestation,
            headers=admin_headers(),
        )

    assert verify_response.status_code == 200
    assert verify_response.json() == {"verified": True, "mismatches": []}

    async with sqlite_db.transaction() as connection:
        await connection.execute(
            """
            UPDATE deployment_versions
            SET serialized_graph = ?
            WHERE deployment_id = ?
            """,
            ("tampered-graph", deployment.deployment_id),
        )

    with TestClient(app) as client:
        failed_verify = client.post(
            f"/deployments/{deployment.deployment_ref}/verify-attestation",
            json=attestation,
            headers=admin_headers(),
        )

    assert failed_verify.status_code == 200
    assert failed_verify.json()["verified"] is False
    assert "graph_snapshot_digest" in failed_verify.json()["mismatches"]
