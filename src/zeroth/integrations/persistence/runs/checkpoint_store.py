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


def _workspace_scope(workspace_id: str | None) -> str:
    return "null" if workspace_id is None else f"value:{workspace_id}"


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
        if encrypted_field is None or state_json.lstrip().startswith("{"):
            return state_json
        return encrypted_field.decrypt(state_json)

    async def write_row(
        self,
        *,
        checkpoint_id: str,
        run_id: str,
        thread_id: str,
        tenant_id: str,
        workspace_id: str | None,
        checkpoint_order: int,
        state_json: str,
        created_at: str,
    ) -> None:
        """Insert or replace one checkpoint row, encrypting the state at rest."""
        async with self.database.transaction() as connection:
            await self.write_row_in_connection(
                connection,
                checkpoint_id=checkpoint_id,
                run_id=run_id,
                thread_id=thread_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
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
        tenant_id: str,
        workspace_id: str | None,
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
        await connection.execute(  # type: ignore[attr-defined]
            """
            INSERT INTO run_checkpoints (
                checkpoint_id, run_id, thread_id, checkpoint_order, state_json, created_at,
                tenant_id, workspace_id, workspace_scope
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, workspace_scope, checkpoint_id) DO UPDATE SET
                run_id = excluded.run_id,
                thread_id = excluded.thread_id,
                checkpoint_order = excluded.checkpoint_order,
                state_json = excluded.state_json,
                workspace_id = excluded.workspace_id
            """,
            (
                checkpoint_id,
                run_id,
                thread_id,
                checkpoint_order,
                self.encrypt_state_json(state_json),
                created_at,
                tenant_id,
                workspace_id,
                _workspace_scope(workspace_id),
            ),
        )

    async def get(
        self,
        checkpoint_id: str,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
    ) -> Run | None:
        """Load a checkpoint through its immutable row owner scope."""
        sql = "SELECT c.state_json FROM run_checkpoints c WHERE c.checkpoint_id = ?"
        params: tuple[str, ...] = (checkpoint_id,)
        if tenant_id is not None:
            sql += " AND c.tenant_id = ? AND c.workspace_scope = ?"
            params += (tenant_id, _workspace_scope(workspace_id))
        sql += " ORDER BY c.tenant_id, c.workspace_scope LIMIT 2"
        async with self.database.transaction() as connection:
            rows = await connection.fetch_all(sql, params)
        if len(rows) != 1:
            return None
        row = rows[0]
        state_json = self.decrypt_state_json(row["state_json"])
        return Run.model_validate(load_typed_value(state_json, dict[str, Any]))

    async def latest_id_for_run(
        self,
        run_id: str,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
    ) -> str | None:
        """Return the checkpoint_id for the most recent checkpoint of a run."""
        async with self.database.transaction() as connection:
            sql = """
                SELECT checkpoint_id FROM run_checkpoints
                WHERE run_id = ?
            """
            params: tuple[str, ...] = (run_id,)
            if tenant_id is not None:
                sql += " AND tenant_id = ? AND workspace_scope = ?"
                params += (tenant_id, _workspace_scope(workspace_id))
            sql += " ORDER BY checkpoint_order DESC LIMIT 1"
            row = await connection.fetch_one(sql, params)
        return row["checkpoint_id"] if row else None

    async def list_ids(
        self,
        thread_id: str,
        *,
        tenant_id: str,
        workspace_id: str | None,
    ) -> list[str]:
        """List a thread's checkpoint IDs through immutable row ownership."""
        async with self.database.transaction() as connection:
            rows = await connection.fetch_all(
                """SELECT checkpoint_id FROM run_checkpoints
                   WHERE thread_id = ? AND tenant_id = ? AND workspace_scope = ?
                   ORDER BY checkpoint_order, checkpoint_id""",
                (thread_id, tenant_id, _workspace_scope(workspace_id)),
            )
        return [row["checkpoint_id"] for row in rows]

    async def delete(
        self,
        checkpoint_id: str,
        *,
        tenant_id: str,
        workspace_id: str | None,
    ) -> bool:
        """Delete exactly one checkpoint owned by the supplied scope."""
        async with self.database.transaction() as connection:
            row = await connection.fetch_one(
                """DELETE FROM run_checkpoints
                   WHERE checkpoint_id = ? AND tenant_id = ? AND workspace_scope = ?
                   RETURNING checkpoint_id""",
                (checkpoint_id, tenant_id, _workspace_scope(workspace_id)),
            )
        return row is not None
