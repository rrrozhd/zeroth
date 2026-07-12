from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from psycopg.errors import LockNotAvailable, QueryCanceled

from zeroth.core.storage.async_postgres import AsyncPostgresDatabase
from zeroth.core.storage.async_sqlite import AsyncSQLiteDatabase
from zeroth.core.storage.database import CoordinationTimeoutError


class _AsyncContext:
    def __init__(self, value: object) -> None:
        self.value = value

    async def __aenter__(self) -> object:
        return self.value

    async def __aexit__(self, *_args: object) -> None:
        return None


class _PostgresConnection:
    def __init__(self, error: Exception | None = None) -> None:
        self.executed: list[str] = []
        self.error = error

    def transaction(self) -> _AsyncContext:
        return _AsyncContext(self)

    async def execute(self, sql: str) -> None:
        self.executed.append(sql)
        if self.error is not None:
            raise self.error


class _PostgresPool:
    def __init__(self, connection: _PostgresConnection) -> None:
        self._connection = connection

    def connection(self) -> _AsyncContext:
        return _AsyncContext(self._connection)


@pytest.mark.parametrize("database_type", [AsyncSQLiteDatabase, AsyncPostgresDatabase])
def test_coordination_timeout_must_be_finite(database_type: type[object], tmp_path: Path) -> None:
    target: object = str(tmp_path / "bounded.db")
    if database_type is AsyncPostgresDatabase:
        target = _PostgresPool(_PostgresConnection())

    with pytest.raises(ValueError, match="finite positive"):
        database_type(target, coordination_timeout_seconds=float("inf"))  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_sqlite_write_lock_blocks_second_database_until_release(tmp_path: Path) -> None:
    database_path = str(tmp_path / "coordination.db")
    first = AsyncSQLiteDatabase(database_path, coordination_timeout_seconds=1.0)
    second = AsyncSQLiteDatabase(database_path, coordination_timeout_seconds=1.0)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def hold_first_lock() -> None:
        async with first.transaction(write_lock=True):
            first_entered.set()
            await release_first.wait()

    async def acquire_second_lock() -> None:
        async with second.transaction(write_lock=True):
            second_entered.set()

    first_task = asyncio.create_task(hold_first_lock())
    await first_entered.wait()
    second_task = asyncio.create_task(acquire_second_lock())

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(second_entered.wait(), timeout=0.05)

    release_first.set()
    await asyncio.gather(first_task, second_task)

    assert second_entered.is_set()
    assert first.backend == second.backend == "sqlite"


@pytest.mark.asyncio
async def test_sqlite_write_lock_timeout_uses_coordination_error(tmp_path: Path) -> None:
    database_path = str(tmp_path / "timeout.db")
    first = AsyncSQLiteDatabase(database_path, coordination_timeout_seconds=1.0)
    second = AsyncSQLiteDatabase(database_path, coordination_timeout_seconds=0.05)

    async with first.transaction(write_lock=True):
        with pytest.raises(CoordinationTimeoutError, match="coordination lock"):
            async with second.transaction(write_lock=True):
                pytest.fail("timed-out transaction entered its critical section")


@pytest.mark.asyncio
async def test_postgres_write_lock_sets_bounded_local_timeout() -> None:
    connection = _PostgresConnection()
    database = AsyncPostgresDatabase(
        _PostgresPool(connection),  # type: ignore[arg-type]
        coordination_timeout_seconds=0.125,
    )

    async with database.transaction(write_lock=True):
        pass

    assert connection.executed == ["SET LOCAL lock_timeout = '125ms'"]
    assert database.backend == "postgres"


@pytest.mark.asyncio
async def test_postgres_lock_timeout_uses_coordination_error() -> None:
    connection = _PostgresConnection(LockNotAvailable("lock timeout"))
    database = AsyncPostgresDatabase(
        _PostgresPool(connection),  # type: ignore[arg-type]
        coordination_timeout_seconds=0.01,
    )

    with pytest.raises(CoordinationTimeoutError, match="coordination lock"):
        async with database.transaction(write_lock=True):
            pytest.fail("timed-out transaction entered its critical section")


@pytest.mark.asyncio
async def test_postgres_ordinary_transaction_preserves_query_cancellation() -> None:
    connection = _PostgresConnection()
    database = AsyncPostgresDatabase(_PostgresPool(connection))  # type: ignore[arg-type]

    with pytest.raises(QueryCanceled):
        async with database.transaction():
            raise QueryCanceled("statement timeout")

    assert connection.executed == []
