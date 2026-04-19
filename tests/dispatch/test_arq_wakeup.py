"""Tests for the ARQ wakeup module."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr

from zeroth.core.dispatch.arq_wakeup import (
    WAKEUP_TASK_NAME,
    arq_settings_from_zeroth,
    create_arq_pool,
    enqueue_wakeup,
)


class _FakeRedisSettings:
    """Minimal stand-in for zeroth.core.config.settings.RedisSettings."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 6379,
        db: int = 0,
        password: SecretStr | None = None,
        tls: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.db = db
        self.password = password
        self.tls = tls


def test_arq_settings_from_zeroth() -> None:
    settings = _FakeRedisSettings(
        host="redis.test",
        port=6380,
        db=2,
        password=SecretStr("secret"),
        tls=True,
    )
    arq_settings = arq_settings_from_zeroth(settings)
    assert arq_settings.host == "redis.test"
    assert arq_settings.port == 6380
    assert arq_settings.database == 2
    assert arq_settings.password == "secret"
    assert arq_settings.ssl is True


def test_arq_settings_from_zeroth_no_password() -> None:
    settings = _FakeRedisSettings(password=None)
    arq_settings = arq_settings_from_zeroth(settings)
    assert arq_settings.password is None


@pytest.mark.asyncio
async def test_enqueue_wakeup_success() -> None:
    pool = MagicMock()
    pool.enqueue_job = AsyncMock()
    await enqueue_wakeup(pool, "run-abc")
    pool.enqueue_job.assert_awaited_once_with(
        WAKEUP_TASK_NAME,
        "run-abc",
        _job_id="wakeup:run-abc",
    )


@pytest.mark.asyncio
async def test_enqueue_wakeup_none_pool() -> None:
    # Should return without error when pool is None.
    await enqueue_wakeup(None, "run-abc")


@pytest.mark.asyncio
async def test_enqueue_wakeup_swallows_exception() -> None:
    pool = MagicMock()
    pool.enqueue_job = AsyncMock(side_effect=ConnectionError("boom"))
    # Must not raise.
    await enqueue_wakeup(pool, "run-abc")


@pytest.mark.asyncio
async def test_create_arq_pool_failure_returns_none() -> None:
    settings = _FakeRedisSettings()
    with patch("zeroth.core.dispatch.arq_wakeup.arq_settings_from_zeroth", side_effect=RuntimeError):
        result = await create_arq_pool(settings)
    assert result is None
