"""Redis configuration and GovernAI-backed runtime store factories.

This module lets you configure how Zeroth connects to Redis (locally, via
Docker, or to a remote server) and builds the GovernAI runtime stores
(for runs, interrupts, and audit events) from that configuration.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from governai.audit.redis import RedisAuditEmitter
from governai.runtime import RedisInterruptStore, RedisRunStore
from pydantic import BaseModel, ConfigDict, Field, SecretStr


class RedisDeploymentMode(StrEnum):
    """How Redis is being run: on the local machine, inside Docker, or on a remote server."""

    LOCAL = "local"
    DOCKER = "docker"
    REMOTE = "remote"


class RedisConfig(BaseModel):
    """All the settings needed to connect to a Redis instance.

    Supports local installs, Docker containers, and remote servers.
    You can set these values directly or load them from environment
    variables using ``from_env()``.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    mode: RedisDeploymentMode = RedisDeploymentMode.LOCAL
    url: str | None = None
    host: str = "127.0.0.1"
    port: int = Field(default=6379, ge=1, le=65535)
    database: int = Field(default=0, ge=0)
    username: str | None = None
    password: SecretStr | None = None
    ssl: bool = False
    key_prefix: str = "zeroth"
    run_ttl_seconds: int | None = Field(default=None, ge=1)
    audit_ttl_seconds: int | None = Field(default=None, ge=1)
    docker_binary: str = "docker"
    docker_container_name: str = "zeroth-redis"
    docker_host: str | None = None

    def redis_url(
        self,
        *,
        require_docker_available: bool = False,
        container_inspector: Callable[[str], bool] | None = None,
    ) -> str:
        """Build and return the full Redis connection URL.

        If a URL was set directly, it's returned as-is. Otherwise, the URL
        is built from the host, port, and authentication settings.
        """
        if self.url:
            return self.url
        if require_docker_available and self.mode is RedisDeploymentMode.DOCKER and not (
            self.docker_container_available(container_inspector=container_inspector)
        ):
            raise RuntimeError(
                f"redis container {self.docker_container_name!r} is not available"
            )
        scheme = "rediss" if self.ssl else "redis"
        auth = self._auth_fragment()
        host = self.host
        if self.mode is RedisDeploymentMode.DOCKER:
            host = self.docker_host or self.docker_container_name
        return f"{scheme}://{auth}{host}:{self.port}/{self.database}"

    def masked_redis_url(self) -> str:
        """Return the Redis URL with any password replaced by '***'.

        Safe to use in logs and error messages.
        """
        resolved = self.redis_url()
        parts = urlsplit(resolved)
        if "@" not in parts.netloc:
            return resolved
        auth, host = parts.netloc.rsplit("@", 1)
        if ":" in auth:
            username, _password = auth.split(":", 1)
            masked_auth = f"{username}:***"
        else:
            masked_auth = "***"
        return urlunsplit(
            (parts.scheme, f"{masked_auth}@{host}", parts.path, parts.query, parts.fragment)
        )

    def docker_container_available(
        self,
        *,
        container_inspector: Callable[[str], bool] | None = None,
    ) -> bool:
        """Check if the Docker Redis container is currently running."""
        if self.mode is not RedisDeploymentMode.DOCKER:
            return False
        inspector = container_inspector or (
            lambda container_name: docker_container_running(
                container_name,
                docker_binary=self.docker_binary,
            )
        )
        return bool(inspector(self.docker_container_name))

    @classmethod
    def from_env(cls, prefix: str = "ZEROTH_REDIS_") -> RedisConfig:
        """Create a RedisConfig by reading environment variables.

        Looks for variables like ZEROTH_REDIS_HOST, ZEROTH_REDIS_PORT, etc.
        You can change the prefix if needed.
        """
        data: dict[str, Any] = {}
        for field_name in (
            "MODE",
            "URL",
            "HOST",
            "PORT",
            "DATABASE",
            "USERNAME",
            "PASSWORD",
            "SSL",
            "KEY_PREFIX",
            "RUN_TTL_SECONDS",
            "AUDIT_TTL_SECONDS",
            "DOCKER_BINARY",
            "DOCKER_CONTAINER_NAME",
            "DOCKER_HOST",
        ):
            value = os.getenv(f"{prefix}{field_name}")
            if value in (None, ""):
                continue
            data[field_name.lower()] = value
        if "mode" in data:
            data["mode"] = RedisDeploymentMode(data["mode"])
        for numeric_key in ("port", "database", "run_ttl_seconds", "audit_ttl_seconds"):
            if numeric_key in data:
                data[numeric_key] = int(data[numeric_key])
        if "ssl" in data:
            data["ssl"] = _parse_bool(data["ssl"])
        if "password" in data:
            data["password"] = SecretStr(data["password"])
        return cls(**data)

    def _auth_fragment(self) -> str:
        """Build the username:password@ portion of a Redis URL."""
        username = quote(self.username or "", safe="")
        password_value = self.password.get_secret_value() if self.password is not None else None
        if username and password_value is not None:
            return f"{username}:{quote(password_value, safe='')}@"
        if username:
            return f"{username}@"
        if password_value is not None:
            return f":{quote(password_value, safe='')}@"
        return ""


@dataclass(frozen=True, slots=True)
class GovernAIRedisRuntimeStores:
    """A bundle of the three GovernAI Redis-backed stores you need at runtime.

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
    """Create all three GovernAI Redis stores from a single RedisConfig.

    This is the main entry point for setting up Redis-backed runtime.
    It resolves the Redis URL and wires up the run store, interrupt store,
    and audit emitter with the right prefixes and TTLs.
    """
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


def docker_container_running(container_name: str, *, docker_binary: str = "docker") -> bool:
    """Check if a Docker container with the given name is currently running.

    Calls ``docker inspect`` under the hood. Returns False if Docker
    isn't installed or the container doesn't exist.
    """
    try:
        result = subprocess.run(
            [docker_binary, "inspect", "-f", "{{.State.Running}}", container_name],
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def _parse_bool(value: str) -> bool:
    """Convert a string like 'true', 'yes', '1' to a Python bool.

    Raises ValueError if the string isn't a recognized boolean value.
    """
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean value: {value!r}")
