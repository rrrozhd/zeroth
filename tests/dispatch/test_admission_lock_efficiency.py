"""Warm admission locking preserves serialization without repeated seed writes."""

from types import SimpleNamespace

import pytest

from tests.conftest import requires_docker
from zeroth.platform.dispatch.lease import LeaseManager


@requires_docker
async def test_existing_admission_row_is_locked_without_a_seed_write(dual_database):
    manager = LeaseManager(dual_database)
    scope = {"tenant_id": "default", "workspace_id": None}
    async with dual_database.transaction(write_lock=True) as connection:
        await manager._lock_admission_scope(connection, "warm-admission", **scope)

    statements = []
    async with dual_database.transaction(write_lock=True) as connection:
        async def execute(sql, params=()):
            statements.append(sql)
            return await connection.execute(sql, params)

        async def fetch_one(sql, params=()):
            statements.append(sql)
            return await connection.fetch_one(sql, params)

        observed = SimpleNamespace(execute=execute, fetch_one=fetch_one)
        await manager._lock_admission_scope(observed, "warm-admission", **scope)

    assert len(statements) == 1
    assert statements[0].lstrip().startswith("SELECT")
    if manager._is_postgres():
        assert "FOR UPDATE" in statements[0]


@requires_docker
@pytest.mark.parametrize("sample_time", [False, True])
async def test_concurrent_cold_scopes_lock_the_single_created_row(postgres_database, sample_time):
    import asyncio

    manager = LeaseManager(postgres_database)
    both_missing = asyncio.Event()
    misses = 0
    active = 0

    async def acquire():
        nonlocal misses, active
        async with postgres_database.transaction(write_lock=True) as connection:
            first_read = True

            async def fetch_one(sql, params=()):
                nonlocal first_read, misses
                row = await connection.fetch_one(sql, params)
                if first_read:
                    first_read = False
                    assert row is None
                    misses += 1
                    if misses == 2:
                        both_missing.set()
                    await asyncio.wait_for(both_missing.wait(), timeout=5)
                return row

            observed = SimpleNamespace(execute=connection.execute, fetch_one=fetch_one)
            sampled = await manager._lock_admission_scope(
                observed, "cold-admission-race", tenant_id="default", workspace_id=None,
                sample_time=sample_time,
            )
            assert (sampled is not None) is sample_time
            assert active == 0
            active += 1
            await asyncio.sleep(.01)
            active -= 1

    await asyncio.wait_for(asyncio.gather(acquire(), acquire()), timeout=10)
    assert misses == 2
    async with postgres_database.transaction() as connection:
        row = await connection.fetch_one(
            "SELECT COUNT(*) AS total FROM guardrail_admission_state WHERE deployment_ref = ?",
            ("cold-admission-race",),
        )
    assert row == {"total": 1}
