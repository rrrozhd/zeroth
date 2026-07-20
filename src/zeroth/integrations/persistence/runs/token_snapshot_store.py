"""SQL adapter for atomic token-engine snapshot replacement."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass

from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshot
from zeroth.platform.primitives import utc_now
from zeroth.platform.storage import AsyncDatabase
from zeroth.runtime.orchestration.token_snapshot_store import (
    TokenSnapshotConcurrencyError,
    TokenSnapshotCorruptionError,
    TokenSnapshotTransitionError,
)


@dataclass(slots=True)
class TokenSnapshotRowStore:
    """Owns the one-row-per-run ``token_engine_snapshots`` table."""

    database: AsyncDatabase

    def _encode(self, snapshot: TokenEngineSnapshot) -> str:
        payload = snapshot.model_dump_json()
        encrypted_field = getattr(self.database, "encrypted_field", None)
        return payload if encrypted_field is None else encrypted_field.encrypt(payload)

    def _decode(self, payload: str) -> TokenEngineSnapshot:
        encrypted_field = getattr(self.database, "encrypted_field", None)
        if encrypted_field is not None:
            with suppress(Exception):
                payload = encrypted_field.decrypt(payload)
        return TokenEngineSnapshot.model_validate_json(payload)

    def _decode_row(self, row: dict[str, object]) -> TokenEngineSnapshot:
        try:
            snapshot = self._decode(str(row["snapshot_json"]))
        except Exception as exc:
            raise TokenSnapshotCorruptionError(
                "persisted token snapshot payload cannot be decoded"
            ) from exc
        expected = {
            "run_id": str(row["run_id"]),
            "revision": int(row["revision"]),
            "schema_version": int(row["schema_version"]),
            "next_token_ordinal": int(row["next_token_ordinal"]),
        }
        for field, value in expected.items():
            if getattr(snapshot, field) != value:
                raise TokenSnapshotCorruptionError(
                    f"persisted {field} metadata contradicts serialized token snapshot"
                )
        return snapshot

    async def get(self, run_id: str) -> TokenEngineSnapshot | None:
        async with self.database.transaction() as connection:
            row = await connection.fetch_one(
                "SELECT run_id, revision, schema_version, next_token_ordinal, snapshot_json "
                "FROM token_engine_snapshots WHERE run_id = ?",
                (run_id,),
            )
        return None if row is None else self._decode_row(row)

    async def compare_and_swap(
        self,
        run_id: str,
        *,
        expected_revision: int | None,
        snapshot: TokenEngineSnapshot,
    ) -> TokenEngineSnapshot:
        if snapshot.run_id != run_id:
            raise TokenSnapshotTransitionError("snapshot run_id must match the repository key")
        if expected_revision is None:
            if snapshot.revision != 0:
                raise TokenSnapshotTransitionError("an initial snapshot must use revision zero")
        elif snapshot.revision != expected_revision + 1:
            raise TokenSnapshotTransitionError(
                "snapshot revision must advance exactly one beyond expected_revision"
            )

        encoded = self._encode(snapshot)
        updated_at = utc_now().isoformat()
        async with self.database.transaction(write_lock=True) as connection:
            run = await connection.fetch_one(
                "SELECT run_id FROM runs WHERE run_id = ?",
                (run_id,),
            )
            if run is None:
                raise KeyError(run_id)

            current_row = await connection.fetch_one(
                "SELECT run_id, revision, schema_version, next_token_ordinal, snapshot_json "
                "FROM token_engine_snapshots WHERE run_id = ?",
                (run_id,),
            )
            current = None if current_row is None else self._decode_row(current_row)
            actual_revision = None if current is None else current.revision
            if actual_revision != expected_revision:
                raise TokenSnapshotConcurrencyError(
                    run_id,
                    expected_revision=expected_revision,
                    actual_revision=actual_revision,
                )
            if current is not None and snapshot.next_token_ordinal < current.next_token_ordinal:
                raise TokenSnapshotTransitionError(
                    "next_token_ordinal cannot move backward across snapshot revisions"
                )

            if expected_revision is None:
                written = await connection.fetch_one(
                    """
                    INSERT INTO token_engine_snapshots (
                        run_id, revision, schema_version, next_token_ordinal,
                        snapshot_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id) DO NOTHING
                    RETURNING revision
                    """,
                    (
                        run_id,
                        snapshot.revision,
                        snapshot.schema_version,
                        snapshot.next_token_ordinal,
                        encoded,
                        updated_at,
                    ),
                )
            else:
                written = await connection.fetch_one(
                    """
                    UPDATE token_engine_snapshots
                    SET revision = ?, schema_version = ?, next_token_ordinal = ?,
                        snapshot_json = ?, updated_at = ?
                    WHERE run_id = ? AND revision = ?
                    RETURNING revision
                    """,
                    (
                        snapshot.revision,
                        snapshot.schema_version,
                        snapshot.next_token_ordinal,
                        encoded,
                        updated_at,
                        run_id,
                        expected_revision,
                    ),
                )
            if written is None:
                winner = await connection.fetch_one(
                    "SELECT revision FROM token_engine_snapshots WHERE run_id = ?",
                    (run_id,),
                )
                raise TokenSnapshotConcurrencyError(
                    run_id,
                    expected_revision=expected_revision,
                    actual_revision=None if winner is None else int(winner["revision"]),
                )
        return snapshot


__all__ = ["TokenSnapshotRowStore"]
