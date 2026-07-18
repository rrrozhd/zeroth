"""Characterization tests for the canonical run persistence adapter.

These pin the behaviour that must survive the move out of
``zeroth.core.runs.repository``: transaction ownership, status-transition
validation, row conversion through the repository, and the retention queries
that run inside a caller-supplied transaction.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from zeroth.core.storage.async_sqlite import AsyncSQLiteDatabase
from zeroth.integrations.persistence.runs import retention_queries
from zeroth.integrations.persistence.runs.run_repository import (
    ALLOWED_TRANSITIONS,
    DEAD_LETTER_REASON,
    RunRepository,
)
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


async def test_create_persists_a_run_and_reads_it_back(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """A created run round-trips through the database."""
    repository = RunRepository(sqlite_db)

    created = await repository.create(_make_run())

    assert created is not None
    assert created.run_id == "run-1"
    assert created.tenant_id == "tenant-1"
    assert (await repository.get("run-1")) is not None


async def test_repository_exposes_its_database_for_coordinated_transactions(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """Services join the repository's transaction, so the database must be reachable."""
    repository = RunRepository(sqlite_db)

    assert repository.database is sqlite_db


async def test_get_hides_a_run_owned_by_another_tenant(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """A tenant-scoped read must not see another tenant's run."""
    repository = RunRepository(sqlite_db)
    await repository.create(_make_run(tenant_id="tenant-1"))

    assert await repository.get("run-1", tenant_id="tenant-1") is not None
    assert await repository.get("run-1", tenant_id="tenant-2") is None
    assert await repository.get("run-1") is not None


async def test_transition_records_the_new_status(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """A permitted transition persists the new status."""
    repository = RunRepository(sqlite_db)
    await repository.create(_make_run())

    transitioned = await repository.transition("run-1", RunStatus.RUNNING)

    assert transitioned.status is RunStatus.RUNNING


async def test_transition_rejects_a_move_out_of_a_terminal_status(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """COMPLETED is terminal: nothing may move a finished run back into flight."""
    repository = RunRepository(sqlite_db)
    await repository.create(_make_run())
    await repository.transition("run-1", RunStatus.RUNNING)
    await repository.transition("run-1", RunStatus.COMPLETED)

    with pytest.raises(ValueError, match="invalid run transition"):
        await repository.transition("run-1", RunStatus.RUNNING)


async def test_transition_raises_for_an_unknown_run(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """Transitioning a run that does not exist is a KeyError, not a silent no-op."""
    repository = RunRepository(sqlite_db)

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
    repository = RunRepository(sqlite_db)
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
    repository = RunRepository(sqlite_db)
    await repository.create(_make_run())

    assert await repository.count_pending("deployment-1") == 1
    assert await repository.increment_failure_count("run-1") == 1
    assert await repository.increment_failure_count("run-1") == 2


async def test_list_dead_letter_runs_selects_only_the_dead_letter_reason(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """A failed run is only dead-lettered when its failure carries the sentinel."""
    repository = RunRepository(sqlite_db)
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
    repository = RunRepository(sqlite_db)
    await repository.create(_make_run())

    first = await repository.erase_checkpoints_for_run("run-1")
    second = await repository.erase_checkpoints_for_run("run-1")

    assert first >= 1
    assert second == 0


async def test_redact_run_nulls_payloads_but_keeps_the_row(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """Redaction preserves chain continuity while removing the plaintext payloads."""
    repository = RunRepository(sqlite_db)
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
    repository = RunRepository(sqlite_db)

    assert await repository.redact_run("missing") is False


async def test_erasure_payloads_expose_database_resident_values(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """The erasure verifier needs every payload that erasure is about to destroy."""
    repository = RunRepository(sqlite_db)
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
    repository = RunRepository(sqlite_db)
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
    repository = RunRepository(sqlite_db)
    await repository.create(_make_run("run-done", thread_id="thread-done"))
    await repository.create(_make_run("run-live", thread_id="thread-live"))
    await repository.transition("run-done", RunStatus.RUNNING)
    await repository.transition("run-done", RunStatus.COMPLETED)
    await repository.transition("run-live", RunStatus.RUNNING)

    future = datetime.now(UTC) + timedelta(days=1)
    erasable = await repository.list_erasable_run_ids("tenant-1", future)

    assert erasable == ["run-done"]


async def test_list_erasable_run_ids_ignores_runs_newer_than_the_cutoff(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """A run updated after the cutoff is outside the TTL window."""
    repository = RunRepository(sqlite_db)
    await repository.create(_make_run())
    await repository.transition("run-1", RunStatus.RUNNING)
    await repository.transition("run-1", RunStatus.COMPLETED)

    past = datetime.now(UTC) - timedelta(days=1)

    assert await repository.list_erasable_run_ids("tenant-1", past) == []


async def test_lock_and_recheck_rejects_a_run_that_became_ineligible(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """Selection is an unlocked snapshot, so the destructive path re-verifies."""
    repository = RunRepository(sqlite_db)
    await repository.create(_make_run())
    await repository.transition("run-1", RunStatus.RUNNING)
    await repository.transition("run-1", RunStatus.COMPLETED)
    future = datetime.now(UTC) + timedelta(days=1)

    async with repository.database.transaction() as connection:
        eligible = await repository.lock_and_recheck_erasable_run(
            connection, "run-1", "tenant-1", future
        )
        wrong_tenant = await repository.lock_and_recheck_erasable_run(
            connection, "run-1", "tenant-other", future
        )
        too_fresh = await repository.lock_and_recheck_erasable_run(
            connection, "run-1", "tenant-1", datetime.now(UTC) - timedelta(days=1)
        )
        missing = await repository.lock_and_recheck_erasable_run(
            connection, "absent", "tenant-1", future
        )

    assert eligible == "run-1"
    assert wrong_tenant is None
    assert too_fresh is None
    assert missing is None


async def test_retention_queries_run_against_a_supplied_connection(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """The extracted queries own no transaction; the caller supplies one.

    This is what lets retention erase runs, checkpoints, and its own audit log
    inside a single atomic transaction.
    """
    repository = RunRepository(sqlite_db)
    await repository.create(_make_run())

    async with sqlite_db.transaction() as connection:
        assert await retention_queries.tenant_id_for_run(connection, "run-1") == "tenant-1"
        assert await retention_queries.redact_run(connection, "run-1") is True
        assert await retention_queries.erase_checkpoints_for_run(connection, "run-1") >= 1
        assert await retention_queries.erase_checkpoints_for_run(connection, "run-1") == 0
