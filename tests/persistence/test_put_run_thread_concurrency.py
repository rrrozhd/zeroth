"""ZER-49 F-03/F-12: two concurrent puts on one thread must keep both checkpoints.

``put_run`` reads the thread row, merges ``run_ids`` and ``checkpoint_refs`` into
that one snapshot, and writes it back. Consolidating the three writes into one
transaction removed the compensating second read-modify-write that used to run
after the checkpoint write, so the merge is now the only thing standing between
two concurrent puts and a lost update: both read the same thread, both append
their own checkpoint to it, and the later write erases the earlier one's
reference. The checkpoint row survives in ``run_checkpoints``; the thread no
longer points at it, and ``_checkpoint_ids`` / ``get_latest_checkpoint`` /
``_next_checkpoint_order`` all read the thread -- so a thread-level restore
resumes from a stale checkpoint and the next put reuses an order already taken.

The gate below is deliberately *timeout-bounded* rather than an
``asyncio.Barrier``. A barrier that insists both puts meet between the thread
read and the thread write is satisfiable only while the reads can overlap, which
is precisely what the fix forbids -- under a proper write lock the second put
cannot start until the first commits, and a hard barrier would deadlock the very
code it is meant to prove correct. Waiting *up to* ``_GATE_SECONDS`` reproduces
the interleaving whenever the isolation is missing and costs one short pause
when it is not.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest

from tests.conftest import requires_docker
from zeroth.integrations.persistence.runs import RunRepository, ThreadRepository
from zeroth.integrations.persistence.runs.run_repository import _RunThreadStore
from zeroth.runtime.runs import Run

DEPLOYMENT = "concurrency-deployment"
GRAPH_VERSION = "graph:v1"
THREAD_ID = "shared-thread"

# Comfortably under DEFAULT_COORDINATION_TIMEOUT_SECONDS (5.0): once the puts are
# serialized, the blocked one spends this long waiting for SQLite's write lock.
_GATE_SECONDS = 0.5

_CHECKPOINT_ORDERS = (
    "SELECT run_id, checkpoint_order FROM run_checkpoints WHERE thread_id = ? "
    "ORDER BY checkpoint_order"
)


class _ThreadWriteGate:
    """Hold each put between its thread read and its thread write.

    The last arrival releases everyone; an arrival that waits ``timeout`` without
    company proceeds alone. ``met`` records which of the two happened, so a
    failure message can say whether the interleaving was even reachable.
    """

    def __init__(self, parties: int, timeout: float) -> None:
        self._parties = parties
        self._timeout = timeout
        self._arrived = 0
        self._open = asyncio.Event()
        self.met = False

    async def wait(self) -> None:
        self._arrived += 1
        if self._arrived >= self._parties:
            self.met = True
            self._open.set()
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._open.wait(), self._timeout)


def _gate_the_thread_write(patching: pytest.MonkeyPatch, gate: _ThreadWriteGate) -> None:
    """Pause every put of the shared thread just before it writes the thread row."""
    original = _RunThreadStore._save_thread_bound

    async def _gated(self: Any, threads: Any, thread: Any, **kwargs: Any) -> None:
        if thread.thread_id == THREAD_ID:
            await gate.wait()
        await original(self, threads, thread, **kwargs)

    patching.setattr(_RunThreadStore, "_save_thread_bound", _gated)


def _new_run() -> Run:
    return Run(
        graph_version_ref=GRAPH_VERSION,
        deployment_ref=DEPLOYMENT,
        thread_id=THREAD_ID,
    )


async def _rows(db: Any, sql: str, value: str) -> list[dict[str, Any]]:
    async with db.transaction() as conn:
        return await conn.fetch_all(sql, (value,))


async def _prove_concurrent_puts_keep_both_checkpoints(db: Any) -> None:
    """Two runs of one thread, put concurrently: the thread must reference both."""
    repo = RunRepository.for_default_compatibility(db)
    threads = ThreadRepository.for_default_compatibility(db)
    first, second = _new_run(), _new_run()
    gate = _ThreadWriteGate(parties=2, timeout=_GATE_SECONDS)

    with pytest.MonkeyPatch.context() as patching:
        _gate_the_thread_write(patching, gate)
        await asyncio.gather(repo.put(first), repo.put(second))

    thread = await threads.get(THREAD_ID)
    assert thread is not None
    interleaved = "the puts overlapped" if gate.met else "the puts were serialized"

    assert len(thread.checkpoint_refs) == 2, (
        f"the thread references {len(thread.checkpoint_refs)} of 2 checkpoints "
        f"({interleaved}) -- the missing one is durable in run_checkpoints but "
        "invisible to every thread-level reader, so a restore resumes from a "
        "stale checkpoint"
    )

    orders = [int(row["checkpoint_order"]) for row in await _rows(db, _CHECKPOINT_ORDERS, THREAD_ID)]
    assert orders == [0, 1], (
        f"checkpoint_order values {orders} are not distinct and consecutive "
        f"({interleaved}) -- each put derived its order from the same thread snapshot"
    )

    assert sorted(thread.run_ids) == sorted([first.run_id, second.run_id]), (
        f"the thread lost a run_id ({interleaved}) -- the same unlocked "
        "read-modify-write drops run_ids too"
    )

    latest = await repo.get_latest_checkpoint(THREAD_ID)
    assert latest is not None
    assert latest.run_id in {first.run_id, second.run_id}
    assert len(await repo.list_checkpoints(THREAD_ID)) == 2


async def test_concurrent_puts_keep_both_checkpoints(async_database) -> None:
    await _prove_concurrent_puts_keep_both_checkpoints(async_database)


@requires_docker
async def test_concurrent_puts_keep_both_checkpoints_on_both_backends(dual_database) -> None:
    """Isolation is a backend property: prove it on SQLite *and* Postgres."""
    await _prove_concurrent_puts_keep_both_checkpoints(dual_database)
