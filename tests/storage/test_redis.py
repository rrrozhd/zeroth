from __future__ import annotations

import pytest
from pydantic import SecretStr

from zeroth.core.storage import (
    RedisConfig,
    RedisDeploymentMode,
    build_governai_redis_runtime,
)


def test_local_redis_config_builds_default_url() -> None:
    config = RedisConfig()

    assert config.mode is RedisDeploymentMode.LOCAL
    assert config.redis_url() == "redis://127.0.0.1:6379/0"
    assert config.masked_redis_url() == "redis://127.0.0.1:6379/0"


def test_remote_redis_config_masks_credentials() -> None:
    config = RedisConfig(
        mode=RedisDeploymentMode.REMOTE,
        host="cache.example.net",
        port=6380,
        database=2,
        username="zeroth",
        password=SecretStr("s3cr3t"),
        ssl=True,
    )

    assert config.redis_url() == "rediss://zeroth:s3cr3t@cache.example.net:6380/2"
    assert config.masked_redis_url() == "rediss://zeroth:***@cache.example.net:6380/2"


def test_docker_redis_config_uses_provisioned_container_host() -> None:
    config = RedisConfig(
        mode=RedisDeploymentMode.DOCKER,
        port=6381,
        docker_container_name="zeroth-cache",
        docker_host="redis-service",
    )

    assert config.redis_url() == "redis://redis-service:6381/0"
    assert config.docker_container_available(
        container_inspector=lambda name: name == "zeroth-cache"
    )


def test_docker_redis_config_raises_when_container_is_required_but_missing() -> None:
    config = RedisConfig(
        mode=RedisDeploymentMode.DOCKER,
        docker_container_name="zeroth-cache",
    )

    with pytest.raises(RuntimeError, match="not available"):
        config.redis_url(
            require_docker_available=True,
            container_inspector=lambda _name: False,
        )


def test_redis_config_from_env(monkeypatch) -> None:
    monkeypatch.setenv("ZEROTH_REDIS_MODE", "remote")
    monkeypatch.setenv("ZEROTH_REDIS_HOST", "redis.internal")
    monkeypatch.setenv("ZEROTH_REDIS_PORT", "6390")
    monkeypatch.setenv("ZEROTH_REDIS_DATABASE", "4")
    monkeypatch.setenv("ZEROTH_REDIS_SSL", "true")
    monkeypatch.setenv("ZEROTH_REDIS_USERNAME", "svc")
    monkeypatch.setenv("ZEROTH_REDIS_PASSWORD", "pw")
    monkeypatch.setenv("ZEROTH_REDIS_KEY_PREFIX", "zeroth-prod")
    monkeypatch.setenv("ZEROTH_REDIS_RUN_TTL_SECONDS", "300")
    monkeypatch.setenv("ZEROTH_REDIS_AUDIT_TTL_SECONDS", "600")
    monkeypatch.setenv("ZEROTH_REDIS_DOCKER_BINARY", "docker")

    config = RedisConfig.from_env()

    assert config.mode is RedisDeploymentMode.REMOTE
    assert config.redis_url() == "rediss://svc:pw@redis.internal:6390/4"
    assert config.key_prefix == "zeroth-prod"
    assert config.run_ttl_seconds == 300
    assert config.audit_ttl_seconds == 600


def test_build_governai_redis_runtime_uses_resolved_prefixes() -> None:
    config = RedisConfig(
        mode=RedisDeploymentMode.REMOTE,
        url="redis://cache.service:6379/1",
        key_prefix="zeroth-stage",
        run_ttl_seconds=120,
        audit_ttl_seconds=240,
    )

    runtime = build_governai_redis_runtime(
        config,
        async_redis_client=object(),
        sync_redis_client=object(),
    )

    assert runtime.run_store.redis_url == "redis://cache.service:6379/1"
    assert runtime.run_store.prefix == "zeroth-stage:run"
    assert runtime.run_store.ttl_seconds == 120
    assert runtime.interrupt_store.prefix == "zeroth-stage:interrupt"
    assert runtime.audit_emitter.prefix == "zeroth-stage:audit"
    assert runtime.audit_emitter.ttl_seconds == 240
