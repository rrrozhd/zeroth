"""Standalone checkpoint writes preserve thread references and roll back together."""

import asyncio

import pytest

from tests.conftest import requires_docker
from tests.persistence.test_put_run_thread_concurrency import (
    _ThreadWriteGate, _gate_the_thread_write,
)
from zeroth.integrations.persistence.runs import RunRepository, ThreadRepository
from zeroth.integrations.persistence.runs.run_repository import _RunThreadStore
from zeroth.runtime.runs import Run


@requires_docker
async def test_checkpoint_and_thread_update_roll_back_together(dual_database, monkeypatch):
    repo = RunRepository.for_default_compatibility(dual_database)
    run = await repo.create(Run(graph_version_ref='checkpoint:v1', deployment_ref='checkpoint'))
    original_id = run.checkpoint_id
    run.checkpoint_id = None
    error = RuntimeError('injected thread write failure')

    async def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(_RunThreadStore, '_save_thread_bound', fail)
    with pytest.raises(RuntimeError) as caught:
        await repo.write_checkpoint(run)
    assert caught.value is error
    assert await repo.get_checkpoint(run.checkpoint_id) is None
    thread = await ThreadRepository.for_default_compatibility(dual_database).get(run.thread_id)
    assert thread.checkpoint_refs == [original_id]


@requires_docker
async def test_concurrent_checkpoint_writers_keep_references_and_order(dual_database, monkeypatch):
    repo = RunRepository.for_default_compatibility(dual_database)
    first = await repo.create(Run(graph_version_ref='checkpoint:v1', deployment_ref='checkpoint', thread_id='shared-thread'))
    second = await repo.create(Run(graph_version_ref='checkpoint:v1', deployment_ref='checkpoint', thread_id=first.thread_id))
    threads = ThreadRepository.for_default_compatibility(dual_database)
    initial = list((await threads.get(first.thread_id)).checkpoint_refs)
    first.checkpoint_id = second.checkpoint_id = None
    gate = _ThreadWriteGate(parties=2, timeout=.2)
    _gate_the_thread_write(monkeypatch, gate)
    ids = await asyncio.gather(repo.write_checkpoint(first), repo.write_checkpoint(second))
    thread = await threads.get(first.thread_id)
    assert set(thread.checkpoint_refs) == set(initial + ids)
    async with dual_database.transaction() as connection:
        rows = await connection.fetch_all('SELECT checkpoint_order FROM run_checkpoints WHERE thread_id = ? ORDER BY checkpoint_order', (first.thread_id,))
    assert [int(row['checkpoint_order']) for row in rows] == [0, 1, 2, 3]


@requires_docker
async def test_missing_thread_seed_race_registers_both_runs(postgres_database, monkeypatch):
    from zeroth.platform.storage.scoped_table import BoundStructuredTable

    repo = RunRepository.for_default_compatibility(postgres_database)
    first = Run(graph_version_ref='checkpoint:v1', deployment_ref='checkpoint', thread_id='missing-race')
    second = Run(graph_version_ref='checkpoint:v1', deployment_ref='checkpoint', thread_id=first.thread_id)
    original = BoundStructuredTable.select_one
    gate = _ThreadWriteGate(parties=2, timeout=1)

    async def select(self, *args, **kwargs):
        row = await original(self, *args, **kwargs)
        if row is None and kwargs.get('where') == {'thread_id': first.thread_id}:
            await gate.wait()
        return row

    monkeypatch.setattr(BoundStructuredTable, 'select_one', select)
    ids = await asyncio.wait_for(asyncio.gather(repo.write_checkpoint(first), repo.write_checkpoint(second)), 5)
    assert gate.met, 'both writers must observe the missing thread before insertion'
    thread = await ThreadRepository.for_default_compatibility(postgres_database).get(first.thread_id)
    assert set(thread.run_ids) == {first.run_id, second.run_id}
    assert set(thread.checkpoint_refs) == set(ids)


@requires_docker
async def test_failed_checkpoint_rolls_back_new_thread_seed(dual_database, monkeypatch):
    repo = RunRepository.for_default_compatibility(dual_database)
    run = Run(graph_version_ref='checkpoint:v1', deployment_ref='checkpoint')
    checkpoints = repo._store.checkpoints
    original = type(checkpoints).write_row_bound
    error = RuntimeError('injected failure after checkpoint write')

    async def fail(self, *args, **kwargs):
        await original(self, *args, **kwargs)
        raise error

    monkeypatch.setattr(type(checkpoints), 'write_row_bound', fail)
    with pytest.raises(RuntimeError) as caught:
        await repo.write_checkpoint(run)
    assert caught.value is error
    assert await repo.get_checkpoint(run.checkpoint_id) is None
    assert await ThreadRepository.for_default_compatibility(dual_database).get(run.thread_id) is None
