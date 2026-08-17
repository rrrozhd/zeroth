"""Real PostgreSQL/Redis fault observations for the load release gate."""

from __future__ import annotations

import contextlib
import time
from typing import Any
from uuid import uuid4

from tests.load_release.workload_probe import memory_bytes
from zeroth.platform.artifacts.store import RedisArtifactStore
from zeroth.platform.artifacts.tenant_scoped import TenantScopedArtifactStore
from zeroth.platform.storage.async_postgres import AsyncPostgresDatabase
from zeroth.platform.storage.database import CoordinationTimeoutError


def elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 6)


def fault_row(
    fault: str,
    surface: str,
    started: float,
    states: list[dict],
    *,
    status: int,
    retry_after: int | None,
    service: Any | None = None,
) -> dict:
    elapsed = elapsed_ms(started)
    deployment = None if service is None else service.deployment
    worker = None if service is None else service.worker
    return {
        "request_id": f"fault-{fault}",
        "profile": "overload",
        "tenant_id": "tenant-1" if deployment is None else deployment.tenant_id,
        "deployment_ref": (
            "tenant-1-deployment-1" if deployment is None else deployment.deployment_ref
        ),
        "replica": "replica-1",
        "worker": "not-applicable" if worker is None else str(worker.worker_id),
        "surface": surface,
        "fault": fault,
        "status_code": status,
        "retry_after_seconds": retry_after,
        "started_at_ms": 0.0,
        "finished_at_ms": elapsed,
        "latency_ms": elapsed,
        "queue_depth": 1,
        "cpu_percent": 0.0,
        "memory_bytes": memory_bytes(),
        "lifecycle": [
            *states,
            {"state": "recovered", "at_ms": elapsed_ms(started), "repair": "automatic"},
        ],
    }


async def postgres_contention(dsn: str) -> dict:
    """Observe the native bounded coordination timeout and a succeeding retry."""
    import psycopg

    started = time.perf_counter()
    owner = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    contender = await AsyncPostgresDatabase.create(
        dsn, min_size=1, max_size=1, coordination_timeout_seconds=0.1
    )
    states = [{"state": "fault-injected", "at_ms": 0.0}]
    try:
        await owner.execute("BEGIN")
        await owner.execute("LOCK TABLE runs IN ACCESS EXCLUSIVE MODE")
        try:
            async with contender.transaction(write_lock=True) as connection:
                await connection.fetch_one("SELECT count(*) AS total FROM runs")
        except CoordinationTimeoutError:
            states.extend(
                [
                    {"state": "coordination-timeout", "at_ms": elapsed_ms(started)},
                    {"state": "rejected", "at_ms": elapsed_ms(started)},
                ]
            )
        else:  # pragma: no cover - a real lock must make this unreachable
            raise AssertionError("PostgreSQL contention did not reach the bounded timeout")
        await owner.execute("COMMIT")
        async with contender.transaction(write_lock=True) as connection:
            assert await connection.fetch_one("SELECT count(*) AS total FROM runs") is not None
        states.append({"state": "query-restored", "at_ms": elapsed_ms(started)})
    finally:
        with contextlib.suppress(Exception):
            await owner.execute("ROLLBACK")
        await owner.close()
        await contender.close()
    return fault_row(
        "database-contention", "slow-script", started, states, status=503, retry_after=1
    )


async def redis_loss(url: str) -> dict:
    """Observe a Redis-backed product artifact across disconnect/reconnect."""
    import redis.asyncio as redis
    from redis.exceptions import ConnectionError as RedisConnectionError

    started = time.perf_counter()
    prefix = f"zeroth:load:{uuid4().hex}"
    key = "redis-loss-run/node"
    client = redis.from_url(url)
    store = TenantScopedArtifactStore(
        RedisArtifactStore("", prefix=prefix, client=client), tenant_id="tenant-1"
    )
    await store.store(key, b"retained", "application/octet-stream")
    states = [{"state": "fault-injected", "at_ms": 0.0}]
    missing = redis.from_url("redis://127.0.0.1:1/14", socket_connect_timeout=0.05)
    unavailable = TenantScopedArtifactStore(
        RedisArtifactStore("", prefix=prefix, client=missing), tenant_id="tenant-1"
    )
    try:
        try:
            await unavailable.retrieve(key)
        except (RedisConnectionError, OSError):
            states.extend(
                [
                    {"state": "artifact-unavailable", "at_ms": elapsed_ms(started)},
                    {"state": "rejected", "at_ms": elapsed_ms(started)},
                ]
            )
        else:  # pragma: no cover
            raise AssertionError("Redis loss probe unexpectedly retrieved the artifact")
    finally:
        await missing.aclose()
    reconnected = redis.from_url(url)
    try:
        recovered = TenantScopedArtifactStore(
            RedisArtifactStore("", prefix=prefix, client=reconnected), tenant_id="tenant-1"
        )
        assert await recovered.retrieve(key) == b"retained"
        states.append({"state": "artifact-restored", "at_ms": elapsed_ms(started)})
        await recovered.delete(key, idempotency_key="load-release-cleanup")
    finally:
        await reconnected.aclose()
        await client.aclose()
    return fault_row("redis-loss", "artifacts", started, states, status=503, retry_after=1)
