"""WS-E retention API: policy, legal holds, right-to-erasure, and scoping."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from tests.service.helpers import (
    admin_headers,
    agent_graph,
    api_key_headers,
    deploy_service,
    operator_headers,
    scoped_auth_config,
)

from zeroth.core.audit import NodeAuditRecord
from zeroth.core.identity import ServiceRole
from zeroth.core.runs import Run
from zeroth.core.service.bootstrap import bootstrap_app


def _audit(*, audit_id: str, run_id: str, deployment_ref: str, ssn: str) -> NodeAuditRecord:
    return NodeAuditRecord(
        audit_id=audit_id,
        run_id=run_id,
        node_id="n1",
        graph_version_ref="graph-ret@1",
        deployment_ref=deployment_ref,
        tenant_id="default",
        status="completed",
        input_snapshot={"ssn": ssn},
        output_snapshot={"result": ssn},
        stdout="out-" + ssn,
        error="err-" + ssn,
        started_at=datetime(2026, 7, 11, tzinfo=UTC),
        completed_at=datetime(2026, 7, 11, 0, 0, 1, tzinfo=UTC),
    )


async def _seed_run(service, deployment, run_id: str, ssn: str, n: int = 2) -> None:
    run = Run(
        run_id=run_id,
        graph_version_ref=deployment.graph_version_ref,
        deployment_ref=deployment.deployment_ref,
        tenant_id="default",
        final_output={"answer": ssn},
    )
    await service.run_repository.put(run)
    for i in range(n):
        await service.audit_repository.write(
            _audit(
                audit_id=f"{run_id}-a{i}",
                run_id=run_id,
                deployment_ref=deployment.deployment_ref,
                ssn=ssn,
            )
        )


async def test_put_and_get_retention_policy(sqlite_db) -> None:
    service, _ = await deploy_service(sqlite_db, agent_graph(graph_id="graph-ret-policy"))
    app = await bootstrap_app(
        sqlite_db, deployment_ref=service.deployment.deployment_ref, auth_config=service.auth_config
    )
    app.state.bootstrap = service

    with TestClient(app) as client:
        put = client.put(
            "/v1/retention/policy",
            json={"audit_ttl_seconds": 2592000, "run_ttl_seconds": None, "enabled": True},
            headers=admin_headers(),
        )
        assert put.status_code == 200, put.text
        assert put.json()["audit_ttl_seconds"] == 2592000

        got = client.get("/v1/retention/policy", headers=admin_headers())
        assert got.status_code == 200
        assert got.json()["audit_ttl_seconds"] == 2592000

        # Operator lacks RETENTION_ADMIN.
        denied = client.get("/v1/retention/policy", headers=operator_headers())
        assert denied.status_code == 403


async def test_right_to_erasure_via_api_strips_pii_and_chain_still_verifies(sqlite_db) -> None:
    service, deployment = await deploy_service(sqlite_db, agent_graph(graph_id="graph-ret-rte"))
    await _seed_run(service, deployment, "run-rte", ssn="777-00-7777")
    app = await bootstrap_app(
        sqlite_db, deployment_ref=deployment.deployment_ref, auth_config=service.auth_config
    )
    app.state.bootstrap = service

    with TestClient(app) as client:
        erase = client.post(
            "/v1/retention/erasure-requests",
            json={"run_id": "run-rte"},
            headers=admin_headers(),
        )
        assert erase.status_code == 200, erase.text
        body = erase.json()
        assert body["reason"] == "rte"
        assert body["runs"][0]["audits_erased"] == 2

        # Evidence bundle still returns 200, PII stripped, continuity intact.
        evidence = client.get("/runs/run-rte/evidence", headers=admin_headers())
        assert evidence.status_code == 200, evidence.text
        audits = evidence.json()["audits"]
        assert audits, "tombstones must still be listed"
        for record in audits:
            assert record["erased"] is True
            assert record["input_snapshot"] == {}
            assert record["output_snapshot"] == {}
            assert record["stdout"] is None

        verify = client.get("/runs/run-rte/audit-verification", headers=admin_headers())
        assert verify.status_code == 200
        assert verify.json()["verified"] is True
        assert verify.json()["signature_verified"] is not False


async def test_legal_hold_makes_erasure_conflict_then_release_allows_it(sqlite_db) -> None:
    service, deployment = await deploy_service(sqlite_db, agent_graph(graph_id="graph-ret-hold"))
    await _seed_run(service, deployment, "run-hold", ssn="888-00-8888")
    app = await bootstrap_app(
        sqlite_db, deployment_ref=deployment.deployment_ref, auth_config=service.auth_config
    )
    app.state.bootstrap = service

    with TestClient(app) as client:
        placed = client.post(
            "/v1/retention/legal-holds",
            json={"run_id": "run-hold", "reason": "litigation"},
            headers=admin_headers(),
        )
        assert placed.status_code == 201, placed.text
        hold_id = placed.json()["hold_id"]

        conflict = client.post(
            "/v1/retention/erasure-requests",
            json={"run_id": "run-hold"},
            headers=admin_headers(),
        )
        assert conflict.status_code == 409, conflict.text

        released = client.delete(
            f"/v1/retention/legal-holds/{hold_id}", headers=admin_headers()
        )
        assert released.status_code == 200
        assert released.json()["active"] is False

        ok = client.post(
            "/v1/retention/erasure-requests",
            json={"run_id": "run-hold"},
            headers=admin_headers(),
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["runs"][0]["audits_erased"] == 2


async def test_cross_tenant_erasure_denied(sqlite_db) -> None:
    """A tenant-b admin cannot erase a default-tenant run (resource-scope)."""
    auth = scoped_auth_config(
        ("admin-default", "key-default", ServiceRole.ADMIN, "default", None),
        ("admin-b", "key-tenant-b", ServiceRole.ADMIN, "tenant-b", None),
    )
    service, deployment = await deploy_service(
        sqlite_db, agent_graph(graph_id="graph-ret-xtenant"), auth_config=auth
    )
    await _seed_run(service, deployment, "run-default", ssn="123-00-4567")
    app = await bootstrap_app(
        sqlite_db, deployment_ref=deployment.deployment_ref, auth_config=auth
    )
    app.state.bootstrap = service

    with TestClient(app) as client:
        denied = client.post(
            "/v1/retention/erasure-requests",
            json={"run_id": "run-default"},
            headers=api_key_headers("key-tenant-b"),
        )
        assert denied.status_code == 404, denied.text

    # PII untouched by the denied cross-tenant request.
    async with sqlite_db.transaction() as connection:
        rows = await connection.fetch_all("SELECT record_json FROM node_audits", ())
    assert any("123-00-4567" in (r["record_json"] or "") for r in rows)


async def test_release_cross_tenant_hold_denied(sqlite_db) -> None:
    auth = scoped_auth_config(
        ("admin-default", "key-default", ServiceRole.ADMIN, "default", None),
        ("admin-b", "key-tenant-b", ServiceRole.ADMIN, "tenant-b", None),
    )
    service, deployment = await deploy_service(
        sqlite_db, agent_graph(graph_id="graph-ret-holdscope"), auth_config=auth
    )
    app = await bootstrap_app(
        sqlite_db, deployment_ref=deployment.deployment_ref, auth_config=auth
    )
    app.state.bootstrap = service

    with TestClient(app) as client:
        placed = client.post(
            "/v1/retention/legal-holds",
            json={"reason": "tenant-wide"},
            headers=api_key_headers("key-default"),
        )
        assert placed.status_code == 201
        hold_id = placed.json()["hold_id"]

        # tenant-b admin cannot release tenant-default's hold.
        denied = client.delete(
            f"/v1/retention/legal-holds/{hold_id}",
            headers=api_key_headers("key-tenant-b"),
        )
        assert denied.status_code == 404
