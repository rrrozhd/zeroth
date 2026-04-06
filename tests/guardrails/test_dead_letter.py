"""Tests for the dead-letter manager."""

from __future__ import annotations

from zeroth.guardrails.dead_letter import DeadLetterManager
from zeroth.runs import RunRepository, RunStatus
from zeroth.runs.models import Run
from zeroth.runs.repository import DEAD_LETTER_REASON

DEPLOYMENT = "dl-test-deployment"


async def _make_run(run_repo: RunRepository) -> Run:
    run = Run(graph_version_ref="g:v1", deployment_ref=DEPLOYMENT)
    persisted = await run_repo.create(run)
    # Transition to RUNNING so it can reach FAILED later.
    return await run_repo.transition(persisted.run_id, RunStatus.RUNNING)


async def test_dead_letter_not_triggered_below_threshold(sqlite_db) -> None:
    run_repo = RunRepository(sqlite_db)
    manager = DeadLetterManager(run_repository=run_repo, max_failure_count=3)

    run = await _make_run(run_repo)

    # Two failures -- not yet at threshold.
    for _ in range(2):
        dead_lettered = await manager.handle_run_failure(run.run_id)
        assert dead_lettered is False

    final = await run_repo.get(run.run_id)
    assert final is not None
    # Failure count incremented but not yet dead-lettered.
    assert final.failure_state is None or final.failure_state.reason != DEAD_LETTER_REASON


async def test_dead_letter_triggered_at_threshold(sqlite_db) -> None:
    run_repo = RunRepository(sqlite_db)
    manager = DeadLetterManager(run_repository=run_repo, max_failure_count=3)

    run = await _make_run(run_repo)

    # Three failures -- should dead-letter on the third.
    result = None
    for _ in range(3):
        result = await manager.handle_run_failure(run.run_id)

    assert result is True

    final = await run_repo.get(run.run_id)
    assert final is not None
    assert final.status is RunStatus.FAILED
    assert final.failure_state is not None
    assert final.failure_state.reason == DEAD_LETTER_REASON


async def test_dead_letter_metadata_captured(sqlite_db) -> None:
    run_repo = RunRepository(sqlite_db)
    manager = DeadLetterManager(run_repository=run_repo, max_failure_count=1)

    run = await _make_run(run_repo)
    await manager.handle_run_failure(run.run_id)

    final = await run_repo.get(run.run_id)
    assert final is not None
    assert final.failure_state is not None
    assert "dead_lettered_at" in (final.failure_state.details or {})


async def test_list_dead_letter_runs_returns_dead_lettered_runs(sqlite_db) -> None:
    run_repo = RunRepository(sqlite_db)
    manager = DeadLetterManager(run_repository=run_repo, max_failure_count=1)

    run1 = await _make_run(run_repo)
    run2 = await _make_run(run_repo)  # This one won't be dead-lettered.

    await manager.handle_run_failure(run1.run_id)

    dead_letters = await run_repo.list_dead_letter_runs(DEPLOYMENT)
    dl_ids = {r.run_id for r in dead_letters}
    assert run1.run_id in dl_ids
    assert run2.run_id not in dl_ids
