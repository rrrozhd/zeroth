"""Tests for the ARQ wakeup module."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr

from zeroth.platform.dispatch.arq_wakeup import (
    WAKEUP_TASK_NAME,
    arq_settings_from_zeroth,
    create_arq_pool,
    enqueue_wakeup,
)


class _FakeRedisSettings:
    """Minimal stand-in for zeroth.platform.config.settings.RedisSettings."""

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
    with patch(
        "zeroth.platform.dispatch.arq_wakeup.arq_settings_from_zeroth",
        side_effect=RuntimeError,
    ):
        result = await create_arq_pool(settings)
    assert result is None


# --- ZER-48 / A08-10: the swallowed failure has to name its cause -------------
#
# Both paths here swallow their exception on purpose -- the Postgres lease store
# is the authoritative queue and poll dispatch keeps runs moving -- so the log
# line is the *only* evidence the degradation ever happened. It carried neither
# the exception type nor its message, and the enqueue path logged at DEBUG,
# which is off in production: bad credentials and an unreachable host produced
# the same invisible line.
#
# ``caplog`` is set to DEBUG deliberately. At WARNING a regression to
# ``logger.debug`` would emit no record at all and the assertions would fail with
# an IndexError instead of naming the level that was actually used.

_WAKEUP_LOGGER = "zeroth.platform.dispatch.arq_wakeup"


@pytest.mark.asyncio
async def test_enqueue_failure_logs_the_exception_type_and_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The swallowed enqueue error names what went wrong, not just that it did."""
    pool = MagicMock()
    pool.enqueue_job = AsyncMock(side_effect=ConnectionError("redis refused the connection"))

    with caplog.at_level(logging.DEBUG, logger=_WAKEUP_LOGGER):
        await enqueue_wakeup(pool, "run-abc")

    records = [r for r in caplog.records if r.name == _WAKEUP_LOGGER]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "ConnectionError" in message, f"the exception type is missing from {message!r}"
    assert "redis refused the connection" in message
    assert "run-abc" in message


@pytest.mark.asyncio
async def test_enqueue_failure_is_logged_at_warning_not_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A degradation an operator can act on is logged where they will see it.

    DEBUG is off in production, so wakeups stopping altogether was silent.
    """
    pool = MagicMock()
    pool.enqueue_job = AsyncMock(side_effect=ConnectionError("boom"))

    with caplog.at_level(logging.DEBUG, logger=_WAKEUP_LOGGER):
        await enqueue_wakeup(pool, "run-abc")

    records = [r for r in caplog.records if r.name == _WAKEUP_LOGGER]
    assert [r.levelno for r in records] == [logging.WARNING]


@pytest.mark.asyncio
async def test_pool_creation_failure_logs_the_exception_type_and_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A pool that cannot be built says which failure it was."""
    settings = _FakeRedisSettings()

    with (
        caplog.at_level(logging.DEBUG, logger=_WAKEUP_LOGGER),
        patch(
            "zeroth.platform.dispatch.arq_wakeup.arq_settings_from_zeroth",
            side_effect=RuntimeError("bad credentials"),
        ),
    ):
        result = await create_arq_pool(settings)

    assert result is None
    records = [r for r in caplog.records if r.name == _WAKEUP_LOGGER]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "RuntimeError" in message, f"the exception type is missing from {message!r}"
    assert "bad credentials" in message
    assert records[0].levelno == logging.WARNING


@pytest.mark.asyncio
async def test_a_successful_enqueue_logs_no_degradation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The happy path is silent, so the warning above means something."""
    pool = MagicMock()
    pool.enqueue_job = AsyncMock()

    with caplog.at_level(logging.DEBUG, logger=_WAKEUP_LOGGER):
        await enqueue_wakeup(pool, "run-abc")

    assert [r for r in caplog.records if r.name == _WAKEUP_LOGGER] == []
