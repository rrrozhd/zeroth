"""SQL adapter for atomic token-engine snapshot replacement."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass

from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshot, TokenEngineSnapshotState
from zeroth.contracts.graph.tokens import DispatchLifecycleState, SchedulingState
from zeroth.platform.primitives import utc_now
from zeroth.platform.storage import AsyncDatabase
from zeroth.runtime.orchestration.token_snapshot_store import (
    TokenSnapshotConcurrencyError,
    TokenSnapshotCorruptionError,
    TokenSnapshotTransitionError,
    TokenSnapshotWriteDisabledError,
)

_TERMINAL_STATES = {
    TokenEngineSnapshotState.COMPLETED,
    TokenEngineSnapshotState.CANCELLED,
    TokenEngineSnapshotState.FAILED,
}


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
        expected: dict[str, object] = {}
        converters = {
            "run_id": str,
            "revision": int,
            "schema_version": int,
            "next_token_ordinal": int,
        }
        for field, converter in converters.items():
            try:
                expected[field] = converter(row[field])
            except (KeyError, TypeError, ValueError) as exc:
                raise TokenSnapshotCorruptionError(
                    f"persisted {field} metadata is malformed"
                ) from exc
        for field, value in expected.items():
            if getattr(snapshot, field) != value:
                raise TokenSnapshotCorruptionError(
                    f"persisted {field} metadata contradicts serialized token snapshot"
                )
        return snapshot

    @staticmethod
    def _integer_metadata(row: dict[str, object], field: str) -> int:
        try:
            return int(row[field])
        except (KeyError, TypeError, ValueError) as exc:
            raise TokenSnapshotCorruptionError(f"persisted {field} metadata is malformed") from exc

    @staticmethod
    def _validate_fence_coherence(snapshot: TokenEngineSnapshot) -> None:
        generation = (
            0 if snapshot.cancellation_fence is None else snapshot.cancellation_fence.generation
        )
        cancellation_requested_token_ids: set[str] = set()
        for dispatch in snapshot.in_flight_dispatches:
            if dispatch.lifecycle_state is not DispatchLifecycleState.CANCELLATION_REQUESTED:
                if dispatch.cancellation_generation != generation:
                    raise TokenSnapshotTransitionError(
                        "ordinary executing dispatch must match the current cancellation fence"
                    )
                continue
            fence = snapshot.cancellation_fence
            if (
                fence is None
                or dispatch.cancellation_generation >= fence.generation
                or dispatch.cancellation_requested_generation != fence.generation
                or dispatch.cancellation_requested_revision != fence.requested_revision
            ):
                raise TokenSnapshotTransitionError(
                    "cancellation-requested dispatch must match the current cancellation fence"
                )
            cancellation_requested_token_ids.add(dispatch.token.token_id)

        for token in snapshot.tokens:
            if token.scheduling_state is SchedulingState.SETTLED:
                continue
            if token.token_id in cancellation_requested_token_ids:
                continue
            if token.cancellation_generation != generation:
                raise TokenSnapshotTransitionError(
                    "every live token must match the current cancellation fence"
                )

    @classmethod
    def _validate_transition(
        cls,
        current: TokenEngineSnapshot | None,
        proposed: TokenEngineSnapshot,
    ) -> None:
        cls._validate_fence_coherence(proposed)
        if current is None:
            return
        current_fence = current.cancellation_fence
        proposed_fence = proposed.cancellation_fence
        current_generation = 0 if current_fence is None else current_fence.generation
        proposed_generation = 0 if proposed_fence is None else proposed_fence.generation
        if proposed_generation < current_generation:
            raise TokenSnapshotTransitionError("cancellation generation cannot decrease")
        if proposed_generation == current_generation and current_fence is not None:
            if proposed_fence is None or (
                proposed_fence.requested_revision != current_fence.requested_revision
            ):
                raise TokenSnapshotTransitionError(
                    "cancellation request metadata cannot change within a generation"
                )
            if not set(current_fence.acknowledged_token_ids).issubset(
                proposed_fence.acknowledged_token_ids
            ):
                raise TokenSnapshotTransitionError("cancellation acknowledgements cannot regress")
        if current.state in _TERMINAL_STATES and proposed.state is not current.state:
            raise TokenSnapshotTransitionError("terminal snapshot state is absorbing")

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
            lock_suffix = " FOR UPDATE" if self.database.backend == "postgres" else ""
            run = await connection.fetch_one(
                "SELECT run_id, token_snapshot_write_disabled FROM runs WHERE run_id = ?"
                + lock_suffix,
                (run_id,),
            )
            if run is None:
                raise KeyError(run_id)
            write_disabled = self._integer_metadata(run, "token_snapshot_write_disabled")
            if write_disabled not in {0, 1}:
                raise TokenSnapshotCorruptionError(
                    "persisted token_snapshot_write_disabled metadata is malformed"
                )
            if write_disabled:
                raise TokenSnapshotWriteDisabledError(run_id)

            current_row = await connection.fetch_one(
                "SELECT run_id, revision, schema_version, next_token_ordinal, snapshot_json "
                "FROM token_engine_snapshots WHERE run_id = ?",
                (run_id,),
            )
            current = None if current_row is None else self._decode_row(current_row)
            self._validate_transition(current, snapshot)
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
                    actual_revision=(
                        None if winner is None else self._integer_metadata(winner, "revision")
                    ),
                )
        return snapshot


__all__ = ["TokenSnapshotRowStore"]
