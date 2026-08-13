"""Atomic CAS persistence for token-engine snapshots."""

from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest
from cryptography.fernet import InvalidToken

from zeroth.contracts.graph import (
    CancellationFence,
    DispatchLifecycleState,
    InFlightDispatch,
    SchedulingState,
    TokenEngineSnapshot,
    TokenEngineSnapshotState,
    TokenEnvelope,
    TokenLifecycleState,
)
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.integrations.persistence.runs.token_snapshot_store import TokenSnapshotRowStore
from zeroth.platform.storage import NullWorkspaceScopeContext
from zeroth.platform.storage import EncryptedField
from zeroth.platform.storage.async_sqlite import AsyncSQLiteDatabase
from zeroth.runtime.orchestration.token_snapshot_store import (
    TokenSnapshotConcurrencyError,
    TokenSnapshotCorruptionError,
    TokenSnapshotStore,
    TokenSnapshotTransitionError,
    TokenSnapshotWriteDisabledError,
)
from zeroth.runtime.runs import Run
from zeroth.service.bootstrap.migrations import run_migrations
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
    repository = RunRepository(sqlite_db, NullWorkspaceScopeContext(tenant_id="tenant-1"))
    assert isinstance(repository, TokenSnapshotStore)


async def test_initial_create_and_read_round_trip(sqlite_db) -> None:
    repository = RunRepository(sqlite_db, NullWorkspaceScopeContext(tenant_id="tenant-1"))
    await repository.create(_run())
    snapshot = _snapshot(0)

    stored = await repository.compare_and_swap_token_snapshot(
        "run-1", expected_revision=None, snapshot=snapshot
    )

    assert stored == snapshot
    assert await repository.get_token_snapshot("run-1") == snapshot


async def test_successful_cas_replaces_one_coherent_snapshot(sqlite_db) -> None:
    repository = RunRepository(sqlite_db, NullWorkspaceScopeContext(tenant_id="tenant-1"))
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
    repository = RunRepository(sqlite_db, NullWorkspaceScopeContext(tenant_id="tenant-1"))
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
    repository = RunRepository(sqlite_db, NullWorkspaceScopeContext(tenant_id="tenant-1"))
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
    repository = RunRepository(sqlite_db, NullWorkspaceScopeContext(tenant_id="tenant-1"))
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


async def test_wrong_encryption_key_is_typed_corruption(tmp_path) -> None:
    path = tmp_path / "rotated-token-snapshot-key.db"
    run_migrations(f"sqlite:///{path}")
    writer_db = AsyncSQLiteDatabase(str(path), encryption_key=EncryptedField.generate_key())
    scope = NullWorkspaceScopeContext(tenant_id="tenant-1")
    writer = RunRepository(writer_db, scope)
    await writer.create(_run())
    await writer.compare_and_swap_token_snapshot(
        "run-1", expected_revision=None, snapshot=_snapshot(0)
    )
    await writer_db.close()

    reader_db = AsyncSQLiteDatabase(str(path), encryption_key=EncryptedField.generate_key())
    try:
        with pytest.raises(TokenSnapshotCorruptionError, match="cannot be decoded") as raised:
            await RunRepository(reader_db, scope).get_token_snapshot("run-1")
        assert isinstance(raised.value.__cause__, InvalidToken)
    finally:
        await reader_db.close()


async def test_cas_fails_loudly_when_row_metadata_contradicts_snapshot(sqlite_db) -> None:
    repository = RunRepository(sqlite_db, NullWorkspaceScopeContext(tenant_id="tenant-1"))
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


@pytest.mark.parametrize("field", ["revision", "schema_version", "next_token_ordinal"])
async def test_malformed_numeric_row_metadata_is_typed_corruption(sqlite_db, field: str) -> None:
    snapshot = _snapshot(0)
    row: dict[str, object] = {
        "run_id": snapshot.run_id,
        "revision": snapshot.revision,
        "schema_version": snapshot.schema_version,
        "next_token_ordinal": snapshot.next_token_ordinal,
        "snapshot_json": snapshot.model_dump_json(),
    }
    row[field] = "not-an-integer"

    with pytest.raises(TokenSnapshotCorruptionError, match=field):
        TokenSnapshotRowStore(
            sqlite_db, NullWorkspaceScopeContext.for_default_compatibility()
        )._decode_row(row)


@pytest.mark.parametrize("next_revision", [0, 2])
async def test_cas_rejects_revision_reuse_or_skip(sqlite_db, next_revision: int) -> None:
    repository = RunRepository(sqlite_db, NullWorkspaceScopeContext(tenant_id="tenant-1"))
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
    repository = RunRepository(sqlite_db, NullWorkspaceScopeContext(tenant_id="tenant-1"))
    await repository.create(_run())
    await repository.compare_and_swap_token_snapshot(
        "run-1", expected_revision=None, snapshot=_snapshot(0, ordinal=4)
    )

    with pytest.raises(TokenSnapshotTransitionError, match="next_token_ordinal"):
        await repository.compare_and_swap_token_snapshot(
            "run-1", expected_revision=0, snapshot=_snapshot(1, ordinal=3)
        )


def _snapshot_with_fence(
    revision: int,
    *,
    fence_generation: int,
    token_generation: int | None = None,
    acknowledgements: tuple[str, ...] = (),
    requested_revision: int | None = None,
) -> TokenEngineSnapshot:
    snapshot = _snapshot(revision)
    token = snapshot.tokens[0].model_copy(
        update={
            "cancellation_generation": (
                fence_generation if token_generation is None else token_generation
            )
        }
    )
    return snapshot.model_copy(
        update={
            "queue": (token,),
            "tokens": (token,),
            "cancellation_fence": CancellationFence(
                generation=fence_generation,
                requested_revision=(
                    None
                    if not fence_generation
                    else revision
                    if requested_revision is None
                    else requested_revision
                ),
                acknowledged_token_ids=acknowledgements,
                state_revision=revision,
            ),
        }
    )


async def test_cas_rejects_cancellation_generation_rollback(sqlite_db) -> None:
    repository = RunRepository(sqlite_db, NullWorkspaceScopeContext(tenant_id="tenant-1"))
    await repository.create(_run())
    await repository.compare_and_swap_token_snapshot(
        "run-1",
        expected_revision=None,
        snapshot=_snapshot_with_fence(0, fence_generation=1),
    )

    with pytest.raises(TokenSnapshotTransitionError, match="generation cannot decrease"):
        await repository.compare_and_swap_token_snapshot(
            "run-1",
            expected_revision=0,
            snapshot=_snapshot_with_fence(1, fence_generation=0),
        )


async def test_cas_rejects_cancellation_acknowledgement_regression(sqlite_db) -> None:
    repository = RunRepository(sqlite_db, NullWorkspaceScopeContext(tenant_id="tenant-1"))
    await repository.create(_run())
    await repository.compare_and_swap_token_snapshot(
        "run-1",
        expected_revision=None,
        snapshot=_snapshot_with_fence(
            0,
            fence_generation=1,
            acknowledgements=("retired-token",),
        ),
    )

    with pytest.raises(TokenSnapshotTransitionError, match="acknowledgements cannot regress"):
        await repository.compare_and_swap_token_snapshot(
            "run-1",
            expected_revision=0,
            snapshot=_snapshot_with_fence(
                1,
                fence_generation=1,
                requested_revision=0,
            ),
        )


async def test_cas_rejects_request_revision_change_within_generation(sqlite_db) -> None:
    repository = RunRepository(sqlite_db, NullWorkspaceScopeContext(tenant_id="tenant-1"))
    await repository.create(_run())
    await repository.compare_and_swap_token_snapshot(
        "run-1",
        expected_revision=None,
        snapshot=_snapshot_with_fence(0, fence_generation=1),
    )

    with pytest.raises(TokenSnapshotTransitionError, match="request metadata"):
        await repository.compare_and_swap_token_snapshot(
            "run-1",
            expected_revision=0,
            snapshot=_snapshot_with_fence(1, fence_generation=1),
        )


async def test_cas_rejects_terminal_snapshot_resurrection(sqlite_db) -> None:
    repository = RunRepository(sqlite_db, NullWorkspaceScopeContext(tenant_id="tenant-1"))
    await repository.create(_run())
    terminal = TokenEngineSnapshot(
        run_id="run-1",
        revision=0,
        state=TokenEngineSnapshotState.COMPLETED,
        next_token_ordinal=1,
    )
    await repository.compare_and_swap_token_snapshot(
        "run-1", expected_revision=None, snapshot=terminal
    )

    with pytest.raises(TokenSnapshotTransitionError, match="terminal snapshot state is absorbing"):
        await repository.compare_and_swap_token_snapshot(
            "run-1", expected_revision=0, snapshot=_snapshot(1)
        )


async def test_cas_rejects_live_token_from_stale_cancellation_generation(sqlite_db) -> None:
    repository = RunRepository(sqlite_db, NullWorkspaceScopeContext(tenant_id="tenant-1"))
    await repository.create(_run())

    with pytest.raises(TokenSnapshotTransitionError, match="current cancellation fence"):
        await repository.compare_and_swap_token_snapshot(
            "run-1",
            expected_revision=None,
            snapshot=_snapshot_with_fence(
                0,
                fence_generation=2,
                token_generation=1,
            ),
        )


def _executing_snapshot() -> TokenEngineSnapshot:
    token = TokenEnvelope(
        token_id="token-1",
        current_node_id="node-a",
        payload={},
        lifecycle_state=TokenLifecycleState.ACTIVE,
        scheduling_state=SchedulingState.EXECUTING,
        cancellation_generation=1,
        state_revision=0,
    )
    executing_dispatch = InFlightDispatch(
        dispatch_id="dispatch-1",
        idempotency_key="run-1:token-1:start",
        token=token,
        attempt=0,
        cancellation_generation=1,
        lifecycle_state=DispatchLifecycleState.EXECUTING,
        started_revision=0,
        updated_revision=0,
    )
    return TokenEngineSnapshot(
        run_id="run-1",
        revision=0,
        state=TokenEngineSnapshotState.RUNNING,
        next_token_ordinal=1,
        tokens=(token,),
        cancellation_fence=CancellationFence(
            generation=1,
            requested_revision=0,
            state_revision=0,
        ),
        in_flight_dispatches=(executing_dispatch,),
    )


async def test_cas_allows_executing_dispatch_with_newer_cancellation_request(sqlite_db) -> None:
    repository = RunRepository(sqlite_db, NullWorkspaceScopeContext(tenant_id="tenant-1"))
    await repository.create(_run())
    initial = _executing_snapshot()
    await repository.compare_and_swap_token_snapshot(
        "run-1", expected_revision=None, snapshot=initial
    )
    token = initial.tokens[0]
    executing_dispatch = initial.in_flight_dispatches[0]
    requested_dispatch = executing_dispatch.model_copy(
        update={
            "lifecycle_state": DispatchLifecycleState.CANCELLATION_REQUESTED,
            "cancellation_requested_generation": 2,
            "cancellation_requested_revision": 1,
            "updated_revision": 1,
        }
    )
    snapshot = TokenEngineSnapshot(
        run_id="run-1",
        revision=1,
        state=TokenEngineSnapshotState.RUNNING,
        next_token_ordinal=1,
        tokens=(token,),
        cancellation_fence=CancellationFence(
            generation=2,
            requested_revision=1,
            state_revision=1,
        ),
        in_flight_dispatches=(requested_dispatch,),
    )

    assert (
        await repository.compare_and_swap_token_snapshot(
            "run-1",
            expected_revision=0,
            snapshot=snapshot,
        )
        == snapshot
    )


def _contradictory_cancellation_request(
    initial: TokenEngineSnapshot,
) -> TokenEngineSnapshot:
    dispatch = initial.in_flight_dispatches[0].model_copy(
        update={
            "lifecycle_state": DispatchLifecycleState.CANCELLATION_REQUESTED,
            "cancellation_requested_generation": 2,
            "cancellation_requested_revision": 1,
            "updated_revision": 1,
        }
    )
    return initial.model_copy(
        update={
            "revision": 1,
            "cancellation_fence": CancellationFence(
                generation=1,
                requested_revision=0,
                state_revision=1,
            ),
            "in_flight_dispatches": (dispatch,),
        }
    )


async def test_cas_rejects_cancellation_request_newer_than_durable_fence(sqlite_db) -> None:
    repository = RunRepository(sqlite_db, NullWorkspaceScopeContext(tenant_id="tenant-1"))
    await repository.create(_run())
    initial = _executing_snapshot()
    await repository.compare_and_swap_token_snapshot(
        "run-1", expected_revision=None, snapshot=initial
    )

    with pytest.raises(TokenSnapshotTransitionError, match="cancellation-requested dispatch"):
        await repository.compare_and_swap_token_snapshot(
            "run-1",
            expected_revision=0,
            snapshot=_contradictory_cancellation_request(initial),
        )


async def test_read_wraps_cancellation_request_fence_contradiction_as_corruption(
    sqlite_db,
) -> None:
    repository = RunRepository(sqlite_db, NullWorkspaceScopeContext(tenant_id="tenant-1"))
    await repository.create(_run())
    initial = _executing_snapshot()
    await repository.compare_and_swap_token_snapshot(
        "run-1", expected_revision=None, snapshot=initial
    )
    contradictory = _contradictory_cancellation_request(initial)
    async with sqlite_db.transaction() as connection:
        await connection.execute(
            "UPDATE token_engine_snapshots SET revision = ?, snapshot_json = ? WHERE run_id = ?",
            (1, contradictory.model_dump_json(), "run-1"),
        )

    with pytest.raises(TokenSnapshotCorruptionError, match="cannot be decoded"):
        await repository.get_token_snapshot("run-1")


async def test_token_snapshot_write_is_rejected_after_durable_erasure_fence(sqlite_db) -> None:
    repository = RunRepository(sqlite_db, NullWorkspaceScopeContext(tenant_id="tenant-1"))
    await repository.create(_run())
    async with sqlite_db.transaction(write_lock=True) as connection:
        await repository.fence_and_erase_token_snapshot_for_run_in_transaction(
            connection,
            "run-1",
        )

    with pytest.raises(TokenSnapshotWriteDisabledError):
        await repository.compare_and_swap_token_snapshot(
            "run-1", expected_revision=None, snapshot=_snapshot(0)
        )
    assert await repository.get_token_snapshot("run-1") is None


async def test_deleting_and_recreating_run_resets_token_snapshot_erasure_fence(sqlite_db) -> None:
    repository = RunRepository(sqlite_db, NullWorkspaceScopeContext(tenant_id="tenant-1"))
    await repository.create(_run())
    async with sqlite_db.transaction(write_lock=True) as connection:
        await repository.fence_and_erase_token_snapshot_for_run_in_transaction(
            connection,
            "run-1",
        )
    await repository.delete("run-1")
    await repository.create(_run())

    assert await repository.compare_and_swap_token_snapshot(
        "run-1", expected_revision=None, snapshot=_snapshot(0)
    ) == _snapshot(0)


async def test_snapshot_survives_repository_reopen(tmp_path) -> None:
    from zeroth.platform.storage.async_sqlite import AsyncSQLiteDatabase
    from zeroth.service.bootstrap.migrations import run_migrations

    path = tmp_path / "reopen.db"
    run_migrations(f"sqlite:///{path}")
    first_db = AsyncSQLiteDatabase(str(path))
    first = RunRepository(first_db, NullWorkspaceScopeContext(tenant_id="tenant-1"))
    await first.create(_run())
    await first.compare_and_swap_token_snapshot(
        "run-1", expected_revision=None, snapshot=_snapshot(0)
    )
    await first_db.close()

    second_db = AsyncSQLiteDatabase(str(path))
    second = RunRepository(second_db, NullWorkspaceScopeContext(tenant_id="tenant-1"))
    try:
        assert await second.get_token_snapshot("run-1") == _snapshot(0)
    finally:
        await second_db.close()


async def test_deleting_run_cascades_snapshot(sqlite_db) -> None:
    repository = RunRepository(sqlite_db, NullWorkspaceScopeContext(tenant_id="tenant-1"))
    await repository.create(_run())
    await repository.compare_and_swap_token_snapshot(
        "run-1", expected_revision=None, snapshot=_snapshot(0)
    )

    await repository.delete("run-1")

    assert await repository.get_token_snapshot("run-1") is None


async def _assert_exactly_one_racing_cas_wins(database) -> None:
    first = RunRepository(database, NullWorkspaceScopeContext(tenant_id="tenant-1"))
    second = RunRepository(database, NullWorkspaceScopeContext(tenant_id="tenant-1"))
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
    repository = RunRepository(postgres_database, NullWorkspaceScopeContext(tenant_id="tenant-1"))
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
