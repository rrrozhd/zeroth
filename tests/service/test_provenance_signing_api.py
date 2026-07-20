"""WS-D provenance endpoints: signed verification, dual-check, cross-tenant, fail-closed."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from tests.service.helpers import (
    RunInputPayload,
    admin_headers,
    agent_graph,
    build_run_for_service,
    default_service_auth_config,
    deploy_service,
)
from zeroth.governance.audit import NodeAuditRecord
from zeroth.contracts.registry import ContractRegistry
from zeroth.service.deployments import DeploymentService, SQLiteDeploymentRepository
from zeroth.contracts.graph import GraphRepository
from zeroth.governance.identity import ServiceRole
from zeroth.core.service.auth import ServiceAuthConfig, StaticApiKeyCredential
from zeroth.core.service.bootstrap import bootstrap_app, bootstrap_service
from zeroth.platform.signing import EnvHmacSigner, SigningKeyProvider

_KEY = EnvHmacSigner(key_id="k1", keys={"k1": b"provenance-endpoint-key"})


async def _signed_setup(
    sqlite_db,
    *,
    signer: SigningKeyProvider | None = _KEY,
    tenant_id: str = "default",
    auth_config: ServiceAuthConfig | None = None,
    deployment_ref: str = "prov-svc",
):
    """Deploy WITH a signer and thread it into the bootstrap surface.

    ``deploy_service`` builds a signer-less service; here we inject one so the
    persisted deployment is signed and the verify endpoints hold the key.
    """
    graph = agent_graph(graph_id="graph-prov")
    graph_repository = GraphRepository(sqlite_db)
    contract_registry = ContractRegistry(sqlite_db)
    await contract_registry.register(RunInputPayload, name="contract://input")
    await contract_registry.register(RunInputPayload, name="contract://output")
    graph = graph.model_copy(update={"tenant_id": tenant_id})
    graph = await graph_repository.create(graph, tenant_id=tenant_id, workspace_id=None)
    await graph_repository.publish(
        graph.graph_id,
        graph.version,
        tenant_id=tenant_id,
        workspace_id=None,
    )

    deployment_service = DeploymentService(
        graph_repository=graph_repository,
        deployment_repository=SQLiteDeploymentRepository(sqlite_db),
        contract_registry=contract_registry,
        signer=signer,
    )
    deployment = await deployment_service.deploy(
        deployment_ref,
        graph.graph_id,
        graph.version,
        tenant_id=tenant_id,
        workspace_id=None,
    )

    resolved_auth = auth_config or default_service_auth_config()
    service = await bootstrap_service(
        sqlite_db, deployment_ref=deployment.deployment_ref, auth_config=resolved_auth
    )
    # Inject the signer into the already-built surface (the settings-built signer
    # is None in tests because no key is configured in the process env).
    service.signer = signer
    service.audit_repository._signer = signer
    service.deployment_service.signer = signer

    app = await bootstrap_app(
        sqlite_db, deployment_ref=deployment.deployment_ref, auth_config=resolved_auth
    )
    app.state.bootstrap = service
    return service, deployment, app


async def _signed_run(service, *, tenant_id: str = "default", node_ids=("start", "finish")):
    run = build_run_for_service(service)
    await service.run_repository.create(run)
    for index, node_id in enumerate(node_ids):
        await service.audit_repository.write(
            NodeAuditRecord(
                audit_id=f"{run.run_id}:{index}",
                run_id=run.run_id,
                node_id=node_id,
                graph_version_ref=service.deployment.graph_version_ref,
                deployment_ref=service.deployment.deployment_ref,
                tenant_id=tenant_id,
                status="completed",
                started_at=datetime(2026, 7, 11, tzinfo=UTC),
                completed_at=datetime(2026, 7, 11, 0, 0, 1, tzinfo=UTC),
            )
        )
    return run


async def test_bootstrap_wires_signer_from_configured_key(sqlite_db, monkeypatch) -> None:
    """Exercise the REAL bootstrap wiring (not hand-injection): a configured key
    must flow into all three signer holders.

    Every other test injects the signer directly, so this is the only assertion
    that the deployed system actually signs. The shared secret provider resolves
    the logical name 'signing.deployment' -> env 'SIGNING_DEPLOYMENT'.
    """
    monkeypatch.setenv("SIGNING_DEPLOYMENT", "bootstrap-hmac-key")
    service, _ = await deploy_service(sqlite_db, agent_graph(graph_id="graph-wire"))

    assert isinstance(service.signer, EnvHmacSigner)
    # The SAME instance must reach the audit repository and deployment service.
    assert service.audit_repository._signer is service.signer
    assert service.deployment_service.signer is service.signer

    # And a deploy through the wired service actually signs (API deploy path).
    signed = await service.deployment_service.deploy(
        "graph-wire-svc-2", service.deployment.graph_id, service.deployment.graph_version
    )
    assert signed.attestation_signature
    assert signed.attestation_signing_key_id == service.signer.key_id()


async def test_run_audit_verification_reports_signed(sqlite_db) -> None:
    service, _, app = await _signed_setup(sqlite_db)
    run = await _signed_run(service)

    with TestClient(app) as client:
        response = client.get(f"/runs/{run.run_id}/audit-verification", headers=admin_headers())

    assert response.status_code == 200
    body = response.json()
    assert body["verified"] is True
    assert body["signature_verified"] is True
    assert body["signing_key_id"] == "k1"
    assert body["unsigned_record_count"] == 0
    assert body["record_count"] == 2


async def test_post_verify_chain_with_matching_head(sqlite_db) -> None:
    service, _, app = await _signed_setup(sqlite_db)
    run = await _signed_run(service)
    records = await service.audit_repository.list_by_run(run.run_id)
    head = records[-1].record_digest

    with TestClient(app) as client:
        good = client.post(
            f"/runs/{run.run_id}/verify-chain",
            json={"expected_head_digest": head},
            headers=admin_headers(),
        )
        bad = client.post(
            f"/runs/{run.run_id}/verify-chain",
            json={"expected_head_digest": "deadbeef"},
            headers=admin_headers(),
        )

    assert good.status_code == 200
    assert good.json()["verified"] is True
    assert good.json()["signature_verified"] is True
    assert bad.status_code == 200
    assert bad.json()["verified"] is False
    assert bad.json()["error"] == "expected head digest mismatch"


async def test_attestation_endpoints_dual_check_signed(sqlite_db) -> None:
    service, deployment, app = await _signed_setup(sqlite_db)

    with TestClient(app) as client:
        attestation = client.get(
            f"/deployments/{deployment.deployment_ref}/attestation",
            headers=admin_headers(),
        )
        self_verify = client.get(
            f"/deployments/{deployment.deployment_ref}/attestation/verify",
            headers=admin_headers(),
        )
        posted = client.post(
            f"/deployments/{deployment.deployment_ref}/verify-attestation",
            json=attestation.json(),
            headers=admin_headers(),
        )

    assert attestation.status_code == 200
    # The attestation returns the PERSISTED signature, not a re-signed payload.
    assert attestation.json()["attestation_signature"]
    assert attestation.json()["attestation_signing_key_id"] == "k1"

    assert self_verify.status_code == 200
    self_body = self_verify.json()
    assert self_body["verified"] is True
    assert self_body["digest_verified"] is True
    assert self_body["signature_verified"] is True
    assert self_body["signing_key_id"] == "k1"

    assert posted.status_code == 200
    assert posted.json()["verified"] is True
    assert posted.json()["signature_verified"] is True


async def test_missing_signer_fails_closed_on_signed_records(sqlite_db) -> None:
    """A signed chain verified by a service that LOST its key -> False, not True.

    Fail-closed: signed rows can never read as verified without the key, and are
    never silently downgraded to the neutral unsigned-legacy state.
    """
    service, _, app = await _signed_setup(sqlite_db)
    run = await _signed_run(service)
    # Verifier loses the key after the records were signed.
    service.signer = None

    with TestClient(app) as client:
        response = client.get(f"/runs/{run.run_id}/audit-verification", headers=admin_headers())

    assert response.status_code == 200
    body = response.json()
    assert body["verified"] is True  # digest chain intact
    assert body["signature_verified"] is False  # signed rows + no key -> fail closed
    assert body["signing_key_id"] is None


def _cross_tenant_auth() -> ServiceAuthConfig:
    return ServiceAuthConfig(
        api_keys=[
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


async def test_cross_tenant_verify_endpoints_return_404(sqlite_db) -> None:
    auth_config = _cross_tenant_auth()
    service, deployment, app = await _signed_setup(
        sqlite_db, tenant_id="tenant-a", auth_config=auth_config
    )
    run = await _signed_run(service, tenant_id="tenant-a")

    with TestClient(app) as client:
        a_headers = {"X-API-Key": "tenant-a-reviewer-key"}
        b_headers = {"X-API-Key": "tenant-b-reviewer-key"}

        # Tenant A sees its own run verification.
        assert (
            client.get(f"/runs/{run.run_id}/audit-verification", headers=a_headers).status_code
            == 200
        )
        # Tenant B is denied the cross-tenant run (404, not a signature verdict).
        run_denied = client.get(f"/runs/{run.run_id}/audit-verification", headers=b_headers)
        post_denied = client.post(f"/runs/{run.run_id}/verify-chain", json={}, headers=b_headers)
        att_denied = client.get(
            f"/deployments/{deployment.deployment_ref}/attestation/verify",
            headers=b_headers,
        )

    # The cross-tenant guarantee is a 404 (existence hidden), whichever scope
    # guard fires first — the deployment-scope check on these bound endpoints
    # surfaces "deployment not found".
    assert run_denied.status_code == 404
    assert run_denied.json()["detail"] in {"deployment not found", "run not found"}
    assert post_denied.status_code == 404
    assert att_denied.status_code == 404
