"""Admission decision clocks are sampled after locking without another query."""
import asyncio
from datetime import UTC

from tests.conftest import requires_docker
from zeroth.platform.dispatch.lease import LeaseManager
from zeroth.platform.storage.async_postgres import PostgresConnection


@requires_docker
async def test_warm_empty_pending_claim_uses_three_reads(postgres_database, monkeypatch):
    manager = LeaseManager(postgres_database)
    scope = {'tenant_id': 'default', 'workspace_id': None}
    async with postgres_database.transaction(write_lock=True) as connection:
        await manager._lock_admission_scope(connection, 'clock-probe', **scope)
    statements = []
    original = PostgresConnection.fetch_one

    async def observed(self, sql, params=()):
        statements.append(sql)
        return await original(self, sql, params)

    monkeypatch.setattr(PostgresConnection, 'fetch_one', observed)
    result = await manager.claim_pending_result('clock-probe', 'worker', max_concurrency=1, **scope)
    assert result.run_id is None
    assert not result.concurrency_saturated
    assert result.active_count == 0
    assert len(statements) == 3, statements


@requires_docker
async def test_admission_clock_follows_lock_wait(postgres_database):
    manager = LeaseManager(postgres_database)
    scope = {'tenant_id': 'default', 'workspace_id': None}
    async with postgres_database.transaction(write_lock=True) as connection:
        await manager._lock_admission_scope(connection, 'clock-probe', **scope)
    entered = asyncio.Event()

    async def waiter():
        async with postgres_database.transaction(write_lock=True) as connection:
            entered.set()
            return await manager._lock_admission_scope(connection, 'clock-probe', sample_time=True, **scope)

    async with postgres_database.transaction(write_lock=True) as holder:
        await manager._lock_admission_scope(holder, 'clock-probe', **scope)
        task = asyncio.create_task(waiter())
        try:
            await entered.wait()
            await asyncio.sleep(.1)
            assert not task.done()
            marker = (await holder.fetch_one('SELECT clock_timestamp() AS t'))['t']
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
    sampled = await asyncio.wait_for(task, timeout=3)
    assert sampled >= marker
    assert sampled.tzinfo is UTC
