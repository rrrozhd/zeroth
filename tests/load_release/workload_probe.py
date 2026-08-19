"""Real multi-scope HTTP workload used by the load/recovery release gate."""

from __future__ import annotations

import asyncio
import platform
import resource
import socket
import time
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import httpx
import uvicorn

from tests.service.helpers import agent_graph, approval_graph, deploy_service, scoped_auth_config
from zeroth.governance.identity import ServiceRole
from zeroth.service.app import create_app
from zeroth.service.bootstrap.factory import bootstrap_scoped_service


_CLAIMED_BY: dict[str, str] = {}


@dataclass(slots=True)
class Scope:
    service: Any
    auth: Any
    secrets: dict[str, str]
    surface: str
    replica: str
    accepts_requests: bool = True


@dataclass(slots=True)
class Target:
    scope: Scope
    client: httpx.AsyncClient


@dataclass(slots=True)
class _Runner:
    delay: float
    fails: bool = False

    async def run(self, input_payload: Any, **_kwargs: Any) -> SimpleNamespace:
        await asyncio.sleep(self.delay)
        if self.fails:
            raise RuntimeError("reproducible failing-script probe")
        return SimpleNamespace(output_data=dict(input_payload), audit_record={})


def memory_bytes() -> int:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(usage if platform.system() == "Darwin" else usage * 1024)


def credentials(tenant: str) -> tuple[Any, dict[str, str]]:
    secrets = {role: f"{tenant}-{role}-load-key" for role in ("operator", "admin")}
    auth = scoped_auth_config(
        ("operator", secrets["operator"], ServiceRole.OPERATOR, tenant, None),
        ("admin", secrets["admin"], ServiceRole.ADMIN, tenant, None),
    )
    return auth, secrets


def _server_shutdown_timeout(app: Any) -> float:
    """Let the worker exhaust its configured drain before timing out Uvicorn."""
    worker = getattr(app.state.bootstrap, "worker", None)
    return max(20.0, float(getattr(worker, "shutdown_timeout", 0.0)) + 5.0)


@asynccontextmanager
async def serve(app: Any):
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    deadline = asyncio.get_running_loop().time() + 20
    while not server.started:
        if task.done():
            await task
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError("load candidate did not start within 20 seconds")
        await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=_server_shutdown_timeout(app))


def headers(secret: str) -> dict[str, str]:
    return {"X-API-Key": secret}


def _graph(surface: str, graph_id: str):
    return (
        approval_graph(graph_id=graph_id)
        if surface == "approvals"
        else agent_graph(graph_id=graph_id)
    )


def install_runner(service: Any, surface: str) -> None:
    if surface == "approvals":
        return
    service.orchestrator.agent_runners["agent-step"] = _Runner(
        delay=0.1,
        fails=surface == "failing-script",
    )
    service.worker.poll_interval = 0.005
    execute = service.worker._execute_leased_run

    async def observed_execute(run_id, **kwargs):
        _CLAIMED_BY.setdefault(run_id, str(service.worker.worker_id))
        return await execute(run_id, **kwargs)

    service.worker._execute_leased_run = observed_execute


async def _replica(
    database: Any,
    deployment: Any,
    auth: Any,
    secrets: dict[str, str],
    surface: str,
    number: int,
) -> Scope:
    service = await bootstrap_scoped_service(
        database,
        deployment_ref=deployment.deployment_ref,
        tenant_id=deployment.tenant_id,
        workspace_id=None,
        auth_config=auth,
    )
    install_runner(service, surface)
    return Scope(service, auth, secrets, surface, f"replica-{number}")


async def _tenant_scopes(database: Any, tenant_number: int, surfaces: list[str]) -> list[Scope]:
    tenant = f"tenant-{tenant_number}"
    auth, secrets = credentials(tenant)
    scopes = []
    for deployment_number in (1, 2):
        surface = surfaces[(tenant_number - 1) * 2 + deployment_number - 1]
        graph_id = f"load-{tenant}-{deployment_number}"
        first, deployment = await deploy_service(
            database,
            _graph(surface, graph_id),
            deployment_ref=f"{tenant}-deployment-{deployment_number}",
            auth_config=auth,
            tenant_id=tenant,
        )
        install_runner(first, surface)
        scopes.append(Scope(first, auth, secrets, surface, "replica-1"))
        scopes.append(await _replica(database, deployment, auth, secrets, surface, 2))
        executor = await _replica(database, deployment, auth, secrets, surface, 3)
        executor.accepts_requests = False
        scopes.append(executor)
    return scopes


async def provision_scopes(database: Any, surfaces: list[str]) -> list[Scope]:
    scopes = []
    for tenant_number in range(1, 4):
        scopes.extend(await _tenant_scopes(database, tenant_number, surfaces))
    return scopes


def _terminal(status: str) -> str:
    if status in {"completed", "succeeded"}:
        return "completed"
    return "failed" if status in {"failed", "dead_letter"} else "cancelled"


async def _settle_run(
    target: Target,
    profile: str,
    sequence: int,
    run_id: str,
    profile_started: float,
) -> list[dict]:
    if profile == "overload" and sequence == 0:
        requested = time.perf_counter()
        response = await target.client.post(
            f"/admin/runs/{run_id}/cancel",
            headers=headers(target.scope.secrets["admin"]),
        )
        response.raise_for_status()
        body = response.json()
        failure = body.get("failure_state") or {}
        assert body.get("status") == "failed" and failure.get("reason") == "operator_cancelled", (
            "cancellation was not observed in the returned product state"
        )
        return [
            {
                "state": "cancel-requested",
                "at_ms": (requested - profile_started) * 1000,
                "run_id": run_id,
            },
            {
                "state": "cancelled",
                "at_ms": (time.perf_counter() - profile_started) * 1000,
                "run_id": run_id,
            },
        ]

    deadline = time.perf_counter() + 20
    approval_resolved = False
    while time.perf_counter() < deadline:
        response = await target.client.get(
            f"/runs/{run_id}", headers=headers(target.scope.secrets["operator"])
        )
        response.raise_for_status()
        body = response.json()
        status = str(body["status"])
        if status == "paused_for_approval" and not approval_resolved:
            approval_id = body["approval_paused_state"]["approval_id"]
            resolved = await target.client.post(
                f"/deployments/{target.scope.service.deployment.deployment_ref}/approvals/"
                f"{approval_id}/resolve",
                json={"decision": "approve"},
                headers=headers(target.scope.secrets["admin"]),
            )
            resolved.raise_for_status()
            approval_resolved = True
        elif status in {"succeeded", "failed", "cancelled", "dead_letter"}:
            return [
                {
                    "state": _terminal(status),
                    "at_ms": (time.perf_counter() - profile_started) * 1000,
                    "run_id": run_id,
                }
            ]
        await asyncio.sleep(0.01)
    raise AssertionError(f"load run {run_id} did not reach a terminal status")


async def _accepted_row(
    target: Target,
    profile: str,
    sequence: int,
    profile_started: float,
    started: float,
    cpu_started: float,
    response: httpx.Response,
) -> dict:
    responded = time.perf_counter()
    run_id = str(response.json()["run_id"])
    worker = await _observed_worker(target.scope.service, run_id)
    queue_depth = await target.scope.service.run_repository.count_pending(
        target.scope.service.deployment.deployment_ref
    )
    terminal = await _settle_run(target, profile, sequence, run_id, profile_started)
    settled = time.perf_counter()
    started_ms = (started - profile_started) * 1000
    finished_ms = (responded - profile_started) * 1000
    cpu = (time.process_time() - cpu_started) / max(settled - started, 1e-9) * 100
    return _row(
        target,
        profile,
        sequence,
        response.status_code,
        None,
        started_ms,
        finished_ms,
        queue_depth,
        worker,
        cpu,
        [
            {"state": "submitted", "at_ms": started_ms},
            {
                "state": "accepted",
                "at_ms": (responded - profile_started) * 1000,
                "run_id": run_id,
            },
            *terminal,
        ],
    )


async def _observed_worker(service: Any, run_id: str) -> str:
    deadline = time.perf_counter() + 20
    deployment = service.deployment
    while time.perf_counter() < deadline:
        if worker := _CLAIMED_BY.get(run_id):
            return worker
        worker = await service.worker.lease_manager.current_holder(
            run_id,
            tenant_id=deployment.tenant_id,
            workspace_id=deployment.workspace_id,
        )
        if worker is not None:
            return str(worker)
        await asyncio.sleep(0.001)
    raise AssertionError(f"run {run_id} executor was not observed")


def _row(
    target: Target,
    profile: str,
    sequence: int,
    status: int,
    retry_after: int | None,
    started_ms: float,
    finished_ms: float,
    queue_depth: int,
    worker: str,
    cpu_percent: float,
    lifecycle: list[dict],
) -> dict:
    return {
        "request_id": f"{profile}-{sequence}",
        "profile": profile,
        "tenant_id": target.scope.service.deployment.tenant_id,
        "deployment_ref": target.scope.service.deployment.deployment_ref,
        "replica": target.scope.replica,
        "worker": worker,
        "surface": target.scope.surface,
        "fault": None,
        "status_code": status,
        "retry_after_seconds": retry_after,
        "started_at_ms": round(started_ms, 6),
        "finished_at_ms": round(finished_ms, 6),
        "latency_ms": round(finished_ms - started_ms, 6),
        "queue_depth": queue_depth,
        "cpu_percent": round(cpu_percent, 6),
        "memory_bytes": memory_bytes(),
        "lifecycle": lifecycle,
    }


async def _measure(
    target: Target,
    profile: str,
    sequence: int,
    profile_started: float,
    semaphore: asyncio.Semaphore,
) -> dict:
    async with semaphore:
        started = time.perf_counter()
        cpu_started = time.process_time()
        response = await target.client.post(
            "/runs",
            json={"input_payload": {"value": sequence}},
            headers=headers(target.scope.secrets["operator"]),
        )
        if response.status_code == 202:
            return await _accepted_row(
                target,
                profile,
                sequence,
                profile_started,
                started,
                cpu_started,
                response,
            )
        finished = time.perf_counter()
        retry = response.headers.get("Retry-After")
        lifecycle = [
            {"state": "submitted", "at_ms": (started - profile_started) * 1000},
            {"state": "rejected", "at_ms": (finished - profile_started) * 1000},
        ]
        return _row(
            target,
            profile,
            sequence,
            response.status_code,
            int(retry) if retry else None,
            (started - profile_started) * 1000,
            (finished - profile_started) * 1000,
            0,
            str(target.scope.service.worker.worker_id),
            (time.process_time() - cpu_started) / max(finished - started, 1e-9) * 100,
            lifecycle,
        )


async def _run_profile(targets: list[Target], name: str, settings: dict) -> list[dict]:
    rate = float(settings["requests_per_second"])
    total = round(settings["duration_seconds"] * rate)
    semaphore = asyncio.Semaphore(settings["max_in_flight"])
    started = time.perf_counter()
    tasks = []
    for sequence in range(total):
        due = started + sequence / rate
        await asyncio.sleep(max(0.0, due - time.perf_counter()))
        target = targets[sequence % len(targets)]
        tasks.append(asyncio.create_task(_measure(target, name, sequence, started, semaphore)))
    return list(await asyncio.gather(*tasks))


async def collect_workload(scopes: list[Scope], profiles: dict) -> list[dict]:
    rows = []
    async with AsyncExitStack() as stack:
        targets = []
        for scope in scopes:
            origin = await stack.enter_async_context(serve(create_app(scope.service)))
            if scope.accepts_requests:
                client = await stack.enter_async_context(
                    httpx.AsyncClient(base_url=origin, timeout=10)
                )
                targets.append(Target(scope, client))
        for name, settings in profiles.items():
            rows.extend(await _run_profile(targets, name, settings))
    return rows
