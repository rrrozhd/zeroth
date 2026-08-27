from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import pytest

from tests.service.helpers import (
    agent_graph,
    deploy_service,
    operator_headers,
    reviewer_headers,
)
from zeroth.platform.dispatch.operations import OperationState
from zeroth.platform.signing import EnvHmacSigner
from zeroth.service.bootstrap import bootstrap_app


async def _ambiguous_service(sqlite_db, *, suffix: str, signed: bool = True):
    service, deployment = await deploy_service(
        sqlite_db, agent_graph(graph_id=f"graph-operation-resolution-{suffix}")
    )
    signer = EnvHmacSigner(key_id="operation-resolution", keys={"operation-resolution": b"key"})
    service.signer = signer if signed else None
    service.audit_repository._signer = signer if signed else None
    operation_key = f"operation-{suffix}"
    store = service.orchestrator.operation_store
    assert store is not None
    await store.claim(
        operation_key,
        run_id=f"run-{suffix}",
        dispatch_id=f"dispatch-{suffix}",
        idempotency_key=f"idem-{suffix}",
        target_ref="unit://charge-card",
    )
    await store.mark_ambiguous(operation_key, reason="worker vanished")
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=deployment.deployment_ref,
        auth_config=service.auth_config,
    )
    app.state.bootstrap = service
    return service, deployment, operation_key, app


@pytest.mark.parametrize(
    ("resolution", "receipt", "expected"),
    [
        ("completed", {"charge_id": "ch_123"}, OperationState.COMPLETED),
        ("failed", None, OperationState.FAILED),
    ],
)
async def test_operator_resolves_ambiguous_operation_with_signed_audit(
    sqlite_db, resolution, receipt, expected
) -> None:
    service, deployment, operation_key, app = await _ambiguous_service(
        sqlite_db, suffix=resolution
    )
    payload = {"resolution": resolution, "reason": "verified in provider console"}
    if receipt is not None:
        payload["receipt"] = receipt

    with TestClient(app) as client:
        response = client.post(
            f"/v1/deployments/{deployment.deployment_ref}/operations/{operation_key}/resolve",
            json=payload,
            headers=operator_headers(),
        )

    assert response.status_code == 200
    assert response.json()["state"] == expected.value
    stored = await service.orchestrator.operation_store.get(operation_key)
    assert stored["state"] == expected
    assert stored["ambiguity_reason"] == "verified in provider console"
    audits = await service.audit_repository.list_by_run(f"run-{resolution}")
    resolution_audit = next(record for record in audits if record.node_id == "operation.resolve")
    assert resolution_audit.actor.subject == "operator-1"
    assert resolution_audit.record_signature is not None
    assert resolution_audit.cost_usd == 0.0
    assert resolution_audit.estimated_cost_usd == 0.0
    assert resolution_audit.cost_measurement.value == "measured"
    assert resolution_audit.execution_metadata["resolution_reason_sha256"] == hashlib.sha256(
        b"verified in provider console"
    ).hexdigest()
    assert resolution_audit.execution_metadata["operation_key"] == operation_key


async def test_reviewer_cannot_resolve_ambiguous_operation(sqlite_db) -> None:
    service, deployment, operation_key, app = await _ambiguous_service(
        sqlite_db, suffix="reviewer-denied"
    )

    with TestClient(app) as client:
        response = client.post(
            f"/v1/deployments/{deployment.deployment_ref}/operations/{operation_key}/resolve",
            json={"resolution": "completed", "reason": "trust me"},
            headers=reviewer_headers(),
        )

    assert response.status_code == 403
    stored = await service.orchestrator.operation_store.get(operation_key)
    assert stored["state"] == OperationState.AMBIGUOUS


async def test_unsigned_service_refuses_resolution_without_mutating(sqlite_db) -> None:
    service, deployment, operation_key, app = await _ambiguous_service(
        sqlite_db, suffix="unsigned", signed=False
    )

    with TestClient(app) as client:
        response = client.post(
            f"/v1/deployments/{deployment.deployment_ref}/operations/{operation_key}/resolve",
            json={"resolution": "completed", "reason": "verified"},
            headers=operator_headers(),
        )

    assert response.status_code == 503
    stored = await service.orchestrator.operation_store.get(operation_key)
    assert stored["state"] == OperationState.AMBIGUOUS


async def test_audit_failure_leaves_operation_ambiguous(sqlite_db) -> None:
    service, deployment, operation_key, app = await _ambiguous_service(
        sqlite_db, suffix="audit-failure"
    )
    service.audit_repository.write = AsyncMock(side_effect=RuntimeError("audit unavailable"))

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            f"/v1/deployments/{deployment.deployment_ref}/operations/{operation_key}/resolve",
            json={"resolution": "completed", "reason": "verified"},
            headers=operator_headers(),
        )

    assert response.status_code == 500
    stored = await service.orchestrator.operation_store.get(operation_key)
    assert stored["state"] == OperationState.AMBIGUOUS
