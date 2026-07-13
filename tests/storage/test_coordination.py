from __future__ import annotations

import asyncio
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from psycopg.errors import LockNotAvailable, QueryCanceled

from tests.conftest import requires_docker
from zeroth.core.storage import async_sqlite
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


class _DiagnosticLockTimeout(LockNotAvailable):
    @property
    def diag(self) -> SimpleNamespace:
        return SimpleNamespace(
            message_primary="canceling statement due to lock timeout",
            message_detail=None,
            context=None,
        )


def test_sqlite_script_splitter_ignores_semicolons_inside_literals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    complete_statement = async_sqlite.sqlite3.complete_statement
    calls: list[str] = []

    def track_complete_statement(statement: str) -> bool:
        calls.append(statement)
        return complete_statement(statement)

    monkeypatch.setattr(async_sqlite.sqlite3, "complete_statement", track_complete_statement)
    literal = ";" * 1_000
    statements = list(
        async_sqlite._split_sql_script(
            f"INSERT INTO items (value) VALUES ('escaped''quote{literal}');"
            f" -- {literal}\n SELECT 1;"
            f" /* {literal} */ SELECT 2;"
        )
    )

    assert len(statements) == 3
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_sqlite_execute_script_preserves_triggers_and_rolls_back(tmp_path: Path) -> None:
    database = AsyncSQLiteDatabase(str(tmp_path / "script-trigger.db"))
    async with database.transaction() as connection:
        await connection.execute_script(
            """
            CREATE TABLE items (value TEXT);
            CREATE TABLE item_log (value TEXT);
            CREATE TRIGGER log_item AFTER INSERT ON items BEGIN
                INSERT INTO item_log (value) VALUES ('literal;semicolon');
                INSERT INTO item_log (value) VALUES (NEW.value);
            END;
            """
        )

    with pytest.raises(RuntimeError, match="rollback script"):
        async with database.transaction(write_lock=True) as connection:
            await connection.execute_script("INSERT INTO items (value) VALUES ('kept;together');")
            raise RuntimeError("rollback script")

    async with database.transaction() as connection:
        items = await connection.fetch_all("SELECT value FROM items")
        log = await connection.fetch_all("SELECT value FROM item_log")
    assert items == []
    assert log == []


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
async def test_sqlite_execute_script_stays_inside_write_lock(tmp_path: Path) -> None:
    database_path = str(tmp_path / "script-boundary.db")
    first = AsyncSQLiteDatabase(database_path, coordination_timeout_seconds=1.0)
    second = AsyncSQLiteDatabase(database_path, coordination_timeout_seconds=1.0)
    script_finished = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def execute_script_while_holding_lock() -> None:
        async with first.transaction(write_lock=True) as connection:
            await connection.execute_script(
                "CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT);"
                "INSERT INTO items (id, name) VALUES (1, 'alpha');"
            )
            script_finished.set()
            await release_first.wait()

    async def acquire_second_lock() -> None:
        async with second.transaction(write_lock=True):
            second_entered.set()

    first_task = asyncio.create_task(execute_script_while_holding_lock())
    await script_finished.wait()
    second_task = asyncio.create_task(acquire_second_lock())

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(second_entered.wait(), timeout=0.05)

    release_first.set()
    await asyncio.gather(first_task, second_task)

    async with first.transaction() as connection:
        row = await connection.fetch_one("SELECT name FROM items WHERE id = ?", (1,))
    assert row == {"name": "alpha"}


@pytest.mark.asyncio
async def test_sqlite_ordinary_read_then_write_survives_interleaved_writer(
    tmp_path: Path,
) -> None:
    database_path = str(tmp_path / "ordinary-upgrade.db")
    first = AsyncSQLiteDatabase(database_path, coordination_timeout_seconds=1.0)
    second = AsyncSQLiteDatabase(database_path, coordination_timeout_seconds=1.0)
    first_read = asyncio.Event()
    second_committed = asyncio.Event()

    async with first.transaction() as connection:
        await connection.execute_script(
            "CREATE TABLE items (id INTEGER PRIMARY KEY, value INTEGER);"
            "INSERT INTO items (id, value) VALUES (1, 0);"
            "INSERT INTO items (id, value) VALUES (2, 0);"
        )

    async def read_then_write() -> None:
        async with first.transaction() as connection:
            await connection.fetch_one("SELECT value FROM items WHERE id = ?", (1,))
            first_read.set()
            await second_committed.wait()
            await connection.execute("UPDATE items SET value = 1 WHERE id = ?", (1,))

    async def interleaved_writer() -> None:
        await first_read.wait()
        async with second.transaction() as connection:
            await connection.execute("UPDATE items SET value = 1 WHERE id = ?", (2,))
        second_committed.set()

    await asyncio.gather(read_then_write(), interleaved_writer())

    async with first.transaction() as connection:
        rows = await connection.fetch_all("SELECT value FROM items ORDER BY id")
    assert rows == [{"value": 1}, {"value": 1}]


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
@pytest.mark.parametrize(
    "error",
    [
        QueryCanceled("canceling statement due to statement timeout"),
        QueryCanceled("canceling statement due to user request"),
        LockNotAvailable("could not obtain lock on row in relation"),
    ],
)
async def test_postgres_write_lock_preserves_body_database_errors(
    error: Exception,
) -> None:
    connection = _PostgresConnection()
    database = AsyncPostgresDatabase(_PostgresPool(connection))  # type: ignore[arg-type]

    with pytest.raises(type(error)):
        async with database.transaction(write_lock=True):
            raise error

    assert connection.executed == ["SET LOCAL lock_timeout = '5000ms'"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        QueryCanceled("canceling statement due to lock timeout"),
        _DiagnosticLockTimeout("could not obtain lock"),
    ],
)
async def test_postgres_write_lock_maps_identified_body_lock_timeout(
    error: Exception,
) -> None:
    connection = _PostgresConnection()
    database = AsyncPostgresDatabase(_PostgresPool(connection))  # type: ignore[arg-type]

    with pytest.raises(CoordinationTimeoutError, match="coordination lock"):
        async with database.transaction(write_lock=True):
            raise error


@requires_docker
@pytest.mark.asyncio
async def test_postgres_contended_row_lock_uses_bounded_coordination_error(
    postgres_database: AsyncPostgresDatabase,
    postgres_container: object,
) -> None:
    url = postgres_container.get_connection_url()  # type: ignore[attr-defined]
    dsn = url.replace("postgresql+psycopg2://", "postgresql://")
    contender = await AsyncPostgresDatabase.create(
        dsn,
        min_size=1,
        max_size=1,
        coordination_timeout_seconds=0.1,
    )
    try:
        async with postgres_database.transaction() as connection:
            await connection.execute("DROP TABLE IF EXISTS zeroth_coordination_lock_test")
            await connection.execute(
                "CREATE TABLE zeroth_coordination_lock_test (id INTEGER PRIMARY KEY)"
            )
            await connection.execute(
                "INSERT INTO zeroth_coordination_lock_test (id) VALUES (?)", (1,)
            )

        async with postgres_database.transaction(write_lock=True) as holder:
            await holder.fetch_one(
                "SELECT id FROM zeroth_coordination_lock_test WHERE id = ? FOR UPDATE",
                (1,),
            )
            started_at = monotonic()
            with pytest.raises(CoordinationTimeoutError, match="coordination lock"):
                async with contender.transaction(write_lock=True) as blocked:
                    await blocked.fetch_one(
                        "SELECT id FROM zeroth_coordination_lock_test WHERE id = ? FOR UPDATE",
                        (1,),
                    )
            assert monotonic() - started_at < 1.0
    finally:
        await contender.close()
        async with postgres_database.transaction() as connection:
            await connection.execute("DROP TABLE IF EXISTS zeroth_coordination_lock_test")


@pytest.mark.asyncio
async def test_postgres_ordinary_transaction_preserves_query_cancellation() -> None:
    connection = _PostgresConnection()
    database = AsyncPostgresDatabase(_PostgresPool(connection))  # type: ignore[arg-type]

    with pytest.raises(QueryCanceled):
        async with database.transaction():
            raise QueryCanceled("statement timeout")

    assert connection.executed == []


@pytest.mark.asyncio
async def test_postgres_create_validates_timeout_before_opening_pool() -> None:
    with patch("zeroth.core.storage.async_postgres.AsyncConnectionPool") as pool_type:
        with pytest.raises(ValueError, match="finite positive"):
            await AsyncPostgresDatabase.create(
                "postgresql://example.invalid/database",
                coordination_timeout_seconds=float("inf"),
            )

    pool_type.assert_not_called()
