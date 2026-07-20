"""Atomic CAS persistence for token-engine snapshots."""

from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

from zeroth.contracts.graph import (
    CancellationFence,
    SchedulingState,
    TokenEngineSnapshot,
    TokenEngineSnapshotState,
    TokenEnvelope,
    TokenLifecycleState,
)
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.runtime.orchestration.token_snapshot_store import (
    TokenSnapshotConcurrencyError,
    TokenSnapshotCorruptionError,
    TokenSnapshotStore,
    TokenSnapshotTransitionError,
)
from zeroth.runtime.runs import Run
from tests.conftest import requires_docker


def _run(run_id: str = "run-1") -> Run:
    return Run(
        run_id=run_id,
        workflow_name="snapshot-test",
        graph_version_ref="graph-1",
        deployment_ref="deployment-1",
        tenant_id="tenant-1",
        thread_id=f"thread-{run_id}",
    )


def _snapshot(revision: int, *, ordinal: int | None = None) -> TokenEngineSnapshot:
    token = TokenEnvelope(
        token_id="token-1",
        current_node_id="node-a",
        payload={"revision": revision},
        lifecycle_state=TokenLifecycleState.ACTIVE,
        scheduling_state=SchedulingState.QUEUED,
        state_revision=revision,
    )
    return TokenEngineSnapshot(
        schema_version=1,
        run_id="run-1",
        revision=revision,
        state=TokenEngineSnapshotState.RUNNING,
        next_token_ordinal=revision + 1 if ordinal is None else ordinal,
        queue=(token,),
        tokens=(token,),
        cancellation_fence=CancellationFence(generation=0, state_revision=revision),
    )


def test_runtime_protocol_import_does_not_load_integrations() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import zeroth.runtime.orchestration.token_snapshot_store; "
            "assert not any(name.startswith('zeroth.integrations') for name in sys.modules)",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


async def test_repository_structurally_satisfies_runtime_protocol(sqlite_db) -> None:
    repository = RunRepository(sqlite_db)
    assert isinstance(repository, TokenSnapshotStore)


async def test_initial_create_and_read_round_trip(sqlite_db) -> None:
    repository = RunRepository(sqlite_db)
    await repository.create(_run())
    snapshot = _snapshot(0)

    stored = await repository.compare_and_swap_token_snapshot(
        "run-1", expected_revision=None, snapshot=snapshot
    )

    assert stored == snapshot
    assert await repository.get_token_snapshot("run-1") == snapshot


async def test_successful_cas_replaces_one_coherent_snapshot(sqlite_db) -> None:
    repository = RunRepository(sqlite_db)
    await repository.create(_run())
    await repository.compare_and_swap_token_snapshot(
        "run-1", expected_revision=None, snapshot=_snapshot(0)
    )

    updated = await repository.compare_and_swap_token_snapshot(
        "run-1", expected_revision=0, snapshot=_snapshot(1)
    )

    assert updated.revision == 1
    assert (await repository.get_token_snapshot("run-1")).revision == 1


async def test_stale_cas_is_typed_and_makes_no_partial_write(sqlite_db) -> None:
    repository = RunRepository(sqlite_db)
    await repository.create(_run())
    original = _snapshot(0)
    await repository.compare_and_swap_token_snapshot(
        "run-1", expected_revision=None, snapshot=original
    )

    with pytest.raises(TokenSnapshotConcurrencyError) as raised:
        await repository.compare_and_swap_token_snapshot(
            "run-1", expected_revision=9, snapshot=_snapshot(10)
        )

    assert raised.value.expected_revision == 9
    assert raised.value.actual_revision == 0
    assert await repository.get_token_snapshot("run-1") == original


async def test_read_fails_loudly_when_row_metadata_contradicts_snapshot(sqlite_db) -> None:
    repository = RunRepository(sqlite_db)
    await repository.create(_run())
    await repository.compare_and_swap_token_snapshot(
        "run-1", expected_revision=None, snapshot=_snapshot(0)
    )
    async with sqlite_db.transaction() as connection:
        await connection.execute(
            "UPDATE token_engine_snapshots SET revision = 7 WHERE run_id = ?",
            ("run-1",),
        )

    with pytest.raises(TokenSnapshotCorruptionError, match="revision"):
        await repository.get_token_snapshot("run-1")


async def test_read_wraps_malformed_snapshot_payload_as_typed_corruption(sqlite_db) -> None:
    repository = RunRepository(sqlite_db)
    await repository.create(_run())
    await repository.compare_and_swap_token_snapshot(
        "run-1", expected_revision=None, snapshot=_snapshot(0)
    )
    async with sqlite_db.transaction() as connection:
        await connection.execute(
            "UPDATE token_engine_snapshots SET snapshot_json = ? WHERE run_id = ?",
            ("not-json", "run-1"),
        )

    with pytest.raises(TokenSnapshotCorruptionError, match="cannot be decoded"):
        await repository.get_token_snapshot("run-1")


async def test_cas_fails_loudly_when_row_metadata_contradicts_snapshot(sqlite_db) -> None:
    repository = RunRepository(sqlite_db)
    await repository.create(_run())
    await repository.compare_and_swap_token_snapshot(
        "run-1", expected_revision=None, snapshot=_snapshot(0)
    )
    async with sqlite_db.transaction() as connection:
        await connection.execute(
            "UPDATE token_engine_snapshots SET next_token_ordinal = 99 WHERE run_id = ?",
            ("run-1",),
        )

    with pytest.raises(TokenSnapshotCorruptionError, match="next_token_ordinal"):
        await repository.compare_and_swap_token_snapshot(
            "run-1", expected_revision=0, snapshot=_snapshot(1, ordinal=100)
        )


@pytest.mark.parametrize("next_revision", [0, 2])
async def test_cas_rejects_revision_reuse_or_skip(sqlite_db, next_revision: int) -> None:
    repository = RunRepository(sqlite_db)
    await repository.create(_run())
    await repository.compare_and_swap_token_snapshot(
        "run-1", expected_revision=None, snapshot=_snapshot(0)
    )

    with pytest.raises(TokenSnapshotTransitionError, match="exactly one"):
        await repository.compare_and_swap_token_snapshot(
            "run-1", expected_revision=0, snapshot=_snapshot(next_revision)
        )

    assert (await repository.get_token_snapshot("run-1")).revision == 0


async def test_cas_rejects_backward_next_token_ordinal(sqlite_db) -> None:
    repository = RunRepository(sqlite_db)
    await repository.create(_run())
    await repository.compare_and_swap_token_snapshot(
        "run-1", expected_revision=None, snapshot=_snapshot(0, ordinal=4)
    )

    with pytest.raises(TokenSnapshotTransitionError, match="next_token_ordinal"):
        await repository.compare_and_swap_token_snapshot(
            "run-1", expected_revision=0, snapshot=_snapshot(1, ordinal=3)
        )


async def test_snapshot_survives_repository_reopen(tmp_path) -> None:
    from zeroth.platform.storage.async_sqlite import AsyncSQLiteDatabase
    from zeroth.service.bootstrap.migrations import run_migrations

    path = tmp_path / "reopen.db"
    run_migrations(f"sqlite:///{path}")
    first_db = AsyncSQLiteDatabase(str(path))
    first = RunRepository(first_db)
    await first.create(_run())
    await first.compare_and_swap_token_snapshot(
        "run-1", expected_revision=None, snapshot=_snapshot(0)
    )
    await first_db.close()

    second_db = AsyncSQLiteDatabase(str(path))
    second = RunRepository(second_db)
    try:
        assert await second.get_token_snapshot("run-1") == _snapshot(0)
    finally:
        await second_db.close()


async def test_deleting_run_cascades_snapshot(sqlite_db) -> None:
    repository = RunRepository(sqlite_db)
    await repository.create(_run())
    await repository.compare_and_swap_token_snapshot(
        "run-1", expected_revision=None, snapshot=_snapshot(0)
    )

    await repository.delete("run-1")

    assert await repository.get_token_snapshot("run-1") is None


async def _assert_exactly_one_racing_cas_wins(database) -> None:
    first = RunRepository(database)
    second = RunRepository(database)
    await first.create(_run())
    await first.compare_and_swap_token_snapshot(
        "run-1", expected_revision=None, snapshot=_snapshot(0)
    )

    results = await asyncio.gather(
        first.compare_and_swap_token_snapshot(
            "run-1", expected_revision=0, snapshot=_snapshot(1, ordinal=2)
        ),
        second.compare_and_swap_token_snapshot(
            "run-1", expected_revision=0, snapshot=_snapshot(1, ordinal=3)
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(item, TokenEngineSnapshot) for item in results) == 1
    assert sum(isinstance(item, TokenSnapshotConcurrencyError) for item in results) == 1
    winner = next(item for item in results if isinstance(item, TokenEngineSnapshot))
    assert await first.get_token_snapshot("run-1") == winner


async def test_two_sqlite_writers_publish_exactly_one_cas_winner(sqlite_db) -> None:
    await _assert_exactly_one_racing_cas_wins(sqlite_db)


@requires_docker
async def test_compare_and_swap_runs_on_postgres(postgres_database) -> None:
    repository = RunRepository(postgres_database)
    await repository.create(_run())
    await repository.compare_and_swap_token_snapshot(
        "run-1", expected_revision=None, snapshot=_snapshot(0)
    )

    updated = await repository.compare_and_swap_token_snapshot(
        "run-1", expected_revision=0, snapshot=_snapshot(1)
    )

    assert updated.revision == 1
    assert await repository.get_token_snapshot("run-1") == updated


@requires_docker
async def test_two_postgres_writers_publish_exactly_one_cas_winner(postgres_database) -> None:
    await _assert_exactly_one_racing_cas_wins(postgres_database)
