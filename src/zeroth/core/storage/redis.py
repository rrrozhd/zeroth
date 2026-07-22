"""Legacy import path for the platform Redis configuration.

The connection configuration lives in :mod:`zeroth.platform.storage.redis`.
The governed-runtime store factory lives in
:mod:`zeroth.integrations.persistence.governed_redis` and is republished
lazily: resolving it eagerly would put runtime and governance code on the
import path of everything that touches storage.
"""

from typing import TYPE_CHECKING, Any

from zeroth.platform.storage.redis import (
    RedisConfig,
    RedisDeploymentMode,
    docker_container_running,
)

if TYPE_CHECKING:
    from zeroth.integrations.persistence.governed_redis import (
        GovernAIRedisRuntimeStores,
        build_governai_redis_runtime,
    )

__all__ = [
    "GovernAIRedisRuntimeStores",
    "RedisConfig",
    "RedisDeploymentMode",
    "build_governai_redis_runtime",
    "docker_container_running",
]


def __getattr__(name: str) -> Any:
    """Lazily republish the governed store factory from the integrations layer."""
    if name in {"GovernAIRedisRuntimeStores", "build_governai_redis_runtime"}:
        import zeroth.integrations.persistence.governed_redis as governed_redis

        return getattr(governed_redis, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
