from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from tests.service.helpers import (
    admin_headers,
    agent_graph,
    api_key_headers,
    deploy_service,
    operator_headers,
    scoped_auth_config,
)
from zeroth.governance.identity import ServiceRole
from zeroth.platform.signing import EnvHmacSigner
from zeroth.service.bootstrap.factory import bootstrap_scoped_app as bootstrap_app
from zeroth.service.certifications.receipt import (
    PromotionReceiptPayload,
    sign_promotion_receipt,
)
from zeroth.service.certifications.repository import CertificationRepository
from zeroth.service.certifications.service import CertificationService
from zeroth.service.api.health import DependencyStatus

COMMIT = "1" * 40
IMAGE = "sha256:" + "2" * 64


def _receipt(
    signer,
    certification_id: str,
    environments=("test", "production"),
    *,
    tenant_id="default",
    workspace_id=None,
):
    now = datetime.now(UTC)
    return sign_promotion_receipt(
        PromotionReceiptPayload(
            certification_id=certification_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            app_name="support-agent",
            app_commit=COMMIT,
            zeroth_version="0.23.10",
            image_reference="registry.example/support-agent",
            image_digest=IMAGE,
            source_digest="sha256:" + "3" * 64,
            evidence_digest="sha256:" + "4" * 64,
            report_digest="sha256:" + "5" * 64,
            environments=environments,
            issued_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(hours=1),
        ),
        signer,
    )


async def _app(sqlite_db):
    service, deployment = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-certification-api"),
        deployment_ref="production/support-agent",
    )
    app = await bootstrap_app(sqlite_db, deployment_ref=deployment.deployment_ref)
    app.state.bootstrap = service
    signer = EnvHmacSigner(key_id="certifier-1", keys={"certifier-1": b"secret"})
    app.state.bootstrap.certification_service = CertificationService(
        CertificationRepository(sqlite_db),
        verifier=signer,
        metrics=service.metrics_collector,
    )
    return app, signer


async def test_signed_exact_receipt_promotes_and_exposes_audit_and_metrics(
    sqlite_db, monkeypatch
) -> None:
    async def redis_disabled(*args, **kwargs):
        return DependencyStatus(status="unavailable")

    async def regulus_ok(*args, **kwargs):
        return DependencyStatus(status="ok")

    monkeypatch.setattr("zeroth.service.api.health.check_redis", redis_disabled)
    monkeypatch.setattr("zeroth.service.api.health.check_regulus", regulus_ok)
    app, signer = await _app(sqlite_db)
    receipt = _receipt(signer, "a" * 32)

    with TestClient(app) as client:
        created = client.post(
            "/v1/certifications",
            json={"receipt": receipt.model_dump(mode="json")},
            headers=operator_headers(),
        )
        promoted = client.post(
            f"/v1/certifications/{receipt.payload.certification_id}/promote",
            json={
                "target_key": "production/support-agent",
                "app_commit": COMMIT,
                "image_digest": IMAGE,
            },
            headers=operator_headers(),
        )
        fetched = client.get(
            f"/v1/certifications/{receipt.payload.certification_id}",
            headers=operator_headers(),
        )
        health = client.get("/health/ready")
        metrics = client.get("/v1/metrics", headers=admin_headers())

    assert created.status_code == 201
    assert created.json()["state"] == "certified"
    assert promoted.status_code == 200
    assert promoted.json()["state"] == "promoted"
    assert promoted.json()["evaluation"]["production_ready"] is True
    assert [event["event_type"] for event in fetched.json()["events"]] == [
        "registered",
        "promoted",
    ]
    assert health.status_code == 200
    assert health.json()["status"] == "ok", health.json()
    assert health.json()["production_ready"] is True
    assert "zeroth_certification_operations_total" in metrics.text
    assert "zeroth_production_ready" in metrics.text


async def test_test_deployment_path_remains_available_but_production_is_blocked(
    sqlite_db, monkeypatch
) -> None:
    async def redis_disabled(*args, **kwargs):
        return DependencyStatus(status="unavailable")

    async def regulus_ok(*args, **kwargs):
        return DependencyStatus(status="ok")

    monkeypatch.setattr("zeroth.service.api.health.check_redis", redis_disabled)
    monkeypatch.setattr("zeroth.service.api.health.check_regulus", regulus_ok)
    app, signer = await _app(sqlite_db)
    receipt = _receipt(signer, "b" * 32, environments=("test",))

    with TestClient(app) as client:
        created = client.post(
            "/v1/certifications",
            json={"receipt": receipt.model_dump(mode="json")},
            headers=operator_headers(),
        )
        deployments = client.get("/v1/deployments", headers=operator_headers())
        promotion = client.post(
            f"/v1/certifications/{receipt.payload.certification_id}/promote",
            json={
                "target_key": "production/support-agent",
                "app_commit": COMMIT,
                "image_digest": IMAGE,
            },
            headers=operator_headers(),
        )
        readiness = client.get("/health/ready")

    assert created.status_code == 201
    assert created.json()["state"] == "test_deployable"
    assert created.json()["evaluation"]["test_deployable"] is True
    assert created.json()["evaluation"]["production_ready"] is False
    assert deployments.status_code == 200
    assert promotion.status_code == 409
    assert readiness.json()["status"] == "ok", readiness.json()
    assert readiness.json()["production_ready"] is False
    assert readiness.json()["certification"]["blockers"][0]["remediation"]


async def test_override_requires_admin_and_is_visible_and_auditable(sqlite_db) -> None:
    app, signer = await _app(sqlite_db)
    receipt = _receipt(signer, "c" * 32, environments=("test",))
    expiry = datetime.now(UTC) + timedelta(minutes=10)
    payload = {
        "scopes": ["environment_policy"],
        "reason": "approved recovery window",
        "expires_at": expiry.isoformat(),
    }

    with TestClient(app) as client:
        client.post(
            "/v1/certifications",
            json={"receipt": receipt.model_dump(mode="json")},
            headers=operator_headers(),
        )
        denied = client.post(
            f"/v1/certifications/{receipt.payload.certification_id}/override",
            json=payload,
            headers=operator_headers(),
        )
        granted = client.post(
            f"/v1/certifications/{receipt.payload.certification_id}/override",
            json=payload,
            headers=admin_headers(),
        )

    assert denied.status_code == 403
    assert granted.status_code == 200
    body = granted.json()
    assert body["override"]["reason"] == "approved recovery window"
    assert body["evaluation"]["override_active"] is True
    assert body["evaluation"]["production_ready"] is True
    assert body["events"][-1]["event_type"] == "override_granted"
    assert body["events"][-1]["actor_id"] == "admin-1"


async def test_tampered_receipt_fails_closed_and_foreign_scope_is_hidden(sqlite_db) -> None:
    app, signer = await _app(sqlite_db)
    receipt = _receipt(signer, "d" * 32)
    tampered = receipt.model_copy(
        update={
            "payload": receipt.payload.model_copy(
                update={"image_digest": "sha256:" + "9" * 64}
            )
        }
    )

    with TestClient(app) as client:
        rejected = client.post(
            "/v1/certifications",
            json={"receipt": tampered.model_dump(mode="json")},
            headers=operator_headers(),
        )
        hidden = client.get(
            f"/v1/certifications/{receipt.payload.certification_id}",
            headers=operator_headers(),
        )

    assert rejected.status_code == 409
    assert hidden.status_code == 404


async def test_foreign_tenant_certification_is_hidden_as_not_found(sqlite_db) -> None:
    auth_config = scoped_auth_config(
        ("tenant-a", "tenant-a-key", ServiceRole.OPERATOR, "tenant-a", "workspace-a"),
        ("tenant-b", "tenant-b-key", ServiceRole.REVIEWER, "tenant-b", "workspace-b"),
    )
    service, deployment = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-certification-scope"),
        deployment_ref="production/scoped-agent",
        auth_config=auth_config,
        tenant_id="tenant-a",
        workspace_id="workspace-a",
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=deployment.deployment_ref,
        tenant_id="tenant-a",
        workspace_id="workspace-a",
        auth_config=auth_config,
    )
    app.state.bootstrap = service
    signer = EnvHmacSigner(key_id="certifier-1", keys={"certifier-1": b"secret"})
    app.state.bootstrap.certification_service = CertificationService(
        CertificationRepository(sqlite_db), verifier=signer
    )
    receipt = _receipt(
        signer,
        "e" * 32,
        tenant_id="tenant-a",
        workspace_id="workspace-a",
    )

    with TestClient(app) as client:
        created = client.post(
            "/v1/certifications",
            json={"receipt": receipt.model_dump(mode="json")},
            headers=api_key_headers("tenant-a-key"),
        )
        hidden = client.get(
            f"/v1/certifications/{receipt.payload.certification_id}",
            headers=api_key_headers("tenant-b-key"),
        )

    assert created.status_code == 201
    assert hidden.status_code == 404
