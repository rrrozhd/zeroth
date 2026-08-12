"""Storage for the ``run_checkpoints`` table.

This adapter owns exactly one table. It writes and reads checkpoint rows and
applies at-rest encryption to the serialized state, and it deliberately does
not know about threads: checkpoint *ordering* and the thread bookkeeping that
surrounds a write are the caller's, because they read and write the thread
record. Splitting them the other way would have moved a transaction boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from zeroth.platform.storage import (
    SERVICE_SCOPE_REGISTRY,
    AsyncDatabase,
    NullWorkspaceScopeContext,
    ScopeContext,
    ScopedTable,
)
from zeroth.platform.storage.json import load_typed_value
from zeroth.platform.storage.scoped_table import BoundStructuredTable
from zeroth.runtime.runs import Run


def new_checkpoint_id() -> str:
    """Generate a new random hex ID for a checkpoint."""
    return uuid4().hex


@dataclass(slots=True)
class CheckpointRowStore:
    """Reads and writes ``run_checkpoints`` rows for a single database."""

    database: AsyncDatabase
    scope_context: ScopeContext | NullWorkspaceScopeContext
    table: ScopedTable = field(init=False)

    def __post_init__(self) -> None:
        self.table = ScopedTable(
            self.database,
            SERVICE_SCOPE_REGISTRY,
            "service.run_checkpoints",
            self.scope_context,
        )

    def encrypt_state_json(self, state_json: str) -> str:
        """Encrypt state_json at rest when the database has an encrypted_field."""
        encrypted_field = getattr(self.database, "encrypted_field", None)
        if encrypted_field is None:
            return state_json
        return encrypted_field.encrypt(state_json)

    def decrypt_state_json(self, state_json: str) -> str:
        """Reverse of encrypt_state_json; passthrough when no encrypted_field."""
        encrypted_field = getattr(self.database, "encrypted_field", None)
        if encrypted_field is None:
            return state_json
        try:
            return encrypted_field.decrypt(state_json)
        except Exception:
            # Value was written before encryption was enabled; return as-is.
            return state_json

    async def write_row(
        self,
        *,
        checkpoint_id: str,
        run_id: str,
        thread_id: str,
        checkpoint_order: int,
        state_json: str,
        created_at: str,
    ) -> None:
        """Insert or replace one checkpoint row, encrypting the state at rest."""
        async with self.table.transaction() as checkpoints:
            await self.write_row_bound(
                checkpoints,
                checkpoint_id=checkpoint_id,
                run_id=run_id,
                thread_id=thread_id,
                checkpoint_order=checkpoint_order,
                state_json=state_json,
                created_at=created_at,
            )

    async def write_row_in_connection(
        self,
        connection: object,
        *,
        checkpoint_id: str,
        run_id: str,
        thread_id: str,
        checkpoint_order: int,
        state_json: str,
        created_at: str,
    ) -> None:
        """The write_row body on a caller-owned connection.

        ZER-26/AUD-004: a fenced ``put_run`` performs the thread, checkpoint and
        runs-row writes in one transaction so a fence rejection rolls back all
        three — a displaced worker previously overwrote the checkpoint row
        before the fenced save raised.
        """
        await self.write_row_bound(
            self.table.in_transaction(connection),
            checkpoint_id=checkpoint_id,
            run_id=run_id,
            thread_id=thread_id,
            checkpoint_order=checkpoint_order,
            state_json=state_json,
            created_at=created_at,
        )

    async def write_row_bound(
        self,
        checkpoints: BoundStructuredTable,
        *,
        checkpoint_id: str,
        run_id: str,
        thread_id: str,
        checkpoint_order: int,
        state_json: str,
        created_at: str,
    ) -> None:
        await checkpoints.upsert(
            {
                "checkpoint_id": checkpoint_id,
                "run_id": run_id,
                "thread_id": thread_id,
                "checkpoint_order": checkpoint_order,
                "state_json": self.encrypt_state_json(state_json),
                "created_at": created_at,
            },
            conflict_columns=("tenant_id", "workspace_scope", "checkpoint_id"),
            update_columns=(
                "run_id",
                "thread_id",
                "checkpoint_order",
                "state_json",
            ),
            returning="checkpoint_id",
        )

    async def get(
        self,
        checkpoint_id: str,
    ) -> Run | None:
        """Load a checkpoint through its immutable row owner scope."""
        row = await self.table.select_one(
            where={"checkpoint_id": checkpoint_id}, columns=("state_json",)
        )
        if row is None:
            return None
        state_json = self.decrypt_state_json(row["state_json"])
        return Run.model_validate(load_typed_value(state_json, dict[str, Any]))

    async def latest_id_for_run(
        self,
        run_id: str,
    ) -> str | None:
        """Return the checkpoint_id for the most recent checkpoint of a run."""
        async with self.table.transaction() as checkpoints:
            rows = await checkpoints.select(
                where={"run_id": run_id},
                columns=("checkpoint_id",),
                order_by=("checkpoint_order",),
            )
        return rows[-1]["checkpoint_id"] if rows else None

    async def list_ids(
        self,
        thread_id: str,
    ) -> list[str]:
        """List a thread's checkpoint IDs through immutable row ownership."""
        async with self.table.transaction() as checkpoints:
            rows = await checkpoints.select(
                where={"thread_id": thread_id},
                columns=("checkpoint_id",),
                order_by=("checkpoint_order", "checkpoint_id"),
            )
        return [row["checkpoint_id"] for row in rows]

    async def delete(
        self,
        checkpoint_id: str,
    ) -> bool:
        """Delete exactly one checkpoint owned by the supplied scope."""
        existing = await self.table.select_one(
            where={"checkpoint_id": checkpoint_id}, columns=("checkpoint_id",)
        )
        if existing is None:
            return False
        await self.table.delete(where={"checkpoint_id": checkpoint_id})
        return True
