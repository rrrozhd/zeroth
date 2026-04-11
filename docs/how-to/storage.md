# How to use storage

## Overview

The storage layer is how every persistent Zeroth subsystem — runs, threads, approvals, contracts, audit — talks to a database. You rarely use it directly from application code; instead you hand an `AsyncDatabase` instance to the repositories and registries that do the real work. This guide shows how to open a database, run migrations, and query runs through a session-style connection.

## Minimal example

```python
import asyncio

from zeroth.core.runs import RunRepository, RunStatus
from zeroth.core.storage.async_sqlite import AsyncSQLiteDatabase


async def main() -> None:
    # 1. Open an async SQLite database (in-memory for this example)
    db = AsyncSQLiteDatabase(path=":memory:")
    await db.connect()

    try:
        # 2. Initialise a repository — this runs Alembic migrations on first use
        runs = RunRepository(db)
        await runs.initialize()

        # 3. Query: list every run that is still pending
        pending = await runs.list_by_status(RunStatus.PENDING)
        print(f"{len(pending)} pending runs")

        # 4. Read one run with its history
        if pending:
            run = await runs.get(pending[0].id)
            for entry in run.history:
                print(f"{entry.node_id:20s} -> {entry.status}")

    finally:
        # 5. Always close — this releases the underlying connection pool
        await db.close()


asyncio.run(main())
```

The pattern is always the same: `connect → initialise repositories → use them → close`. For Postgres, swap `AsyncSQLiteDatabase` for `AsyncPostgresDatabase.create(dsn)` — or just use `create_database(settings)`, which selects the right backend based on your `ZerothSettings`.

## Common patterns

- **Let `create_database` decide.** Production code should almost never instantiate `AsyncSQLiteDatabase` or `AsyncPostgresDatabase` directly. Call `create_database(settings)` so the backend is driven by config.
- **Share one database across subsystems.** Build one `AsyncDatabase` at startup and pass it to every repository and registry. They all use the same transactions and the same pool.
- **Use Redis for ephemeral runtime state.** Don't reach for SQL when you need cross-worker coordination — `GovernAIRedisRuntimeStores` (via `build_governai_redis_runtime`) is the right primitive for dispatch, thread state, and distributed locks.
- **Encrypt secrets at rest.** The SQLite backend supports `EncryptedField` for per-column encryption; enable it by setting `database.encryption_key` in your settings.

## Pitfalls

1. **Forgetting to `connect()`.** `AsyncSQLiteDatabase` and `AsyncPostgresDatabase` are both lazy — calling repository methods without `await db.connect()` raises an opaque "database not connected" error.
2. **Postgres pool sizing.** `postgres_pool_min` / `postgres_pool_max` must reflect your actual concurrency. Undersized pools block every request; oversized pools exhaust Postgres `max_connections`.
3. **SQLite vs Postgres SQL drift.** The repositories paper over most differences, but hand-written SQL in your own subsystems must use portable syntax if you want to target both. Prefer parameter binding over string interpolation.
4. **Alembic migrations on startup.** `repository.initialize()` runs migrations in-process. If two workers boot simultaneously against the same empty database, one will lose the race and retry. Plan for it — use a leader-election step, or run migrations out-of-band in CI.
5. **Closing too eagerly.** Calling `db.close()` while a repository still holds an in-flight transaction raises. Always `await` the repository call first, then close.

## Reference cross-link

- [Python API reference — `zeroth.core.storage`](../reference/python-api.md)
