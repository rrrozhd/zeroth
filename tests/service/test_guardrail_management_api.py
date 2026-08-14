"""Operator API regressions for ingress guardrail policy."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.service.helpers import (
    admin_headers,
    agent_graph,
    api_key_headers,
    deploy_service,
    operator_headers,
    reviewer_headers,
    scoped_auth_config,
)
from zeroth.governance.identity import ServiceRole
from zeroth.service.bootstrap import bootstrap_app


async def _client(sqlite_db, *, auth_config=None):
    service, _ = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-guardrail-management"),
        deployment_ref="guardrail-management",
        auth_config=auth_config,
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        auth_config=auth_config,
    )
    app.state.bootstrap = service
    return service, app


async def test_effective_settings_and_immutable_audit_history(sqlite_db) -> None:
    service, app = await _client(sqlite_db)
    ref = service.deployment.deployment_ref

    with TestClient(app) as client:
        tenant = client.put(
            "/v1/guardrails",
            headers=operator_headers(),
            json={"rate_limit_capacity": 20, "max_concurrency": 5},
        )
        deployment = client.put(
            f"/v1/deployments/{ref}/guardrails",
            headers=admin_headers(),
            json={"rate_limit_capacity": 7, "backpressure_queue_depth": 12},
        )
        current = client.get(
            f"/v1/deployments/{ref}/guardrails",
            headers=operator_headers(),
        )
        history = client.get("/v1/guardrails/history", headers=admin_headers())

    assert tenant.status_code == deployment.status_code == current.status_code == 200
    assert current.json()["effective"] == {
        "rate_limit_capacity": 7.0,
        "rate_limit_refill_rate": 1.0,
        "rate_limit_burst": 0.0,
        "quota_daily_limit": None,
        "backpressure_queue_depth": 12,
        "max_concurrency": 5,
    }
    assert [row["scope"] for row in history.json()] == ["tenant", "deployment"]
    assert [row["changed_by"] for row in history.json()] == ["operator-1", "admin-1"]
    snapshot = service.metrics_collector.snapshot()["counters"]
    assert snapshot['zeroth_guardrail_policy_changes_total{scope="tenant"}'] == 1
    assert snapshot['zeroth_guardrail_policy_changes_total{scope="deployment"}'] == 1


async def test_guardrail_management_rbac_and_invalid_changes_fail_closed(sqlite_db) -> None:
    service, app = await _client(sqlite_db)

    with TestClient(app) as client:
        forbidden = client.put(
            "/v1/guardrails",
            headers=reviewer_headers(),
            json={"max_concurrency": 2},
        )
        invalid = client.put(
            "/v1/guardrails",
            headers=admin_headers(),
            json={"max_concurrency": 0},
        )
        invalid_null = client.put(
            "/v1/guardrails",
            headers=admin_headers(),
            json={"max_concurrency": None},
        )
        empty = client.put("/v1/guardrails", headers=admin_headers(), json={})
        history = client.get("/v1/guardrails/history", headers=admin_headers())

    assert forbidden.status_code == 403
    assert invalid.status_code == 422
    assert invalid_null.status_code == 422
    assert empty.status_code == 422
    assert history.json() == []
    assert await service.guardrail_policy_repository.history() == []


async def test_guardrail_management_hides_cross_tenant_service(sqlite_db) -> None:
    auth = scoped_auth_config(
        ("owner", "owner-key", ServiceRole.ADMIN, "default", None),
        ("foreign", "foreign-key", ServiceRole.ADMIN, "tenant-foreign", None),
    )
    service, app = await _client(sqlite_db, auth_config=auth)

    with TestClient(app) as client:
        hidden = client.get(
            f"/v1/deployments/{service.deployment.deployment_ref}/guardrails",
            headers=api_key_headers("foreign-key"),
        )
        rejected = client.put(
            "/v1/guardrails",
            headers=api_key_headers("foreign-key"),
            json={"max_concurrency": 1},
        )

    assert hidden.status_code == 404
    assert rejected.status_code == 404
    assert await service.guardrail_policy_repository.history() == []


async def test_rate_rejection_metrics_audit_and_effective_utilization(sqlite_db) -> None:
    service, app = await _client(sqlite_db)
    ref = service.deployment.deployment_ref
    service.worker = None

    with TestClient(app) as client:
        updated = client.put(
            f"/v1/deployments/{ref}/guardrails",
            headers=admin_headers(),
            json={
                "rate_limit_capacity": 1,
                "rate_limit_refill_rate": 0.001,
                "backpressure_queue_depth": 100,
            },
        )
        accepted = client.post(
            "/runs",
            headers=operator_headers(),
            json={"input_payload": {"value": 1}},
        )
        rejected = client.post(
            "/runs",
            headers=operator_headers(),
            json={"input_payload": {"value": 2}},
        )
        rendered = client.get("/v1/metrics", headers=admin_headers())

    assert updated.status_code == 200
    assert accepted.status_code == 202
    assert rejected.status_code == 429
    assert updated.json()["effective"]["rate_limit_capacity"] == 1
    assert 'zeroth_guardrail_rejections_total{reason="rate"} 1' in rendered.text
    assert 'zeroth_guardrail_utilization_ratio{resource="queue"}' in rendered.text
    assert 'zeroth_guardrail_utilization_ratio{resource="rate"}' in rendered.text
    audits = await service.audit_repository.list_by_node("service.guardrail.rate")
    assert len(audits) == 1
    assert audits[0].status == "rejected"
    assert audits[0].node_id == "service.guardrail.rate"
    assert audits[0].audit_id.endswith(":guardrail:rate")
    assert audits[0].execution_metadata["admitted"] is False
    assert audits[0].execution_metadata["decision"] == "deny"
    assert audits[0].execution_metadata["enforcement_applied"] is True
    assert audits[0].tenant_id == "default"


async def test_queue_rejection_metrics_are_distinct(sqlite_db) -> None:
    service, app = await _client(sqlite_db)
    ref = service.deployment.deployment_ref
    service.worker = None

    with TestClient(app) as client:
        client.put(
            f"/v1/deployments/{ref}/guardrails",
            headers=admin_headers(),
            json={"rate_limit_capacity": 100, "backpressure_queue_depth": 1},
        )
        first = client.post(
            "/runs", headers=operator_headers(), json={"input_payload": {"value": 1}}
        )
        rejected = client.post(
            "/runs", headers=operator_headers(), json={"input_payload": {"value": 2}}
        )

    assert first.status_code == 202
    assert rejected.status_code == 503
    assert (
        service.metrics_collector.snapshot()["counters"][
            'zeroth_guardrail_rejections_total{reason="queue"}'
        ]
        == 1
    )


async def test_quota_rejection_metrics_are_distinct(sqlite_db) -> None:
    service, app = await _client(sqlite_db)
    ref = service.deployment.deployment_ref
    service.worker = None

    with TestClient(app) as client:
        client.put(
            f"/v1/deployments/{ref}/guardrails",
            headers=admin_headers(),
            json={"rate_limit_capacity": 100, "quota_daily_limit": 1},
        )
        first = client.post(
            "/runs", headers=operator_headers(), json={"input_payload": {"value": 1}}
        )
        rejected = client.post(
            "/runs", headers=operator_headers(), json={"input_payload": {"value": 2}}
        )

    assert first.status_code == 202
    assert rejected.status_code == 503
    assert (
        service.metrics_collector.snapshot()["counters"][
            'zeroth_guardrail_rejections_total{reason="quota"}'
        ]
        == 1
    )


async def test_concurrency_rejection_metrics_are_distinct(sqlite_db) -> None:
    service, app = await _client(sqlite_db)
    worker = service.worker
    assert worker is not None
    service.worker = None
    ref = service.deployment.deployment_ref

    with TestClient(app) as client:
        client.put(
            f"/v1/deployments/{ref}/guardrails",
            headers=admin_headers(),
            json={"rate_limit_capacity": 100, "max_concurrency": 1},
        )
        accepted = client.post(
            "/runs", headers=operator_headers(), json={"input_payload": {"value": 1}}
        )

    assert accepted.status_code == 202
    claimed = await service.lease_manager.claim_pending(
        ref,
        "other-replica",
        tenant_id="default",
        workspace_id=None,
        max_concurrency=1,
    )
    assert claimed == accepted.json()["run_id"]
    assert await worker._claim_pending() is None
    assert (
        service.metrics_collector.snapshot()["counters"][
            'zeroth_guardrail_rejections_total{reason="concurrency"}'
        ]
        == 1
    )
