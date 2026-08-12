"""Characterization tests for the canonical run persistence adapter.

These pin the behaviour that must survive the move out of
``zeroth.core.runs.repository``: transaction ownership, status-transition
validation, row conversion through the repository, and the retention queries
that run inside a caller-supplied transaction.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from tests.conftest import requires_docker

from zeroth.platform.storage import (
    SERVICE_SCOPE_REGISTRY,
    NullWorkspaceScopeContext,
    ScopedTable,
)
from zeroth.integrations.persistence.runs import retention_queries
from zeroth.integrations.persistence.runs.run_repository import (
    ALLOWED_TRANSITIONS,
    DEAD_LETTER_REASON,
    RunRepository,
)
from zeroth.integrations.persistence.runs.thread_repository import ThreadRepository
from zeroth.platform.storage.async_sqlite import AsyncSQLiteDatabase
from zeroth.runtime.runs import Run, RunFailureState, RunHistoryEntry, RunStatus


def test_run_repository_imports_in_a_cold_interpreter() -> None:
    """The run adapter must import without ``zeroth.core`` warmed first."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from zeroth.integrations.persistence.runs import RunRepository",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"RunRepository is not cold-importable:\n{result.stderr}"


def _make_run(
    run_id: str = "run-1",
    *,
    thread_id: str = "thread-1",
    tenant_id: str = "tenant-1",
    status: RunStatus = RunStatus.PENDING,
) -> Run:
    return Run(
        run_id=run_id,
        workflow_name="demo",
        graph_version_ref="graph-1",
        deployment_ref="deployment-1",
        tenant_id=tenant_id,
        thread_id=thread_id,
        status=status,
    )


def _repository(database, tenant_id: str = "tenant-1") -> RunRepository:
    return RunRepository(database, NullWorkspaceScopeContext(tenant_id=tenant_id))


async def test_create_persists_a_run_and_reads_it_back(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """A created run round-trips through the database."""
    repository = _repository(sqlite_db)

    created = await repository.create(_make_run())

    assert created is not None
    assert created.run_id == "run-1"
    assert created.tenant_id == "tenant-1"
    assert (await repository.get("run-1")) is not None


async def test_list_runs_clamps_non_positive_limit(sqlite_db: AsyncSQLiteDatabase) -> None:
    """A02-12: list_runs is called beyond the FastAPI route layer's Query bound.

    SQLite's own ``LIMIT -1`` means "no limit" -- if a negative caller-supplied
    limit reached the query unclamped, this would return every row instead of
    being floored to a sane minimum.
    """
    repository = _repository(sqlite_db)
    for i in range(5):
        await repository.create(_make_run(f"run-{i}", tenant_id="tenant-1"))

    runs = await repository.list_runs("deployment-1", limit=-1)

    assert len(runs) < 5


async def test_list_runs_clamps_negative_offset(sqlite_db: AsyncSQLiteDatabase) -> None:
    """A negative offset must not raise and must behave like offset=0."""
    repository = _repository(sqlite_db)
    await repository.create(_make_run("run-x", tenant_id="tenant-1"))

    runs = await repository.list_runs("deployment-1", offset=-5, limit=10)

    assert len(runs) == 1


async def test_repository_exposes_its_database_for_coordinated_transactions(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """Services join the repository's transaction, so the database must be reachable."""
    repository = _repository(sqlite_db)

    assert repository.database is sqlite_db


async def test_get_hides_a_run_owned_by_another_tenant(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """A tenant-scoped read must not see another tenant's run."""
    repository = _repository(sqlite_db, "tenant-1")
    foreign = _repository(sqlite_db, "tenant-2")
    await repository.create(_make_run(tenant_id="tenant-1"))

    assert await repository.get("run-1") is not None
    assert await foreign.get("run-1") is None
    assert await repository.get("run-1") is not None


async def test_create_preserves_both_owners_when_tenants_share_a_run_id(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    repository = _repository(sqlite_db, "tenant-a")
    foreign_repository = _repository(sqlite_db, "tenant-b")
    owner = _make_run("guessed-run", tenant_id="tenant-a")
    owner.metadata = {"owner": "tenant-a"}
    await repository.create(owner)
    foreign = _make_run("guessed-run", tenant_id="tenant-b")
    foreign.metadata = {"owner": "tenant-b"}

    await foreign_repository.create(foreign)

    persisted = await repository.get("guessed-run")
    assert persisted is not None
    assert persisted.tenant_id == "tenant-a"
    assert persisted.metadata == {"owner": "tenant-a"}
    foreign_persisted = await foreign_repository.get("guessed-run")
    assert foreign_persisted is not None
    assert foreign_persisted.metadata == {"owner": "tenant-b"}


async def test_racing_tenants_can_create_the_same_run_id(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    first = _repository(sqlite_db, "tenant-a")
    second = _repository(sqlite_db, "tenant-b")
    contenders = [
        _make_run("raced-run", thread_id="thread-a", tenant_id="tenant-a"),
        _make_run("raced-run", thread_id="thread-b", tenant_id="tenant-b"),
    ]

    results = await asyncio.gather(
        first.create(contenders[0]),
        second.create(contenders[1]),
        return_exceptions=True,
    )

    assert all(isinstance(result, Run) for result in results)
    assert (await first.get("raced-run")).tenant_id == "tenant-a"
    assert (await second.get("raced-run")).tenant_id == "tenant-b"


async def test_concurrent_runs_on_one_thread_preserve_all_references_sqlite(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    repository = _repository(sqlite_db)
    runs = [_make_run(f"run-{index}", thread_id="shared-thread") for index in range(8)]

    await asyncio.gather(*(repository.create(run) for run in runs))

    thread = await ThreadRepository(sqlite_db, NullWorkspaceScopeContext(tenant_id="tenant-1")).get(
        "shared-thread"
    )
    assert thread is not None
    assert set(thread.run_ids) == {run.run_id for run in runs}
    assert set(thread.checkpoint_refs) == {run.checkpoint_id for run in runs}


@requires_docker
async def test_concurrent_runs_on_one_thread_preserve_all_references_postgres(
    postgres_database,
) -> None:
    repository = _repository(postgres_database, tenant_id="thread-lock-tenant")
    runs = [
        _make_run(f"pg-run-{index}", thread_id="pg-shared-thread", tenant_id="thread-lock-tenant")
        for index in range(12)
    ]

    await asyncio.gather(*(repository.create(run) for run in runs))

    thread = await ThreadRepository(
        postgres_database,
        NullWorkspaceScopeContext(tenant_id="thread-lock-tenant"),
    ).get("pg-shared-thread")
    assert thread is not None
    assert set(thread.run_ids) == {run.run_id for run in runs}
    assert set(thread.checkpoint_refs) == {run.checkpoint_id for run in runs}


async def test_replayed_same_run_id_is_refused_without_mutating_owner(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    repository = _repository(sqlite_db, "tenant-a")
    original = _make_run("replayed-run", tenant_id="tenant-a")
    original.metadata = {"request": "first"}
    await repository.create(original)

    replay = original.model_copy(deep=True)
    replay.metadata = {"request": "replayed"}
    with pytest.raises(KeyError, match="run already exists"):
        await repository.create(replay)

    persisted = await repository.get("replayed-run")
    assert persisted is not None
    assert persisted.metadata == {"request": "first"}


async def test_transition_records_the_new_status(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """A permitted transition persists the new status."""
    repository = _repository(sqlite_db)
    await repository.create(_make_run())

    transitioned = await repository.transition("run-1", RunStatus.RUNNING)

    assert transitioned.status is RunStatus.RUNNING


async def test_transition_rejects_a_move_out_of_a_terminal_status(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """COMPLETED is terminal: nothing may move a finished run back into flight."""
    repository = _repository(sqlite_db)
    await repository.create(_make_run())
    await repository.transition("run-1", RunStatus.RUNNING)
    await repository.transition("run-1", RunStatus.COMPLETED)

    with pytest.raises(ValueError, match="invalid run transition"):
        await repository.transition("run-1", RunStatus.RUNNING)


async def test_transition_raises_for_an_unknown_run(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """Transitioning a run that does not exist is a KeyError, not a silent no-op."""
    repository = _repository(sqlite_db)

    with pytest.raises(KeyError):
        await repository.transition("missing", RunStatus.RUNNING)


def test_allowed_transitions_keeps_completed_terminal_and_failed_replayable() -> None:
    """The transition table is a governance contract, not an implementation detail."""
    assert ALLOWED_TRANSITIONS[RunStatus.COMPLETED] == set()
    assert ALLOWED_TRANSITIONS[RunStatus.FAILED] == {RunStatus.PENDING}


async def test_record_history_appends_and_recomputes_completed_steps(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """Completed steps are derived from history, not tracked independently."""
    repository = _repository(sqlite_db)
    await repository.create(_make_run())

    updated = await repository.record_history(
        "run-1", RunHistoryEntry(node_id="node-a", status="completed")
    )

    assert [entry.node_id for entry in updated.execution_history] == ["node-a"]
    assert updated.completed_steps == ["node-a"]


async def test_count_pending_and_increment_failure_count(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """Backpressure and retry accounting read through the same adapter."""
    repository = _repository(sqlite_db)
    await repository.create(_make_run())

    assert await repository.count_pending("deployment-1") == 1
    assert await repository.increment_failure_count("run-1") == 1
    assert await repository.increment_failure_count("run-1") == 2


async def test_list_dead_letter_runs_selects_only_the_dead_letter_reason(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """A failed run is only dead-lettered when its failure carries the sentinel."""
    repository = _repository(sqlite_db)
    await repository.create(_make_run("run-dead", thread_id="thread-dead"))
    await repository.create(_make_run("run-plain", thread_id="thread-plain"))
    await repository.transition(
        "run-dead",
        RunStatus.FAILED,
        failure_state=RunFailureState(reason=DEAD_LETTER_REASON, message="gave up"),
    )
    await repository.transition(
        "run-plain",
        RunStatus.FAILED,
        failure_state=RunFailureState(reason="boom", message="one-off"),
    )

    dead_lettered = await repository.list_dead_letter_runs("deployment-1")

    assert [run.run_id for run in dead_lettered] == ["run-dead"]


async def test_erase_checkpoints_for_run_is_idempotent(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """Right-to-erasure reruns must not fail or double-count."""
    repository = _repository(sqlite_db)
    await repository.create(_make_run())

    first = await repository.erase_checkpoints_for_run("run-1")
    second = await repository.erase_checkpoints_for_run("run-1")

    assert first >= 1
    assert second == 0


async def test_redact_run_nulls_payloads_but_keeps_the_row(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """Redaction preserves chain continuity while removing the plaintext payloads."""
    repository = _repository(sqlite_db)
    run = _make_run()
    run.metadata = {"secret": "value"}
    await repository.create(run)

    assert await repository.redact_run("run-1") is True

    redacted = await repository.get("run-1")
    assert redacted is not None
    assert redacted.metadata == {}
    assert redacted.final_output is None


async def test_redact_run_reports_a_missing_run(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """Redacting a run that is already gone reports False rather than raising."""
    repository = _repository(sqlite_db)

    assert await repository.redact_run("missing") is False


async def test_erasure_payloads_expose_database_resident_values(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """The erasure verifier needs every payload that erasure is about to destroy."""
    repository = _repository(sqlite_db)
    run = _make_run()
    run.metadata = {"secret": "value"}
    await repository.create(run)

    async with repository.database.transaction() as connection:
        payloads = await repository.erasure_payloads_in_transaction(connection, "run-1")

    assert any("secret" in str(payload) for payload in payloads)


async def test_tenant_id_for_run_resolves_inside_a_caller_transaction(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """Retention resolves ownership without opening a second transaction."""
    repository = _repository(sqlite_db, "tenant-9")
    await repository.create(_make_run(tenant_id="tenant-9"))

    async with repository.database.transaction() as connection:
        resolved = await repository.tenant_id_for_run_in_transaction(connection, "run-1")
        missing = await repository.tenant_id_for_run_in_transaction(connection, "nope")

    assert resolved == "tenant-9"
    assert missing is None


async def test_list_erasable_run_ids_selects_only_stale_terminal_runs(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """Live work is never TTL-erasable regardless of age."""
    repository = _repository(sqlite_db)
    await repository.create(_make_run("run-done", thread_id="thread-done"))
    await repository.create(_make_run("run-live", thread_id="thread-live"))
    await repository.transition("run-done", RunStatus.RUNNING)
    await repository.transition("run-done", RunStatus.COMPLETED)
    await repository.transition("run-live", RunStatus.RUNNING)

    future = datetime.now(UTC) + timedelta(days=1)
    erasable = await repository.list_erasable_run_ids(future)

    assert erasable == ["run-done"]


async def test_list_erasable_run_ids_ignores_runs_newer_than_the_cutoff(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """A run updated after the cutoff is outside the TTL window."""
    repository = _repository(sqlite_db)
    await repository.create(_make_run())
    await repository.transition("run-1", RunStatus.RUNNING)
    await repository.transition("run-1", RunStatus.COMPLETED)

    past = datetime.now(UTC) - timedelta(days=1)

    assert await repository.list_erasable_run_ids(past) == []


async def test_lock_and_recheck_rejects_a_run_that_became_ineligible(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """Selection is an unlocked snapshot, so the destructive path re-verifies."""
    repository = _repository(sqlite_db)
    await repository.create(_make_run())
    await repository.transition("run-1", RunStatus.RUNNING)
    await repository.transition("run-1", RunStatus.COMPLETED)
    future = datetime.now(UTC) + timedelta(days=1)

    async with repository.database.transaction() as connection:
        eligible = await repository.lock_and_recheck_erasable_run(connection, "run-1", future)
        too_fresh = await repository.lock_and_recheck_erasable_run(
            connection, "run-1", datetime.now(UTC) - timedelta(days=1)
        )
        missing = await repository.lock_and_recheck_erasable_run(connection, "absent", future)

    assert eligible == "run-1"
    assert too_fresh is None
    assert missing is None


async def test_retention_queries_run_against_a_supplied_connection(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """The extracted queries own no transaction; the caller supplies one.

    This is what lets retention erase runs, checkpoints, and its own audit log
    inside a single atomic transaction.
    """
    repository = _repository(sqlite_db)
    await repository.create(_make_run())
    context = NullWorkspaceScopeContext(tenant_id="tenant-1")
    runs = ScopedTable(sqlite_db, SERVICE_SCOPE_REGISTRY, "service.runs", context)
    checkpoints = ScopedTable(sqlite_db, SERVICE_SCOPE_REGISTRY, "service.run_checkpoints", context)

    async with sqlite_db.transaction() as connection:
        bound_runs = runs.in_transaction(connection)
        bound_checkpoints = checkpoints.in_transaction(connection)
        assert await retention_queries.tenant_id_for_run(bound_runs, "run-1") == "tenant-1"
        assert await retention_queries.redact_run(bound_runs, "run-1") is True
        assert await retention_queries.erase_checkpoints_for_run(bound_checkpoints, "run-1") >= 1
        assert await retention_queries.erase_checkpoints_for_run(bound_checkpoints, "run-1") == 0
