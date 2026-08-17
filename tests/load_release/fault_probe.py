"""Production-backed fault observations for the load/recovery release gate."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from tests.load_release.backend_fault_probe import (
    elapsed_ms,
    fault_row,
    postgres_contention,
    redis_loss,
)
from tests.load_release.workload_probe import _observed_worker, headers, install_runner, serve
from zeroth.contracts.governed.models.common import RunStatus
from zeroth.governance.guardrails.policy import GuardrailPolicyPatch
from zeroth.service.app import create_app
from zeroth.service.bootstrap.factory import bootstrap_scoped_service
from zeroth.service.webhooks.models import WebhookEventType, WebhookSubscription


async def _scoped_service(
    database: Any,
    anchor: tuple[Any, Any, dict[str, str]],
    deployment_ref: str,
    *,
    worker: bool,
    queue_depth: int = 1000,
    deploy: bool = True,
    runner_surface: str = "slow-script",
) -> tuple[Any, dict[str, str]]:
    seed, auth, secrets = anchor
    if deploy:
        await seed.deployment_service.deploy(
            deployment_ref,
            seed.deployment.graph_id,
            seed.deployment.graph_version,
            tenant_id=seed.deployment.tenant_id,
            workspace_id=None,
        )
    service = await bootstrap_scoped_service(
        database,
        deployment_ref=deployment_ref,
        tenant_id=seed.deployment.tenant_id,
        workspace_id=None,
        auth_config=auth,
    )
    if worker:
        install_runner(service, runner_surface)
    else:
        service.worker = None
    if deploy:
        await service.guardrail_policy_repository.append(
            scope="deployment",
            deployment_ref=deployment_ref,
            policy=GuardrailPolicyPatch(
                backpressure_queue_depth=queue_depth,
                rate_limit_capacity=1000,
            ),
            changed_by="load-release",
        )
    return service, secrets


async def _wait_status(
    client: httpx.AsyncClient, run_id: str, secret: str, expected: set[str]
) -> dict:
    deadline = time.perf_counter() + 10
    status = "unknown"
    while time.perf_counter() < deadline:
        response = await client.get(f"/runs/{run_id}", headers=headers(secret))
        response.raise_for_status()
        body = response.json()
        status = str(body["status"])
        if status in expected:
            return body
        await asyncio.sleep(0.01)
    raise AssertionError(f"run {run_id} did not recover; last status {status}")


async def _wait_terminal(client: httpx.AsyncClient, run_id: str, secret: str) -> str:
    body = await _wait_status(
        client, run_id, secret, {"succeeded", "failed", "cancelled", "dead_letter"}
    )
    status = str(body["status"])
    return "completed" if status == "succeeded" else "failed" if status == "dead_letter" else status


async def _submit_under_worker_loss(
    service: Any, secrets: dict[str, str], started: float, states: list[dict]
) -> tuple[str, tuple[float, float], tuple[float, float], int, int]:
    async with serve(create_app(service)) as origin:
        async with httpx.AsyncClient(base_url=origin, timeout=10) as client:
            accepted_started = time.perf_counter()
            accepted = await client.post(
                "/runs", json={"input_payload": {"value": 1}}, headers=headers(secrets["operator"])
            )
            accepted_finished = time.perf_counter()
            assert accepted.status_code == 202
            run_id = str(accepted.json()["run_id"])
            states.append({"state": "accepted", "at_ms": elapsed_ms(started), "run_id": run_id})
            rejected_started = time.perf_counter()
            rejected = await client.post(
                "/runs", json={"input_payload": {"value": 2}}, headers=headers(secrets["operator"])
            )
            rejected_finished = time.perf_counter()
            assert rejected.status_code == 503
            states.append({"state": "rejected", "at_ms": elapsed_ms(started)})
            return (
                run_id,
                (accepted_started, accepted_finished),
                (rejected_started, rejected_finished),
                rejected.status_code,
                int(rejected.headers["Retry-After"]),
            )


async def worker_loss(database: Any, anchor: tuple[Any, Any, dict[str, str]]) -> list[dict]:
    """Leave accepted work durable, replace the worker, and observe settlement."""
    deployment_ref = f"{anchor[0].deployment.tenant_id}-worker-loss"
    service, secrets = await _scoped_service(
        database,
        anchor,
        deployment_ref,
        worker=False,
        queue_depth=1,
        runner_surface="failing-script",
    )
    started = time.perf_counter()
    states = [
        {"state": "fault-injected", "at_ms": 0.0},
        {"state": "worker-withdrawn", "at_ms": 0.0},
    ]
    run_id, accepted_window, rejected_window, status, retry_after = await _submit_under_worker_loss(
        service, secrets, started, states
    )
    rejected_states = [state for state in states if state["state"] == "rejected"]
    states = [state for state in states if state["state"] != "rejected"]
    replacement, _ = await _scoped_service(
        database,
        anchor,
        deployment_ref,
        worker=True,
        queue_depth=1,
        deploy=False,
        runner_surface="failing-script",
    )
    async with serve(create_app(replacement)) as origin:
        states.append({"state": "worker-replaced", "at_ms": elapsed_ms(started)})
        async with httpx.AsyncClient(base_url=origin, timeout=10) as client:
            terminal = await _wait_terminal(client, run_id, secrets["operator"])
            states.append({"state": terminal, "at_ms": elapsed_ms(started), "run_id": run_id})
    return [
        fault_row(
            "worker-loss",
            "failing-script",
            started,
            states,
            status=202,
            retry_after=None,
            service=replacement,
            request_id="fault-worker-loss-accepted",
            request_started=accepted_window[0],
            request_finished=accepted_window[1],
            worker_id=str(replacement.worker.worker_id),
        ),
        fault_row(
            "worker-loss",
            "failing-script",
            started,
            rejected_states,
            status=status,
            retry_after=retry_after,
            service=service,
            request_id="fault-worker-loss-rejected",
            request_started=rejected_window[0],
            request_finished=rejected_window[1],
            worker_id="api-without-worker",
            recovered=False,
        ),
    ]


async def _submit_for_restart(
    service: Any, secrets: dict[str, str], started: float, states: list[dict]
) -> str:
    async with serve(create_app(service)) as origin:
        async with httpx.AsyncClient(base_url=origin, timeout=10) as client:
            accepted = await client.post(
                "/runs", json={"input_payload": {"value": 1}}, headers=headers(secrets["operator"])
            )
            accepted.raise_for_status()
            run_id = str(accepted.json()["run_id"])
            states.append({"state": "accepted", "at_ms": elapsed_ms(started), "run_id": run_id})
            worker = await _observed_worker(service, run_id)
            states.append(
                {
                    "state": "draining",
                    "at_ms": elapsed_ms(started),
                    "run_id": run_id,
                    "worker_id": worker,
                }
            )
    return run_id


async def _resume_run(
    replacement: Any,
    secrets: dict[str, str],
    run_id: str,
    started: float,
    states: list[dict],
) -> None:
    async with serve(create_app(replacement)) as origin:
        states.append({"state": "service-started", "at_ms": elapsed_ms(started)})
        async with httpx.AsyncClient(base_url=origin, timeout=10) as client:
            terminal = await _wait_terminal(client, run_id, secrets["operator"])
            states.append({"state": terminal, "at_ms": elapsed_ms(started), "run_id": run_id})


async def service_restart(database: Any, anchor: tuple[Any, Any, dict[str, str]]) -> dict:
    """Restart a real service around durable accepted work and settle it once."""
    deployment_ref = f"{anchor[0].deployment.tenant_id}-service-restart"
    service, secrets = await _scoped_service(
        database,
        anchor,
        deployment_ref,
        worker=True,
        runner_surface="slow-script",
    )
    service.orchestrator.agent_runners["agent-step"].delay = 2
    service.worker.shutdown_timeout = 0
    started = time.perf_counter()
    states = [{"state": "fault-injected", "at_ms": 0.0}]
    run_id = await _submit_for_restart(service, secrets, started, states)
    drained = await service.run_repository.get(run_id)
    assert drained is not None and drained.status is RunStatus.PENDING
    states.append({"state": "service-stopped", "at_ms": elapsed_ms(started)})
    replacement, _ = await _scoped_service(
        database,
        anchor,
        deployment_ref,
        worker=True,
        deploy=False,
        runner_surface="slow-script",
    )
    await _resume_run(replacement, secrets, run_id, started, states)
    return fault_row(
        "service-restart",
        "slow-script",
        started,
        states,
        status=202,
        retry_after=None,
        service=replacement,
    )


class _DelayedTransport(httpx.AsyncBaseTransport):
    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.transport = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(self.delay)
        return await self.transport.handle_async_request(request)

    async def aclose(self) -> None:
        await self.transport.aclose()


async def network_delay(database: Any, anchor: tuple[Any, Any, dict[str, str]]) -> dict:
    """Delay a real HTTP transport once, then observe the undelayed path."""
    deployment_ref = f"{anchor[0].deployment.tenant_id}-network-delay"
    service, _ = await _scoped_service(database, anchor, deployment_ref, worker=False)
    started = time.perf_counter()
    states = [{"state": "fault-injected", "at_ms": 0.0}]
    async with serve(create_app(service)) as origin:
        async with httpx.AsyncClient(
            base_url=origin, transport=_DelayedTransport(0.05), timeout=10
        ) as delayed:
            response = await delayed.get("/health/ready")
            assert response.status_code == 200 and elapsed_ms(started) >= 50
            states.append({"state": "transport-delayed", "at_ms": elapsed_ms(started)})
        async with httpx.AsyncClient(base_url=origin, timeout=10) as recovered:
            assert (await recovered.get("/health/ready")).status_code == 200
            states.extend(
                [
                    {"state": "request-completed", "at_ms": elapsed_ms(started)},
                    {"state": "transport-restored", "at_ms": elapsed_ms(started)},
                ]
            )
    return fault_row(
        "network-delay",
        "langgraph-streams",
        started,
        states,
        status=200,
        retry_after=None,
        service=service,
    )


class _ThrottledDownstream:
    def __init__(self) -> None:
        self.attempts = 0

    async def __call__(self, _request: httpx.Request) -> httpx.Response:
        self.attempts += 1
        if self.attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(204)


async def _exercise_webhook_retry(
    service: Any,
    worker: Any,
    downstream: _ThrottledDownstream,
    started: float,
    states: list[dict],
) -> None:
    subscription = await service.webhook_service.create_subscription(
        WebhookSubscription(
            deployment_ref=service.deployment.deployment_ref,
            tenant_id=service.deployment.tenant_id,
            target_url="https://93.184.216.34/hook",
            event_types=[WebhookEventType.RUN_COMPLETED],
        )
    )
    deliveries = await service.webhook_service.emit_event(
        event_type=WebhookEventType.RUN_COMPLETED,
        deployment_ref=service.deployment.deployment_ref,
        tenant_id=service.deployment.tenant_id,
        data={"source": "load-release"},
    )
    assert len(deliveries) == 1 and deliveries[0].subscription_id == subscription.subscription_id
    first = await service.webhook_repository.claim_pending_delivery()
    assert first is not None
    await worker._deliver(first.delivery, first.generation)
    states.extend(
        [
            {"state": "downstream-429", "at_ms": elapsed_ms(started)},
            {"state": "rejected", "at_ms": elapsed_ms(started)},
        ]
    )
    retry = await service.webhook_repository.claim_pending_delivery()
    assert retry is not None
    states.append({"state": "delivery-retried", "at_ms": elapsed_ms(started)})
    await worker._deliver(retry.delivery, retry.generation)
    assert downstream.attempts == 2
    states.append({"state": "delivered", "at_ms": elapsed_ms(started)})


async def downstream_throttling(database: Any, anchor: tuple[Any, Any, dict[str, str]]) -> dict:
    """Drive a real persisted webhook delivery through 429 and automatic retry."""
    deployment_ref = f"{anchor[0].deployment.tenant_id}-downstream-throttling"
    service, _ = await _scoped_service(database, anchor, deployment_ref, worker=False)
    worker = service.delivery_worker
    assert worker is not None and service.webhook_service is not None
    downstream = _ThrottledDownstream()
    client = httpx.AsyncClient(transport=httpx.MockTransport(downstream))
    worker.http_client = client
    worker.retry_base_delay = 0
    worker.retry_max_delay = 0
    service.delivery_worker = None
    started = time.perf_counter()
    states = [{"state": "fault-injected", "at_ms": 0.0}]
    try:
        async with serve(create_app(service)):
            await _exercise_webhook_retry(service, worker, downstream, started, states)
    finally:
        await client.aclose()
    return fault_row(
        "downstream-throttling",
        "webhooks",
        started,
        states,
        status=429,
        retry_after=1,
        service=service,
    )


async def collect_fault_observations(
    database: Any,
    anchors: dict[str, tuple[Any, Any, dict[str, str]]],
    *,
    postgres_dsn: str,
    redis_url: str,
) -> list[dict]:
    """Collect one fault-specific, automatically recovered observation per class."""
    return [
        await postgres_contention(postgres_dsn),
        await redis_loss(redis_url),
        *await worker_loss(database, anchors["failing-script"]),
        await service_restart(database, anchors["slow-script"]),
        await network_delay(database, anchors["langgraph-streams"]),
        await downstream_throttling(database, anchors["webhooks"]),
    ]
