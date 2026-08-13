"""Characterization tests for the ``run_checkpoints`` table adapter.

``zeroth.integrations.persistence.runs.checkpoint_store`` owns exactly one
table. Checkpoint *ordering* and the thread bookkeeping around a write stay
with the caller, so these tests exercise the row operations and the
encrypt-at-rest behaviour without asserting anything about threads.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import InvalidToken

from zeroth.service.bootstrap.migrations import run_migrations
from zeroth.platform.storage import EncryptedField, NullWorkspaceScopeContext
from zeroth.platform.storage.async_sqlite import AsyncSQLiteDatabase
from zeroth.platform.storage.json import to_json_value
from zeroth.integrations.persistence.runs.checkpoint_store import (
    CheckpointRowStore,
    new_checkpoint_id,
)
from zeroth.runtime.runs import Run


def test_checkpoint_store_imports_in_a_cold_interpreter() -> None:
    """The checkpoint adapter must import without ``zeroth.core`` warmed first."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from zeroth.integrations.persistence.runs import checkpoint_store",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"checkpoint_store is not cold-importable:\n{result.stderr}"


def _make_run(run_id: str = "run-1", *, thread_id: str = "thread-1") -> Run:
    return Run(
        run_id=run_id,
        workflow_name="demo",
        graph_version_ref="graph-1",
        deployment_ref="deployment-1",
        tenant_id="tenant-1",
        thread_id=thread_id,
    )


async def _write(
    store: CheckpointRowStore,
    run: Run,
    *,
    checkpoint_id: str,
    checkpoint_order: int,
) -> None:
    await store.write_row(
        checkpoint_id=checkpoint_id,
        run_id=run.run_id,
        thread_id=run.thread_id,
        checkpoint_order=checkpoint_order,
        state_json=to_json_value(run.model_dump(mode="json")),
        created_at=run.updated_at.isoformat(),
    )


def _store(database: AsyncSQLiteDatabase, tenant_id: str = "tenant-1") -> CheckpointRowStore:
    return CheckpointRowStore(database, NullWorkspaceScopeContext(tenant_id=tenant_id))


def test_new_checkpoint_id_returns_a_unique_hex_id() -> None:
    """Checkpoint IDs are random hex, so concurrent writers do not collide."""
    first = new_checkpoint_id()
    second = new_checkpoint_id()

    assert first != second
    assert len(first) == 32
    assert int(first, 16) >= 0


async def test_write_row_then_get_round_trips_the_run_state(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """A written checkpoint reads back as the run it snapshotted."""
    store = _store(sqlite_db)
    run = _make_run()

    await _write(store, run, checkpoint_id="checkpoint-1", checkpoint_order=0)
    restored = await store.get("checkpoint-1")

    assert restored is not None
    assert restored.run_id == run.run_id
    assert restored.thread_id == run.thread_id
    assert restored.workflow_name == run.workflow_name


async def test_get_returns_none_for_an_unknown_checkpoint(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """Missing checkpoints are absent, not an error."""
    store = _store(sqlite_db)

    assert await store.get("missing") is None


async def test_write_row_upserts_an_existing_checkpoint_id(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """Re-writing a checkpoint ID replaces its state rather than failing."""
    store = _store(sqlite_db)
    run = _make_run()
    await _write(store, run, checkpoint_id="checkpoint-1", checkpoint_order=0)

    run.workflow_name = "updated"
    await _write(store, run, checkpoint_id="checkpoint-1", checkpoint_order=1)
    restored = await store.get("checkpoint-1")

    assert restored is not None
    assert restored.workflow_name == "updated"


async def test_same_checkpoint_id_is_atomic_and_independent_across_tenants(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    owner_store = _store(sqlite_db)
    foreign_store = _store(sqlite_db, "tenant-2")
    owner = _make_run("run-a")
    foreign = _make_run("run-b")
    foreign.tenant_id = "tenant-2"
    owner.workflow_name = "owner-secret"
    foreign.workflow_name = "foreign-value"

    await asyncio.gather(
        _write(owner_store, owner, checkpoint_id="shared-checkpoint", checkpoint_order=0),
        _write(foreign_store, foreign, checkpoint_id="shared-checkpoint", checkpoint_order=0),
    )

    owner_copy = await owner_store.get("shared-checkpoint")
    foreign_copy = await foreign_store.get("shared-checkpoint")
    assert owner_copy is not None and owner_copy.workflow_name == "owner-secret"
    assert foreign_copy is not None and foreign_copy.workflow_name == "foreign-value"


async def test_checkpoint_list_and_delete_use_durable_owner_scope(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    owner_store = _store(sqlite_db)
    foreign_store = _store(sqlite_db, "tenant-2")
    owner = _make_run("run-a", thread_id="shared-thread")
    foreign = _make_run("run-b", thread_id="shared-thread")
    foreign.tenant_id = "tenant-2"
    await _write(owner_store, owner, checkpoint_id="shared-checkpoint", checkpoint_order=0)
    await _write(foreign_store, foreign, checkpoint_id="shared-checkpoint", checkpoint_order=0)

    assert await owner_store.list_ids("shared-thread") == ["shared-checkpoint"]
    assert await owner_store.list_ids("unknown-thread") == []
    assert await foreign_store.delete("shared-checkpoint") is True
    assert await owner_store.get("shared-checkpoint") is not None
    assert await foreign_store.get("shared-checkpoint") is None


async def test_latest_id_for_run_returns_the_highest_ordered_checkpoint(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """Checkpoint recency is decided by ``checkpoint_order``, not insertion order."""
    store = _store(sqlite_db)
    run = _make_run()
    await _write(store, run, checkpoint_id="checkpoint-b", checkpoint_order=2)
    await _write(store, run, checkpoint_id="checkpoint-a", checkpoint_order=1)

    assert await store.latest_id_for_run(run.run_id) == "checkpoint-b"


async def test_latest_id_for_run_returns_none_without_checkpoints(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """A run that never checkpointed has no latest checkpoint."""
    store = _store(sqlite_db)

    assert await store.latest_id_for_run("run-1") is None


@pytest.fixture
async def encrypted_database(tmp_path: Path) -> AsyncSQLiteDatabase:
    """A migrated database configured with an at-rest encryption key."""
    db_path = str(tmp_path / "encrypted.db")
    run_migrations(f"sqlite:///{db_path}")
    database = AsyncSQLiteDatabase(
        path=db_path,
        encryption_key=EncryptedField.generate_key(),
    )
    yield database
    await database.close()


async def test_state_json_is_encrypted_at_rest_when_a_key_is_configured(
    encrypted_database: AsyncSQLiteDatabase,
) -> None:
    """Checkpoint state is the richest plaintext surface, so it must not sit in the clear."""
    store = _store(encrypted_database)
    run = _make_run()

    await _write(store, run, checkpoint_id="checkpoint-1", checkpoint_order=0)

    async with encrypted_database.transaction() as connection:
        row = await connection.fetch_one(
            "SELECT state_json FROM run_checkpoints WHERE checkpoint_id = ?",
            ("checkpoint-1",),
        )
    assert row is not None
    assert "demo" not in row["state_json"]

    restored = await store.get("checkpoint-1")
    assert restored is not None
    assert restored.workflow_name == "demo"


async def test_reading_falls_back_to_plaintext_written_before_encryption(
    encrypted_database: AsyncSQLiteDatabase,
) -> None:
    """Enabling encryption must not orphan checkpoints written while it was off."""
    run = _make_run()
    async with encrypted_database.transaction() as connection:
        await connection.execute(
            """
                INSERT INTO run_checkpoints (
                    checkpoint_id, run_id, thread_id, checkpoint_order, state_json, created_at,
                    tenant_id, workspace_id, workspace_scope
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "plaintext-checkpoint",
                run.run_id,
                run.thread_id,
                0,
                    to_json_value(run.model_dump(mode="json")),
                    run.updated_at.isoformat(),
                    run.tenant_id,
                    run.workspace_id,
                    "null",
                ),
        )

    restored = await _store(encrypted_database).get("plaintext-checkpoint")

    assert restored is not None
    assert restored.workflow_name == "demo"


async def test_wrong_encryption_key_fails_closed(tmp_path: Path) -> None:
    db_path = str(tmp_path / "rotated-checkpoint-key.db")
    run_migrations(f"sqlite:///{db_path}")
    writer = AsyncSQLiteDatabase(path=db_path, encryption_key=EncryptedField.generate_key())
    run = _make_run()
    await _write(_store(writer), run, checkpoint_id="checkpoint-1", checkpoint_order=0)
    await writer.close()

    reader = AsyncSQLiteDatabase(path=db_path, encryption_key=EncryptedField.generate_key())
    try:
        with pytest.raises(InvalidToken):
            await _store(reader).get("checkpoint-1")
    finally:
        await reader.close()
