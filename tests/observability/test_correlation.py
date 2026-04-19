"""Tests for correlation ID context var helpers."""

from __future__ import annotations

import asyncio

from zeroth.core.observability.correlation import (
    get_correlation_id,
    new_correlation_id,
    set_correlation_id,
)


def test_set_and_get_round_trip() -> None:
    cid = "test-cid-123"
    set_correlation_id(cid)
    assert get_correlation_id() == cid


def test_new_correlation_id_generates_unique_ids() -> None:
    id1 = new_correlation_id()
    id2 = new_correlation_id()
    assert id1 != id2
    assert len(id1) > 0


def test_new_correlation_id_sets_current() -> None:
    cid = new_correlation_id()
    assert get_correlation_id() == cid


async def _set_in_task(value: str) -> str:
    set_correlation_id(value)
    await asyncio.sleep(0)
    return get_correlation_id()


def test_correlation_id_isolated_across_tasks() -> None:
    async def _run():
        task_a = asyncio.create_task(_set_in_task("task-a"))
        task_b = asyncio.create_task(_set_in_task("task-b"))
        result_a, result_b = await asyncio.gather(task_a, task_b)
        # Each task keeps its own context; results match what was set.
        assert result_a == "task-a"
        assert result_b == "task-b"

    asyncio.run(_run())
