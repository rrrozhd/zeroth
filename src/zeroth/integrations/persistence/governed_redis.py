"""Governed-runtime Redis store factory.

Builds the governed runtime stores (for runs, interrupts, and audit events)
from a :class:`~zeroth.platform.storage.redis.RedisConfig`. This is
domain-aware wiring over the platform's Redis configuration: it constructs
runtime and governance store implementations, which is why it lives in the
integrations layer rather than in ``zeroth.platform.storage`` (Task 11); the
legacy ``zeroth.core.storage`` paths keep republishing both symbols lazily.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Imported for annotations only, preserving the lazy behavior this factory
    # had inside the storage layer: the governed stores are loaded when a
    # store is actually built, not when this module is imported.
    from zeroth.core.governed.runtime import RedisInterruptStore, RedisRunStore
    from zeroth.governance.audit.redis import RedisAuditEmitter
    from zeroth.platform.storage.redis import RedisConfig


@dataclass(frozen=True, slots=True)
class GovernAIRedisRuntimeStores:
    """A bundle of the three governed-runtime Redis-backed stores you need at runtime.

    Contains a run store (tracks run state), an interrupt store (handles
    pauses and approvals), and an audit emitter (records what happened).
    """

    run_store: RedisRunStore
    interrupt_store: RedisInterruptStore
    audit_emitter: RedisAuditEmitter


def build_governai_redis_runtime(
    config: RedisConfig,
    *,
    async_redis_client: Any | None = None,
    sync_redis_client: Any | None = None,
    require_docker_available: bool = False,
    container_inspector: Callable[[str], bool] | None = None,
) -> GovernAIRedisRuntimeStores:
    """Create all three governed-runtime Redis stores from a single RedisConfig.

    This is the main entry point for setting up Redis-backed runtime.
    It resolves the Redis URL and wires up the run store, interrupt store,
    and audit emitter with the right prefixes and TTLs.
    """
    from zeroth.core.governed.runtime import RedisInterruptStore, RedisRunStore
    from zeroth.governance.audit.redis import RedisAuditEmitter

    redis_url = config.redis_url(
        require_docker_available=require_docker_available,
        container_inspector=container_inspector,
    )
    return GovernAIRedisRuntimeStores(
        run_store=RedisRunStore(
            redis_url=redis_url,
            prefix=f"{config.key_prefix}:run",
            ttl_seconds=config.run_ttl_seconds,
            redis_client=async_redis_client,
        ),
        interrupt_store=RedisInterruptStore(
            redis_url=redis_url,
            prefix=f"{config.key_prefix}:interrupt",
            redis_client=sync_redis_client,
        ),
        audit_emitter=RedisAuditEmitter(
            redis_url=redis_url,
            prefix=f"{config.key_prefix}:audit",
            ttl_seconds=config.audit_ttl_seconds,
            redis_client=async_redis_client,
        ),
    )


__all__ = ["GovernAIRedisRuntimeStores", "build_governai_redis_runtime"]
