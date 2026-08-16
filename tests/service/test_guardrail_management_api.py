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
from zeroth.governance.guardrails.config import GuardrailConfig
from zeroth.governance.audit import AuditRepository
from zeroth.governance.identity import ServiceRole
from zeroth.platform.storage import ScopeContext
from zeroth.service.app import create_app
from zeroth.service.bootstrap.factory import bootstrap_scoped_service


async def _client(
    sqlite_db,
    *,
    auth_config=None,
    tenant_id: str = "default",
    workspace_id: str | None = None,
    guardrail_config: GuardrailConfig | None = None,
):
    service, _ = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-guardrail-management"),
        deployment_ref="guardrail-management",
        auth_config=auth_config,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
    )
    if guardrail_config is not None:
        service = await bootstrap_scoped_service(
            sqlite_db,
            deployment_ref=service.deployment.deployment_ref,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            auth_config=service.auth_config,
            guardrail_config=guardrail_config,
        )
    return service, create_app(service)


async def test_effective_settings_and_immutable_audit_history(sqlite_db) -> None:
    service, app = await _client(sqlite_db)
    ref = service.deployment.deployment_ref

    with TestClient(app) as client:
        tenant = client.put(
            "/v1/guardrails",
            headers=admin_headers(),
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
    assert [row["changed_by"] for row in history.json()] == ["admin-1", "admin-1"]
    snapshot = service.metrics_collector.snapshot()["counters"]
    assert snapshot['zeroth_guardrail_policy_changes_total{scope="tenant"}'] == 1
    assert snapshot['zeroth_guardrail_policy_changes_total{scope="deployment"}'] == 1


async def test_tenant_guardrails_require_unscoped_tenant_authority(sqlite_db) -> None:
    auth = scoped_auth_config(
        ("tenant-admin", "tenant-admin-key", ServiceRole.ADMIN, "tenant-shared", None),
        (
            "workspace-admin",
            "workspace-admin-key",
            ServiceRole.ADMIN,
            "tenant-shared",
            "workspace-a",
        ),
        (
            "other-workspace-admin",
            "other-workspace-key",
            ServiceRole.ADMIN,
            "tenant-shared",
            "workspace-b",
        ),
    )
    service, app = await _client(
        sqlite_db,
        auth_config=auth,
        tenant_id="tenant-shared",
        workspace_id="workspace-a",
    )

    with TestClient(app) as client:
        workspace_read = client.get(
            "/v1/guardrails", headers=api_key_headers("workspace-admin-key")
        )
        workspace_write = client.put(
            "/v1/guardrails",
            headers=api_key_headers("workspace-admin-key"),
            json={"max_concurrency": 2},
        )
        workspace_history = client.get(
            "/v1/guardrails/history",
            headers=api_key_headers("workspace-admin-key"),
        )
        cross_workspace = client.put(
            f"/v1/deployments/{service.deployment.deployment_ref}/guardrails",
            headers=api_key_headers("other-workspace-key"),
            json={"max_concurrency": 3},
        )
        tenant_write = client.put(
            "/v1/guardrails",
            headers=api_key_headers("tenant-admin-key"),
            json={"max_concurrency": 4},
        )

    assert workspace_read.status_code == 403
    assert workspace_write.status_code == 403
    assert workspace_history.status_code == 403
    assert cross_workspace.status_code == 404
    assert tenant_write.status_code == 200
    assert tenant_write.json()["effective"]["max_concurrency"] == 4


async def test_custom_baseline_is_shared_and_partial_revisions_accumulate(sqlite_db) -> None:
    config = GuardrailConfig(
        rate_limit_capacity=3,
        rate_limit_refill_rate=0.25,
        quota_daily_limit=13,
        backpressure_queue_depth=17,
        max_concurrency=6,
    )
    service, app = await _client(sqlite_db, guardrail_config=config)
    ref = service.deployment.deployment_ref
    worker = service.worker
    assert worker is not None

    with TestClient(app) as client:
        initial = client.get(f"/v1/deployments/{ref}/guardrails", headers=operator_headers())
        first = client.put(
            f"/v1/deployments/{ref}/guardrails",
            headers=admin_headers(),
            json={"max_concurrency": 2},
        )
        second = client.put(
            f"/v1/deployments/{ref}/guardrails",
            headers=admin_headers(),
            json={"backpressure_queue_depth": 9},
        )

    expected = {
        "rate_limit_capacity": 3.0,
        "rate_limit_refill_rate": 0.25,
        "rate_limit_burst": 0.0,
        "quota_daily_limit": 13,
        "backpressure_queue_depth": 17,
        "max_concurrency": 6,
    }
    assert initial.json()["effective"] == expected
    assert first.json()["effective"] == {**expected, "max_concurrency": 2}
    assert second.json()["effective"] == {
        **expected,
        "backpressure_queue_depth": 9,
        "max_concurrency": 2,
    }
    assert await worker._effective_max_concurrency() == 2


async def test_guardrail_api_inspects_composed_overrides_and_retains_reset_tombstone(
    sqlite_db,
) -> None:
    config = GuardrailConfig(max_concurrency=6, backpressure_queue_depth=17)
    service, app = await _client(sqlite_db, guardrail_config=config)
    ref = service.deployment.deployment_ref

    with TestClient(app) as client:
        tenant = client.put(
            "/v1/guardrails",
            headers=admin_headers(),
            json={"max_concurrency": 5},
        )
        first = client.put(
            f"/v1/deployments/{ref}/guardrails",
            headers=admin_headers(),
            json={"max_concurrency": 2},
        )
        second = client.put(
            f"/v1/deployments/{ref}/guardrails",
            headers=admin_headers(),
            json={"backpressure_queue_depth": 9},
        )
        current = client.get(
            f"/v1/deployments/{ref}/guardrails",
            headers=operator_headers(),
        )
        reset = client.put(
            f"/v1/deployments/{ref}/guardrails",
            headers=admin_headers(),
            json={"reset_fields": ["max_concurrency"]},
        )
        history = client.get("/v1/guardrails/history", headers=admin_headers())

    assert tenant.status_code == first.status_code == second.status_code == 200
    assert current.json()["deployment_overrides"] == {
        "backpressure_queue_depth": 9,
        "max_concurrency": 2,
    }
    assert current.json()["deployment_revision"]["policy"] == {
        "backpressure_queue_depth": 9,
    }
    assert reset.status_code == 200
    assert reset.json()["deployment_overrides"] == {
        "backpressure_queue_depth": 9,
    }
    assert reset.json()["effective"]["max_concurrency"] == 5
    assert history.json()[-1]["policy"] == {"reset_fields": ["max_concurrency"]}


async def test_guardrail_management_rbac_and_invalid_changes_fail_closed(sqlite_db) -> None:
    service, app = await _client(sqlite_db)

    with TestClient(app) as client:
        forbidden = client.put(
            "/v1/guardrails",
            headers=reviewer_headers(),
            json={"reset_fields": ["max_concurrency"]},
        )
        invalid = client.put(
            "/v1/guardrails",
            headers=admin_headers(),
            json={"max_concurrency": 0},
        )
        unsafe_refill = client.put(
            "/v1/guardrails",
            headers=admin_headers(),
            json={"rate_limit_refill_rate": 5e-324},
        )
        invalid_null = client.put(
            "/v1/guardrails",
            headers=admin_headers(),
            json={"max_concurrency": None},
        )
        empty_reset = client.put(
            "/v1/guardrails",
            headers=admin_headers(),
            json={"reset_fields": []},
        )
        invalid_reset = client.put(
            "/v1/guardrails",
            headers=admin_headers(),
            json={"reset_fields": ["not_a_guardrail"]},
        )
        overlapping_reset = client.put(
            "/v1/guardrails",
            headers=admin_headers(),
            json={"max_concurrency": 2, "reset_fields": ["max_concurrency"]},
        )
        empty = client.put("/v1/guardrails", headers=admin_headers(), json={})
        history = client.get("/v1/guardrails/history", headers=admin_headers())

    assert forbidden.status_code == 403
    assert invalid.status_code == 422
    assert unsafe_refill.status_code == 422
    assert invalid_null.status_code == 422
    assert empty_reset.status_code == 422
    assert invalid_reset.status_code == 422
    assert overlapping_reset.status_code == 422
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
            json={"reset_fields": ["max_concurrency"]},
        )

    assert hidden.status_code == 404
    assert rejected.status_code == 404
    assert await service.guardrail_policy_repository.history() == []


async def test_deployment_guardrails_reject_foreign_tenant_deployment_through_owner_app(
    sqlite_db,
) -> None:
    auth = scoped_auth_config(
        ("owner", "owner-key", ServiceRole.ADMIN, "tenant-owner", None),
        ("foreign", "foreign-key", ServiceRole.ADMIN, "tenant-foreign", None),
    )
    owner, app = await _client(sqlite_db, auth_config=auth, tenant_id="tenant-owner")
    foreign, _ = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-foreign-guardrails"),
        deployment_ref="foreign-guardrails",
        auth_config=auth,
        tenant_id="tenant-foreign",
    )
    foreign_ref = foreign.deployment.deployment_ref

    with TestClient(app) as client:
        seeded = client.put(
            "/v1/guardrails",
            headers=api_key_headers("owner-key"),
            json={"max_concurrency": 2},
        )
        read = client.get(
            f"/v1/deployments/{foreign_ref}/guardrails",
            headers=api_key_headers("foreign-key"),
        )
        write = client.put(
            f"/v1/deployments/{foreign_ref}/guardrails",
            headers=api_key_headers("foreign-key"),
            json={"max_concurrency": 3},
        )

    assert seeded.status_code == 200
    assert read.status_code == write.status_code == 404
    history = await owner.guardrail_policy_repository.history()
    assert len(history) == 1
    assert history[0].scope == "tenant"


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
    auth = scoped_auth_config(
        ("operator", "scoped-operator-key", ServiceRole.OPERATOR, "tenant-a", "workspace-a"),
        ("admin", "scoped-admin-key", ServiceRole.ADMIN, "tenant-a", "workspace-a"),
    )
    service, app = await _client(
        sqlite_db,
        auth_config=auth,
        tenant_id="tenant-a",
        workspace_id="workspace-a",
    )
    worker = service.worker
    assert worker is not None
    service.worker = None
    ref = service.deployment.deployment_ref

    with TestClient(app) as client:
        client.put(
            f"/v1/deployments/{ref}/guardrails",
            headers=api_key_headers("scoped-admin-key"),
            json={"rate_limit_capacity": 100, "max_concurrency": 1},
        )
        accepted = client.post(
            "/runs",
            headers=api_key_headers("scoped-operator-key"),
            json={"input_payload": {"value": 1}},
        )

    assert accepted.status_code == 202
    claimed = await service.lease_manager.claim_pending(
        ref,
        "other-replica",
        tenant_id="tenant-a",
        workspace_id="workspace-a",
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
    assert await worker._claim_pending() is None
    audits = await service.audit_repository.list_by_node("service.guardrail.concurrency")
    assert len(audits) == 1
    assert audits[0].status == "rejected"
    assert {
        key: audits[0].execution_metadata[key]
        for key in ("active_count", "effective_limit", "reason_code", "utilization")
    } == {
        "active_count": 1,
        "effective_limit": 1,
        "reason_code": "concurrency",
        "utilization": 1.0,
    }
    foreign_workspace = AuditRepository.scoped(
        sqlite_db,
        ScopeContext(tenant_id="tenant-a", workspace_id="foreign-workspace"),
    )
    assert await foreign_workspace.list_by_node("service.guardrail.concurrency") == []
