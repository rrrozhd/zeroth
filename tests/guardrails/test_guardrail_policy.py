"""Durable ingress-guardrail policy regressions."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests.conftest import requires_docker
from tests.service.helpers import admin_headers, agent_graph, deploy_service, operator_headers
from zeroth.governance.guardrails.policy import (
    EffectiveGuardrailSettings,
    GuardrailPolicyPatch,
    GuardrailPolicyRepository,
    effective_guardrails,
)
from zeroth.governance.guardrails import rate_limit as rate_limit_module
from zeroth.governance.guardrails.rate_limit import QuotaEnforcer, TokenBucketRateLimiter
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.integrations.persistence.runs.run_repository import GuardrailAdmissionRejectedError
from zeroth.platform.dispatch import LeaseManager
from zeroth.platform.storage import NullWorkspaceScopeContext
from zeroth.runtime.runs import Run
from zeroth.service.bootstrap import bootstrap_app


def test_policy_bounds_fail_closed() -> None:
    with pytest.raises(ValidationError):
        GuardrailPolicyPatch(rate_limit_capacity=0)
    with pytest.raises(ValidationError):
        GuardrailPolicyPatch(rate_limit_refill_rate=-1)
    with pytest.raises(ValidationError):
        GuardrailPolicyPatch(rate_limit_burst=1_000_001)
    with pytest.raises(ValidationError):
        GuardrailPolicyPatch(max_concurrency=0)
    with pytest.raises(ValidationError):
        GuardrailPolicyPatch(backpressure_queue_depth=0)
    with pytest.raises(ValidationError):
        GuardrailPolicyPatch(quota_daily_limit=0)
    with pytest.raises(ValidationError):
        GuardrailPolicyPatch(max_concurrency=2, reset_fields=("max_concurrency",))


def test_quota_unlimited_is_distinct_from_reset() -> None:
    unlimited = GuardrailPolicyPatch(quota_daily_limit=None)
    reset = GuardrailPolicyPatch(reset_fields=("quota_daily_limit",))

    assert unlimited.supplied_values() == {"quota_daily_limit": None}
    assert unlimited.reset_fields == ()
    assert reset.supplied_values() == {}
    assert reset.reset_fields == ("quota_daily_limit",)


def test_effective_policy_uses_field_wise_deployment_tenant_default_precedence() -> None:
    tenant = GuardrailPolicyPatch(
        rate_limit_capacity=20,
        rate_limit_refill_rate=2,
        rate_limit_burst=4,
        max_concurrency=3,
        quota_daily_limit=200,
    )
    deployment = GuardrailPolicyPatch(
        rate_limit_capacity=7,
        backpressure_queue_depth=11,
        quota_daily_limit=None,
    )

    effective = effective_guardrails(tenant=tenant, deployment=deployment)

    assert effective.rate_limit_capacity == 7
    assert effective.rate_limit_refill_rate == 2
    assert effective.rate_limit_burst == 4
    assert effective.max_concurrency == 3
    assert effective.backpressure_queue_depth == 11
    assert effective.quota_daily_limit is None


async def test_append_only_history_is_scoped_and_preserves_live_counters(sqlite_db) -> None:
    owner_scope = NullWorkspaceScopeContext(tenant_id="tenant-owner")
    foreign_scope = NullWorkspaceScopeContext(tenant_id="tenant-foreign")
    owner = GuardrailPolicyRepository.scoped(sqlite_db, owner_scope)
    foreign = GuardrailPolicyRepository.scoped(sqlite_db, foreign_scope)
    limiter = TokenBucketRateLimiter.scoped(sqlite_db, owner_scope)
    quota = QuotaEnforcer.scoped(sqlite_db, owner_scope)

    await limiter.check_and_consume("preserved", capacity=5, refill_rate=0)
    await quota.check_and_increment("preserved", limit=5)
    bucket_before = await limiter.get("preserved")
    quota_before = await quota.get("preserved")

    first = await owner.append(
        scope="tenant",
        policy=GuardrailPolicyPatch(rate_limit_capacity=12, max_concurrency=4),
        changed_by="operator-one",
    )
    second = await owner.append(
        scope="deployment",
        deployment_ref="deployment-a",
        policy=GuardrailPolicyPatch(rate_limit_burst=3, max_concurrency=2),
        changed_by="operator-two",
    )
    third = await owner.append(
        scope="deployment",
        deployment_ref="deployment-a",
        policy=GuardrailPolicyPatch(reset_fields=("max_concurrency",)),
        changed_by="operator-three",
    )

    history = await owner.history()
    effective = await owner.effective("deployment-a")
    assert [row.revision_id for row in history] == [
        first.revision_id,
        second.revision_id,
        third.revision_id,
    ]
    assert [row.changed_by for row in history] == [
        "operator-one",
        "operator-two",
        "operator-three",
    ]
    assert effective.rate_limit_capacity == 12
    assert effective.rate_limit_burst == 3
    assert effective.max_concurrency == 4
    assert await foreign.history() == []
    assert await limiter.get("preserved") == bucket_before
    assert await quota.get("preserved") == quota_before


async def test_current_overrides_compose_deltas_and_reset_one_field(sqlite_db) -> None:
    repository = GuardrailPolicyRepository.scoped(
        sqlite_db,
        NullWorkspaceScopeContext(tenant_id="tenant-reset"),
        baseline=EffectiveGuardrailSettings(max_concurrency=6),
    )
    await repository.append(
        scope="tenant",
        policy=GuardrailPolicyPatch(max_concurrency=5),
        changed_by="tenant-admin",
    )
    await repository.append(
        scope="deployment",
        deployment_ref="deployment-reset",
        policy=GuardrailPolicyPatch(max_concurrency=2),
        changed_by="deployment-admin",
    )
    await repository.append(
        scope="deployment",
        deployment_ref="deployment-reset",
        policy=GuardrailPolicyPatch(backpressure_queue_depth=9),
        changed_by="deployment-admin",
    )

    current = await repository.current("deployment", deployment_ref="deployment-reset")
    assert current is not None
    assert current.supplied_values() == {
        "backpressure_queue_depth": 9,
        "max_concurrency": 2,
    }

    reset = await repository.append(
        scope="deployment",
        deployment_ref="deployment-reset",
        policy=GuardrailPolicyPatch(reset_fields=("max_concurrency",)),
        changed_by="deployment-admin",
    )

    current = await repository.current("deployment", deployment_ref="deployment-reset")
    assert current is not None
    assert current.supplied_values() == {"backpressure_queue_depth": 9}
    assert (await repository.effective("deployment-reset")).max_concurrency == 5
    assert reset.policy.reset_fields == ("max_concurrency",)
    assert (await repository.history())[-1] == reset


async def test_concurrent_policy_writers_append_distinct_revisions(sqlite_db) -> None:
    repository = GuardrailPolicyRepository.scoped(
        sqlite_db, NullWorkspaceScopeContext(tenant_id="tenant-concurrent")
    )

    results = await asyncio.gather(
        *(
            repository.append(
                scope="deployment",
                deployment_ref="replicated",
                policy=GuardrailPolicyPatch(max_concurrency=index + 1),
                changed_by=f"replica-{index}",
            )
            for index in range(4)
        )
    )

    assert len({row.revision_id for row in results}) == 4
    assert len(await repository.history(scope="deployment", deployment_ref="replicated")) == 4


def _queued_run(index: int) -> Run:
    return Run(
        run_id=f"guarded-{index}",
        thread_id=f"guarded-thread-{index}",
        deployment_ref="guarded-deployment",
        graph_version_ref="guarded-graph@1",
    )


async def test_concurrent_replicas_cannot_overspend_queue_capacity(sqlite_db) -> None:
    settings = EffectiveGuardrailSettings(
        rate_limit_capacity=100,
        rate_limit_refill_rate=1,
        backpressure_queue_depth=1,
        max_concurrency=1,
    )

    async def admit(index: int):
        repository = RunRepository.for_default_compatibility(sqlite_db)
        return await repository.create_guarded(
            _queued_run(index),
            settings=settings,
            rate_limiter=TokenBucketRateLimiter(sqlite_db),
            quota_enforcer=QuotaEnforcer(sqlite_db),
        )

    results = await asyncio.gather(admit(1), admit(2), return_exceptions=True)
    admitted = [result for result in results if isinstance(result, Run)]
    rejected = [result for result in results if isinstance(result, GuardrailAdmissionRejectedError)]

    assert len(admitted) == len(rejected) == 1
    assert rejected[0].reason == "queue"
    assert rejected[0].retry_after_seconds >= 1
    assert (
        await RunRepository.for_default_compatibility(sqlite_db).count_pending("guarded-deployment")
        == 1
    )


async def test_queue_retry_after_is_a_time_interval_not_a_batch_count(sqlite_db) -> None:
    repository = RunRepository.for_default_compatibility(sqlite_db)
    for index in range(3):
        await repository.create(_queued_run(index))
    settings = EffectiveGuardrailSettings(
        rate_limit_capacity=100,
        backpressure_queue_depth=1,
        max_concurrency=1,
    )

    with pytest.raises(GuardrailAdmissionRejectedError) as rejected:
        await repository.create_guarded(
            _queued_run(4),
            settings=settings,
            rate_limiter=TokenBucketRateLimiter(sqlite_db),
            quota_enforcer=QuotaEnforcer(sqlite_db),
        )

    assert rejected.value.reason == "queue"
    assert rejected.value.retry_after_seconds == 1


async def _assert_distributed_replicas_share_running_concurrency(database) -> None:
    repository = RunRepository.for_default_compatibility(database)
    await repository.create(_queued_run(1))
    await repository.create(_queued_run(2))
    first = LeaseManager(database)
    second = LeaseManager(database)

    claimed = await asyncio.gather(
        first.claim_pending(
            "guarded-deployment",
            "replica-one",
            tenant_id="default",
            workspace_id=None,
            max_concurrency=1,
        ),
        second.claim_pending(
            "guarded-deployment",
            "replica-two",
            tenant_id="default",
            workspace_id=None,
            max_concurrency=1,
        ),
    )

    assert sum(run_id is not None for run_id in claimed) == 1


async def test_distributed_replicas_share_running_concurrency(sqlite_db) -> None:
    await _assert_distributed_replicas_share_running_concurrency(sqlite_db)


@requires_docker
async def test_postgres_replicas_share_running_concurrency(postgres_database) -> None:
    await _assert_distributed_replicas_share_running_concurrency(postgres_database)


@requires_docker
async def test_postgres_replicas_cannot_overspend_token_bucket(postgres_database) -> None:
    replicas = (
        TokenBucketRateLimiter(postgres_database),
        TokenBucketRateLimiter(postgres_database),
    )
    decisions = await asyncio.gather(
        *(
            replicas[index % len(replicas)].decide(
                "postgres-shared-bucket",
                capacity=5,
                refill_rate=0,
            )
            for index in range(20)
        )
    )

    assert sum(decision.allowed for decision in decisions) == 5
    assert (await replicas[0].get("postgres-shared-bucket"))["token_count"] == 0


@requires_docker
async def test_postgres_replicas_cannot_overspend_quota(postgres_database) -> None:
    replicas = (QuotaEnforcer(postgres_database), QuotaEnforcer(postgres_database))
    decisions = await asyncio.gather(
        *(
            replicas[index % len(replicas)].decide(
                "postgres-shared-quota",
                limit=5,
                window_seconds=86_400,
            )
            for index in range(20)
        )
    )

    assert sum(decision.allowed for decision in decisions) == 5
    assert (await replicas[0].get("postgres-shared-quota"))["value"] == 5


async def test_clock_drives_burst_exhaustion_and_exact_refill(sqlite_db, monkeypatch) -> None:
    now = [datetime(2026, 8, 14, 12, tzinfo=UTC)]
    monkeypatch.setattr(rate_limit_module, "utc_now", lambda: now[0])
    limiter = TokenBucketRateLimiter(sqlite_db)

    decisions = [await limiter.decide("clock-burst", capacity=3, refill_rate=0.5) for _ in range(4)]

    assert [decision.allowed for decision in decisions] == [True, True, True, False]
    assert decisions[-1].retry_after_seconds == 2
    now[0] += timedelta(seconds=2)
    refilled = await limiter.decide("clock-burst", capacity=3, refill_rate=0.5)
    assert refilled.allowed is True
    assert refilled.remaining == 0


async def test_deployed_service_applies_policy_update_without_restart(sqlite_db) -> None:
    service, _ = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-deployed-guardrails"),
        deployment_ref="deployed-guardrails",
    )
    service.worker = None
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        updated = client.put(
            "/v1/deployments/deployed-guardrails/guardrails",
            headers=admin_headers(),
            json={
                "rate_limit_capacity": 1,
                "rate_limit_refill_rate": 0.001,
                "backpressure_queue_depth": 100,
            },
        )
        accepted = client.post(
            "/runs", headers=operator_headers(), json={"input_payload": {"value": 1}}
        )
        rejected = client.post(
            "/runs", headers=operator_headers(), json={"input_payload": {"value": 2}}
        )

    assert updated.status_code == 200
    assert updated.json()["effective"]["rate_limit_capacity"] == 1
    assert accepted.status_code == 202
    assert rejected.status_code == 429
    assert rejected.headers["Retry-After"].isdigit()
