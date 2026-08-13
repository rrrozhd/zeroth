"""ZER-49 AC3 / A07-16: ``put_run`` must land its three writes atomically.

``put_run`` used to write the thread row, the checkpoint row and the runs row in
three separate transactions. A failure of the last one therefore left the first
two durable: a thread and a checkpoint recording a state the runs row never
reached, so a restore replayed work the run had not committed.

The probe replaces ``_RunThreadStore._save_run_bound`` -- the single method both
put paths use for the runs-row write, and the last of the three. It stands in
for anything that can fail there: a fence rejection, a constraint violation, a
dropped connection, a killed process. The assertions never look at the probe;
they look at what stayed on disk.

``created_at`` is deliberately absent from the checkpoint upsert's update
columns, so only a put that *inserts* its checkpoint row proves anything about
that column -- hence the insert path in the timestamp-ordering guard.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from tests.conftest import requires_docker
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.integrations.persistence.runs.run_repository import _RunThreadStore
from zeroth.runtime.runs import Run

DEPLOYMENT = "atomicity-deployment"
GRAPH_VERSION = "graph:v1"

_COUNT_RUN = "SELECT COUNT(*) AS n FROM runs WHERE run_id = ?"
_COUNT_THREAD = "SELECT COUNT(*) AS n FROM threads WHERE thread_id = ?"
_COUNT_CHECKPOINTS = "SELECT COUNT(*) AS n FROM run_checkpoints WHERE run_id = ?"
_THREAD_UPDATED_AT = "SELECT updated_at AS value FROM threads WHERE thread_id = ?"
_RUN_UPDATED_AT = "SELECT updated_at AS value FROM runs WHERE run_id = ?"
_CHECKPOINT_CREATED_AT = "SELECT created_at AS value FROM run_checkpoints WHERE run_id = ?"


class _InjectedWriteError(RuntimeError):
    """Stands in for any failure of the runs-row write."""


def _fail_the_runs_row_write(patching: pytest.MonkeyPatch) -> None:
    """Make the third and last of put's writes fail, leaving the first two done."""

    async def _boom(self: Any, runs: Any, run: Run, fence: Any, **kwargs: Any) -> None:
        raise _InjectedWriteError(run.run_id)

    patching.setattr(_RunThreadStore, "_save_run_bound", _boom)


async def _count(db: Any, sql: str, value: str) -> int:
    async with db.transaction() as conn:
        row = await conn.fetch_one(sql, (value,))
    return int(row["n"])


async def _cell(db: Any, sql: str, value: str) -> Any:
    async with db.transaction() as conn:
        row = await conn.fetch_one(sql, (value,))
    assert row is not None, f"no row for {value}"
    return row["value"]


def _new_run() -> Run:
    return Run(graph_version_ref=GRAPH_VERSION, deployment_ref=DEPLOYMENT)


async def _prove_a_failed_first_put_leaves_no_thread_or_checkpoint(db: Any) -> None:
    """A put that inserts a run must leave nothing behind when its last write fails."""
    repo = RunRepository.for_default_compatibility(db)
    run = _new_run()

    with pytest.MonkeyPatch.context() as patching:
        _fail_the_runs_row_write(patching)
        with pytest.raises(_InjectedWriteError):
            await repo.put(run)

    assert await _count(db, _COUNT_RUN, run.run_id) == 0
    assert await _count(db, _COUNT_THREAD, run.thread_id) == 0, (
        "the thread row outlived the failed put"
    )
    assert await _count(db, _COUNT_CHECKPOINTS, run.run_id) == 0, (
        "a checkpoint survived for a run that was never written"
    )


async def _prove_a_failed_put_does_not_advance_the_checkpoint(db: Any) -> None:
    """A failed update must leave the checkpoint and the thread exactly as they were."""
    repo = RunRepository.for_default_compatibility(db)
    created = await repo.create(_new_run())
    checkpoint_id = created.checkpoint_id
    assert checkpoint_id is not None

    committed = await repo.get(created.run_id)
    committed.metadata["stage"] = "committed"
    await repo.put(committed)

    before_checkpoint = await repo.get_checkpoint(checkpoint_id)
    assert before_checkpoint.metadata["stage"] == "committed"
    before_thread = await _cell(db, _THREAD_UPDATED_AT, created.thread_id)

    doomed = await repo.get(created.run_id)
    doomed.metadata["stage"] = "doomed"
    with pytest.MonkeyPatch.context() as patching:
        _fail_the_runs_row_write(patching)
        with pytest.raises(_InjectedWriteError):
            await repo.put(doomed)

    after_run = await repo.get(created.run_id)
    after_checkpoint = await repo.get_checkpoint(checkpoint_id)
    after_thread = await _cell(db, _THREAD_UPDATED_AT, created.thread_id)

    assert after_run.metadata["stage"] == "committed"
    assert after_checkpoint.metadata["stage"] == "committed", (
        "the checkpoint advanced past the runs row -- a restore would replay "
        "state the run never committed"
    )
    assert after_checkpoint.updated_at <= after_run.updated_at, (
        "the durable checkpoint is newer than the runs row it belongs to"
    )
    assert after_thread == before_thread, "the thread row recorded a put that never landed"


async def _prove_a_successful_put_keeps_the_checkpoint_older_than_the_run(db: Any) -> None:
    """The checkpoint is written *before* the runs row, and its timestamp says so.

    Unlike the two proofs above this one holds before the atomicity fix as well:
    it is the regression guard for it. Making the three writes share one
    transaction must not collapse the two ``touch()`` points into one, because
    the ordering of ``run_checkpoints.created_at`` against ``runs.updated_at``
    is what tells a reader which of the two was durable first.
    """
    repo = RunRepository.for_default_compatibility(db)
    run = _new_run()

    await repo.put(run)

    assert await repo.get(run.run_id) is not None
    checkpoint_created_at = datetime.fromisoformat(
        await _cell(db, _CHECKPOINT_CREATED_AT, run.run_id)
    )
    run_updated_at = datetime.fromisoformat(await _cell(db, _RUN_UPDATED_AT, run.run_id))

    assert checkpoint_created_at < run_updated_at, (
        "the checkpoint must be strictly older than the runs row it precedes"
    )


async def test_a_failed_first_put_leaves_no_thread_or_checkpoint(async_database) -> None:
    await _prove_a_failed_first_put_leaves_no_thread_or_checkpoint(async_database)


async def test_a_failed_put_does_not_advance_the_checkpoint(async_database) -> None:
    await _prove_a_failed_put_does_not_advance_the_checkpoint(async_database)


async def test_a_successful_put_keeps_the_checkpoint_older_than_the_run(async_database) -> None:
    await _prove_a_successful_put_keeps_the_checkpoint_older_than_the_run(async_database)


@requires_docker
async def test_put_atomicity_holds_on_both_backends(dual_database) -> None:
    """Atomicity is a backend property: prove it on SQLite *and* Postgres."""
    await _prove_a_failed_first_put_leaves_no_thread_or_checkpoint(dual_database)
    await _prove_a_failed_put_does_not_advance_the_checkpoint(dual_database)
    await _prove_a_successful_put_keeps_the_checkpoint_older_than_the_run(dual_database)
