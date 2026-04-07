"""ARQ-backed wakeup notifications for low-latency run dispatch.

ARQ is used strictly as a wakeup signal -- the Postgres lease store
remains the authoritative queue. If ARQ/Redis is unavailable, workers
fall back to poll-based dispatch.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

WAKEUP_TASK_NAME = "wakeup_worker"


def arq_settings_from_zeroth(redis_settings: Any) -> Any:
    """Convert ZerothSettings.redis to ARQ RedisSettings.

    Args:
        redis_settings: A RedisSettings instance from zeroth.config.settings.

    Returns:
        An arq.connections.RedisSettings instance.
    """
    from arq.connections import RedisSettings as ArqRedisSettings

    password = (
        redis_settings.password.get_secret_value()
        if redis_settings.password
        else None
    )
    return ArqRedisSettings(
        host=redis_settings.host,
        port=redis_settings.port,
        database=redis_settings.db,
        password=password,
        ssl=redis_settings.tls,
    )


async def create_arq_pool(redis_settings: Any) -> Any:
    """Create an ARQ connection pool from Zeroth Redis settings.

    Returns None if ARQ or Redis is unavailable.
    """
    try:
        from arq import create_pool

        arq_redis = arq_settings_from_zeroth(redis_settings)
        return await create_pool(arq_redis)
    except Exception:
        logger.warning("Failed to create ARQ pool, wakeup notifications disabled")
        return None


async def enqueue_wakeup(arq_pool: Any, run_id: str) -> None:
    """Best-effort wakeup enqueue. Never raises.

    Enqueues a minimal ARQ job carrying only the run_id as a signal
    for a worker to check the lease store. The job itself does nothing --
    the act of receiving it IS the wakeup signal.
    """
    if arq_pool is None:
        return
    try:
        await arq_pool.enqueue_job(
            WAKEUP_TASK_NAME,
            run_id,
            _job_id=f"wakeup:{run_id}",
        )
    except Exception:
        logger.debug(
            "ARQ wakeup enqueue failed for %s, poll fallback active", run_id
        )


async def run_arq_consumer(
    redis_settings: Any,
    on_wakeup: Callable[[str], Awaitable[None]],
) -> None:
    """Run ARQ consumer as a background task.

    Calls on_wakeup(run_id) for each wakeup signal received.
    Runs until cancelled. Designed to be wrapped in asyncio.create_task.

    Args:
        redis_settings: Zeroth RedisSettings (will be converted to ARQ format).
        on_wakeup: Async callback invoked when a wakeup signal arrives.
    """
    from arq.worker import Worker as ArqWorker

    async def handle_wakeup(ctx: dict, run_id: str) -> None:
        await on_wakeup(run_id)

    handle_wakeup.__qualname__ = WAKEUP_TASK_NAME  # ARQ uses qualname for matching

    arq_redis = arq_settings_from_zeroth(redis_settings)
    worker = ArqWorker(
        functions=[handle_wakeup],
        redis_settings=arq_redis,
        burst=False,
        max_jobs=10,
    )
    await worker.async_run()
