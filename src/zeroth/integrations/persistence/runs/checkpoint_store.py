"""Storage for the ``run_checkpoints`` table.

This adapter owns exactly one table. It writes and reads checkpoint rows and
applies at-rest encryption to the serialized state, and it deliberately does
not know about threads: checkpoint *ordering* and the thread bookkeeping that
surrounds a write are the caller's, because they read and write the thread
record. Splitting them the other way would have moved a transaction boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from zeroth.platform.storage import AsyncDatabase
from zeroth.platform.storage.json import load_typed_value
from zeroth.runtime.runs import Run


def new_checkpoint_id() -> str:
    """Generate a new random hex ID for a checkpoint."""
    return uuid4().hex


@dataclass(slots=True)
class CheckpointRowStore:
    """Reads and writes ``run_checkpoints`` rows for a single database."""

    database: AsyncDatabase

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
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                INSERT INTO run_checkpoints (
                    checkpoint_id, run_id, thread_id, checkpoint_order, state_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(checkpoint_id) DO UPDATE SET
                    run_id = excluded.run_id,
                    thread_id = excluded.thread_id,
                    checkpoint_order = excluded.checkpoint_order,
                    state_json = excluded.state_json
                """,
                (
                    checkpoint_id,
                    run_id,
                    thread_id,
                    checkpoint_order,
                    self.encrypt_state_json(state_json),
                    created_at,
                ),
            )

    async def get(self, checkpoint_id: str) -> Run | None:
        """Load a previously saved checkpoint by its ID."""
        async with self.database.transaction() as connection:
            row = await connection.fetch_one(
                "SELECT state_json FROM run_checkpoints WHERE checkpoint_id = ?",
                (checkpoint_id,),
            )
        if row is None:
            return None
        state_json = self.decrypt_state_json(row["state_json"])
        return Run.model_validate(load_typed_value(state_json, dict[str, Any]))

    async def latest_id_for_run(self, run_id: str) -> str | None:
        """Return the checkpoint_id for the most recent checkpoint of a run."""
        async with self.database.transaction() as connection:
            row = await connection.fetch_one(
                """
                SELECT checkpoint_id FROM run_checkpoints
                WHERE run_id = ?
                ORDER BY checkpoint_order DESC
                LIMIT 1
                """,
                (run_id,),
            )
        return row["checkpoint_id"] if row else None
