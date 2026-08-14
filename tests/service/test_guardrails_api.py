"""Tests for guardrail enforcement in the run creation API."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tests.service.helpers import (
    agent_graph,
    deploy_service,
    operator_headers,
)
from zeroth.service.bootstrap import bootstrap_app
from zeroth.governance.guardrails.rate_limit import QuotaEnforcer, TokenBucketRateLimiter
from zeroth.governance.identity import ActorIdentity, AuthMethod
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.runtime.runs import Run
from zeroth.service.api.run_api import _check_guardrails

DEPLOYMENT = "guardrail-test"


async def test_rate_limit_rejection_returns_429_with_retry_after(sqlite_db) -> None:
    service, _ = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-ratelimit"),
        deployment_ref=DEPLOYMENT,
    )
    # Use a tiny capacity so it exhausts immediately.
    service.guardrail_config.rate_limit_capacity = 1.0
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        # First request consumes the only token.
        r1 = client.post(
            "/runs",
            json={"input_payload": {"value": 1}},
            headers=operator_headers(),
        )
        # Second request should be rate-limited.
        r2 = client.post(
            "/runs",
            json={"input_payload": {"value": 2}},
            headers=operator_headers(),
        )

    assert r1.status_code == 202
    assert r2.status_code == 429
    assert r2.headers.get("Retry-After") is not None


async def test_queue_rejection_returns_503_with_retry_after(sqlite_db) -> None:
    service, _ = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-backpressure"),
        deployment_ref=DEPLOYMENT + "-bp",
    )
    # Set depth limit to 1 — any run already in the queue triggers backpressure.
    service.guardrail_config.backpressure_queue_depth = 1
    service.worker = None

    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        # Pause the worker so runs pile up — use a very large rate limit capacity.
        service.guardrail_config.rate_limit_capacity = 1000.0

        # First run is created (queue depth 0 → 1, within limit since limit is 1 not 0).
        r1 = client.post(
            "/runs",
            json={"input_payload": {"value": 1}},
            headers=operator_headers(),
        )
        r2 = client.post(
            "/runs",
            json={"input_payload": {"value": 2}},
            headers=operator_headers(),
        )

    assert r1.status_code == 202
    assert r2.status_code == 503
    assert r2.headers["Retry-After"].isdigit()
    assert "queue" in r2.json()["detail"].lower()


async def test_quota_rejection_returns_503_with_retry_after(sqlite_db) -> None:
    service, _ = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-quota"),
        deployment_ref=DEPLOYMENT + "-quota",
    )
    service.guardrail_config.quota_daily_limit = 1

    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        r1 = client.post(
            "/runs",
            json={"input_payload": {"value": 1}},
            headers=operator_headers(),
        )
        r2 = client.post(
            "/runs",
            json={"input_payload": {"value": 2}},
            headers=operator_headers(),
        )

    assert r1.status_code == 202
    assert r2.status_code == 503
    assert "quota" in r2.json()["detail"].lower()
    assert r2.headers["Retry-After"].isdigit()


def _guardrail_run(tenant_id: str, deployment_ref: str = "same-logical-deployment") -> Run:
    return Run(
        run_id=f"run-{tenant_id}",
        thread_id=f"thread-{tenant_id}",
        graph_version_ref="graph:v1",
        deployment_ref=deployment_ref,
        tenant_id=tenant_id,
        submitted_by=ActorIdentity(subject="same-subject", auth_method=AuthMethod.API_KEY),
    )


async def test_run_api_token_bucket_key_isolates_same_subject_and_deployment_by_tenant(
    sqlite_db,
) -> None:
    bootstrap = SimpleNamespace(
        guardrail_config=SimpleNamespace(
            backpressure_queue_depth=100,
            rate_limit_capacity=1.0,
            rate_limit_refill_rate=0.001,
            quota_daily_limit=None,
        ),
        run_repository=RunRepository.for_default_compatibility(sqlite_db),
        rate_limiter=TokenBucketRateLimiter(sqlite_db),
    )
    tenant_a = _guardrail_run("tenant-a")
    tenant_b = _guardrail_run("tenant-b")

    await _check_guardrails(bootstrap, tenant_a)
    with pytest.raises(HTTPException) as exhausted:
        await _check_guardrails(bootstrap, tenant_a)
    await _check_guardrails(bootstrap, tenant_b)

    assert exhausted.value.status_code == 429


async def test_run_api_quota_key_isolates_same_subject_and_deployment_by_tenant(sqlite_db) -> None:
    bootstrap = SimpleNamespace(
        guardrail_config=SimpleNamespace(
            backpressure_queue_depth=100,
            rate_limit_capacity=100.0,
            rate_limit_refill_rate=0.001,
            quota_daily_limit=1,
        ),
        run_repository=RunRepository.for_default_compatibility(sqlite_db),
        rate_limiter=None,
        quota_enforcer=QuotaEnforcer(sqlite_db),
    )
    tenant_a = _guardrail_run("tenant-a")
    tenant_b = _guardrail_run("tenant-b")

    await _check_guardrails(bootstrap, tenant_a)
    with pytest.raises(HTTPException) as exhausted:
        await _check_guardrails(bootstrap, tenant_a)
    await _check_guardrails(bootstrap, tenant_b)

    assert exhausted.value.status_code == 503


async def test_run_api_token_bucket_identity_resists_delimiter_collision(sqlite_db) -> None:
    bootstrap = SimpleNamespace(
        guardrail_config=SimpleNamespace(
            backpressure_queue_depth=100,
            rate_limit_capacity=1.0,
            rate_limit_refill_rate=0.001,
            quota_daily_limit=None,
        ),
        run_repository=RunRepository.for_default_compatibility(sqlite_db),
        rate_limiter=TokenBucketRateLimiter(sqlite_db),
    )
    first = _guardrail_run("alpha:deployment:shared", "tail")
    collision = _guardrail_run("alpha", "shared:deployment:tail")

    await _check_guardrails(bootstrap, first)
    await _check_guardrails(bootstrap, collision)
    with pytest.raises(HTTPException) as first_exhausted:
        await _check_guardrails(bootstrap, first)
    with pytest.raises(HTTPException) as collision_exhausted:
        await _check_guardrails(bootstrap, collision)

    assert first_exhausted.value.status_code == collision_exhausted.value.status_code == 429


async def test_run_api_quota_identity_resists_delimiter_collision(sqlite_db) -> None:
    bootstrap = SimpleNamespace(
        guardrail_config=SimpleNamespace(
            backpressure_queue_depth=100,
            rate_limit_capacity=100.0,
            rate_limit_refill_rate=0.001,
            quota_daily_limit=1,
        ),
        run_repository=RunRepository.for_default_compatibility(sqlite_db),
        rate_limiter=None,
        quota_enforcer=QuotaEnforcer(sqlite_db),
    )
    first = _guardrail_run("alpha:deployment:shared", "tail")
    collision = _guardrail_run("alpha", "shared:deployment:tail")

    await _check_guardrails(bootstrap, first)
    await _check_guardrails(bootstrap, collision)
    with pytest.raises(HTTPException) as first_exhausted:
        await _check_guardrails(bootstrap, first)
    with pytest.raises(HTTPException) as collision_exhausted:
        await _check_guardrails(bootstrap, collision)

    assert first_exhausted.value.status_code == collision_exhausted.value.status_code == 503
