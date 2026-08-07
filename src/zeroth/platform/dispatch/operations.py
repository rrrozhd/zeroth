"""Durable receipts for side-effecting operations.

The runtime's delivery boundary is at-least-once: a worker can apply an external
effect and then die before recording that it did.  This store is what turns that
into a *recognisable* repeat -- every logical operation gets one durable row, so
a second attempt can discover the first one's outcome instead of replaying blind.

Like :mod:`zeroth.platform.dispatch.lease`, this module sits below the run domain
and speaks the column contract rather than importing runtime vocabulary; the
platform layer is dependency-free by construction (``ALLOWED_DEPENDENCIES``).
Callers map their own identity types onto these primitives.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from zeroth.platform.storage import AsyncDatabase

# The support vocabulary mirrors zeroth.contracts.graph.SideEffectSupport. It is
# duplicated as plain strings, not imported, because platform may not depend on
# contracts. tests/dispatch/test_side_effect_operations.py pins the values.
SUPPORT_AT_LEAST_ONCE = "at_least_once"


class OperationState(StrEnum):
    """The five outcomes a side-effecting operation can be in.

    ``NOT_STARTED`` is deliberately not a stored row.  Writing one before the
    effect is attempted would itself be a durable act that recovery could not
    tell apart from a real attempt, so absence *is* the not-started state.
    """

    NOT_STARTED = "NOT_STARTED"
    IN_FLIGHT = "IN_FLIGHT"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True, slots=True)
class OperationClaim:
    """The verdict on whether the caller may perform the effect."""

    state: OperationState
    first_execution: bool
    receipt: str | None = None
    reconciliation_required: bool = False
    reconciliation_exhausted: bool = False
    residual_duplicate_risk: bool = False
    attempts: int = 0


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class SideEffectOperationStore:
    """Persists one row per logical operation and converges duplicate reports."""

    database: AsyncDatabase
    max_reconciliation_attempts: int = 3
    metrics_collector: Any | None = None

    def _count(self, name: str) -> None:
        """Emit one counter, if a collector was wired.

        Each outcome gets its own counter rather than one counter with a label,
        so "how often did we suppress a replay" is answerable without a metrics
        backend that supports label queries.
        """
        if self.metrics_collector is not None:
            self.metrics_collector.increment(name)

    # -----------------------------------------------------------------------
    # Reads
    # -----------------------------------------------------------------------

    async def get(self, operation_key: str) -> dict[str, Any] | None:
        """Return the stored record, with ``dedupe_supported`` derived."""
        async with self.database.transaction() as conn:
            row = await conn.fetch_one(
                "SELECT * FROM side_effect_operations WHERE operation_key = ?",
                (operation_key,),
            )
        if row is None:
            return None
        record = dict(row)
        record["dedupe_supported"] = record["support"] != SUPPORT_AT_LEAST_ONCE
        return record

    async def state_of(self, operation_key: str) -> OperationState:
        record = await self.get(operation_key)
        if record is None:
            return OperationState.NOT_STARTED
        return OperationState(record["state"])

    async def pending_reconciliation(self, run_id: str) -> list[dict[str, Any]]:
        """Ambiguous operations for a run -- durable work, not an in-memory flag."""
        async with self.database.transaction() as conn:
            rows = await conn.fetch_all(
                """
                SELECT * FROM side_effect_operations
                WHERE run_id = ? AND state = ?
                ORDER BY created_at ASC
                """,
                (run_id, OperationState.AMBIGUOUS.value),
            )
        return [dict(row) for row in rows]

    # -----------------------------------------------------------------------
    # Claim
    # -----------------------------------------------------------------------

    async def claim(
        self,
        operation_key: str,
        *,
        run_id: str,
        dispatch_id: str,
        idempotency_key: str,
        target_ref: str,
        attempt: int = 0,
        support: str = SUPPORT_AT_LEAST_ONCE,
    ) -> OperationClaim:
        """Decide whether this caller may apply the effect.

        Four cases, and the distinction between the last two is the whole point:
        a *confirmed* failure is safe to retry, whereas an operation still marked
        IN_FLIGHT means some earlier attempt vanished mid-flight and nobody knows
        whether the effect landed.  That becomes AMBIGUOUS, never FAILED.
        """
        now = _utc_now()
        async with self.database.transaction() as conn:
            row = await conn.fetch_one(
                "SELECT * FROM side_effect_operations WHERE operation_key = ?",
                (operation_key,),
            )

            if row is None:
                # ON CONFLICT DO NOTHING ... RETURNING is the compare-and-set:
                # a concurrent claimer that lost the race gets no row back
                # instead of a uniqueness error, and only the winner is told it
                # may perform the effect.
                won = await conn.fetch_one(
                    """
                    INSERT INTO side_effect_operations (
                        operation_key, run_id, dispatch_id, idempotency_key,
                        target_ref, attempt, state, support,
                        reconciliation_attempts, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    ON CONFLICT (operation_key) DO NOTHING
                    RETURNING operation_key
                    """,
                    (
                        operation_key,
                        run_id,
                        dispatch_id,
                        idempotency_key,
                        target_ref,
                        attempt,
                        OperationState.IN_FLIGHT.value,
                        support,
                        now,
                        now,
                    ),
                )
                if won is not None:
                    self._count("zeroth_side_effect_first_execution_total")
                    return OperationClaim(
                        state=OperationState.IN_FLIGHT,
                        first_execution=True,
                    )
                # Lost the insert race: another claimer owns it and is in
                # flight, so this caller must not also perform the effect.
                self._count("zeroth_side_effect_ambiguous_total")
                return OperationClaim(
                    state=OperationState.AMBIGUOUS,
                    first_execution=False,
                    reconciliation_required=True,
                    residual_duplicate_risk=support == SUPPORT_AT_LEAST_ONCE,
                )

            state = OperationState(row["state"])
            stored_support = row["support"]
            attempts = int(row["reconciliation_attempts"])

            if state is OperationState.COMPLETED:
                # Replay suppression: the effect is known to have landed.
                self._count("zeroth_side_effect_replay_suppressed_total")
                return OperationClaim(
                    state=state,
                    first_execution=False,
                    receipt=row["receipt"],
                    attempts=attempts,
                )

            if state is OperationState.FAILED:
                # Guarded on the observed state so two claimers retrying the
                # same failed operation cannot both be authorised.
                won = await conn.fetch_one(
                    """
                    UPDATE side_effect_operations
                    SET state = ?, attempt = ?, error = NULL, updated_at = ?
                    WHERE operation_key = ? AND state = ?
                    RETURNING operation_key
                    """,
                    (
                        OperationState.IN_FLIGHT.value,
                        attempt,
                        now,
                        operation_key,
                        OperationState.FAILED.value,
                    ),
                )
                if won is not None:
                    self._count("zeroth_side_effect_first_execution_total")
                    return OperationClaim(
                        state=OperationState.IN_FLIGHT,
                        first_execution=True,
                        attempts=attempts,
                    )
                self._count("zeroth_side_effect_ambiguous_total")
                return OperationClaim(
                    state=OperationState.AMBIGUOUS,
                    first_execution=False,
                    reconciliation_required=True,
                    residual_duplicate_risk=stored_support == SUPPORT_AT_LEAST_ONCE,
                    attempts=attempts,
                )

            # IN_FLIGHT (a vanished attempt) or already AMBIGUOUS.
            if state is OperationState.IN_FLIGHT:
                # Guarded on IN_FLIGHT, not just the key: the in-flight attempt
                # may complete between the read above and this write, and an
                # unguarded update would demote a COMPLETED operation back to
                # AMBIGUOUS -- losing a known-good receipt.
                await conn.execute(
                    """
                    UPDATE side_effect_operations
                    SET state = ?, attempt = ?, ambiguity_reason = ?, updated_at = ?
                    WHERE operation_key = ? AND state = ?
                    """,
                    (
                        OperationState.AMBIGUOUS.value,
                        attempt,
                        "claimed while a previous attempt was still in flight",
                        now,
                        operation_key,
                        OperationState.IN_FLIGHT.value,
                    ),
                )
                # The guard may have refused because the in-flight attempt just
                # completed. Report what is actually stored rather than
                # asserting ambiguity we no longer have.
                settled = await conn.fetch_one(
                    "SELECT state, receipt FROM side_effect_operations WHERE operation_key = ?",
                    (operation_key,),
                )
                if (
                    settled is not None
                    and OperationState(settled["state"]) is OperationState.COMPLETED
                ):
                    self._count("zeroth_side_effect_replay_suppressed_total")
                    return OperationClaim(
                        state=OperationState.COMPLETED,
                        first_execution=False,
                        receipt=settled["receipt"],
                        attempts=attempts,
                    )

        exhausted = attempts >= self.max_reconciliation_attempts
        self._count("zeroth_side_effect_ambiguous_total")
        return OperationClaim(
            state=OperationState.AMBIGUOUS,
            first_execution=False,
            reconciliation_required=not exhausted,
            reconciliation_exhausted=exhausted,
            # Only when the target can neither dedupe nor be queried does a
            # re-execution actually risk applying the effect twice.
            residual_duplicate_risk=stored_support == SUPPORT_AT_LEAST_ONCE,
            attempts=attempts,
        )

    # -----------------------------------------------------------------------
    # Outcome reports -- all convergent
    # -----------------------------------------------------------------------

    async def complete(self, operation_key: str, *, receipt: str) -> bool:
        """Store the result.  Returns False if a result was already stored.

        The first COMPLETED result wins.  Letting a later report overwrite it
        would make the stored outcome depend on arrival order between two
        workers that both believe they own the operation.
        """
        now = _utc_now()
        async with self.database.transaction() as conn:
            # One guarded statement, not read-then-write. The guard is the
            # state itself, so exactly one of two concurrent completers gets a
            # row back -- the earlier version read the state and then updated
            # separately, which let both observe a non-completed row.
            won = await conn.fetch_one(
                """
                UPDATE side_effect_operations
                SET state = ?, receipt = ?, error = NULL, updated_at = ?
                WHERE operation_key = ? AND state != ?
                RETURNING operation_key
                """,
                (
                    OperationState.COMPLETED.value,
                    receipt,
                    now,
                    operation_key,
                    OperationState.COMPLETED.value,
                ),
            )
        return won is not None

    async def fail(self, operation_key: str, *, error: str) -> None:
        """Record a *confirmed* failure.  A completed operation is never undone."""
        now = _utc_now()
        async with self.database.transaction() as conn:
            await conn.execute(
                """
                UPDATE side_effect_operations
                SET state = ?, error = ?, updated_at = ?
                WHERE operation_key = ? AND state != ?
                """,
                (
                    OperationState.FAILED.value,
                    error,
                    now,
                    operation_key,
                    OperationState.COMPLETED.value,
                ),
            )

    async def mark_ambiguous(self, operation_key: str, *, reason: str) -> None:
        """Record that the outcome is unknown -- distinct from known-failed."""
        self._count("zeroth_side_effect_ambiguous_total")
        now = _utc_now()
        async with self.database.transaction() as conn:
            await conn.execute(
                """
                UPDATE side_effect_operations
                SET state = ?, ambiguity_reason = ?, updated_at = ?
                WHERE operation_key = ? AND state != ?
                """,
                (
                    OperationState.AMBIGUOUS.value,
                    reason,
                    now,
                    operation_key,
                    OperationState.COMPLETED.value,
                ),
            )

    async def record_reconciliation(
        self,
        operation_key: str,
        *,
        resolved: bool,
        receipt: str | None = None,
        error: str | None = None,
    ) -> OperationState:
        """Fold one reconciliation attempt into the record.

        An unresolved attempt burns budget and leaves the operation AMBIGUOUS --
        exhausting the budget must not manufacture a verdict, because the runtime
        still does not know what the external system did.
        """
        now = _utc_now()
        async with self.database.transaction() as conn:
            if resolved:
                # Guarded on "not already completed", so the first reconciler to
                # discover the outcome wins and a later one cannot overwrite it.
                won = await conn.fetch_one(
                    """
                    UPDATE side_effect_operations
                    SET state = ?, receipt = ?, error = NULL,
                        reconciliation_attempts = reconciliation_attempts + 1,
                        updated_at = ?
                    WHERE operation_key = ? AND state != ?
                    RETURNING operation_key
                    """,
                    (
                        OperationState.COMPLETED.value,
                        receipt,
                        now,
                        operation_key,
                        OperationState.COMPLETED.value,
                    ),
                )
                if won is not None:
                    self._count("zeroth_side_effect_reconciliation_succeeded_total")
                    return OperationState.COMPLETED
                # Either it was already completed, or the row is gone.
                return await self.state_of(operation_key)

            # An unresolved attempt burns budget and leaves the operation
            # AMBIGUOUS. Exhausting the budget must not manufacture a verdict:
            # the runtime still does not know what the external system did.
            moved = await conn.fetch_one(
                """
                UPDATE side_effect_operations
                SET state = ?, error = ?,
                    reconciliation_attempts = reconciliation_attempts + 1,
                    updated_at = ?
                WHERE operation_key = ? AND state != ?
                RETURNING operation_key
                """,
                (
                    OperationState.AMBIGUOUS.value,
                    error,
                    now,
                    operation_key,
                    OperationState.COMPLETED.value,
                ),
            )
        if moved is None:
            return await self.state_of(operation_key)
        self._count("zeroth_side_effect_reconciliation_failed_total")
        return OperationState.AMBIGUOUS


def record_is_dedupe_supported(record: Mapping[str, Any]) -> bool:
    """Whether a stored record's target can collapse a repeat."""
    return record["support"] != SUPPORT_AT_LEAST_ONCE
