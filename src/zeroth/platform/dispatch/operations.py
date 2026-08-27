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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self

from zeroth.platform.storage import (
    SERVICE_SCOPE_REGISTRY,
    AsyncDatabase,
    NullWorkspaceScopeContext,
    ScopeContext,
    ScopedTable,
)
from zeroth.platform.storage.scoped_table import BoundStructuredTable
from zeroth.platform.storage.scoping import (
    ResourceOperation,
    named_isolation_probe,
    persistence_operation,
    persistence_surface,
)

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


@persistence_surface(
    "service.side_effect_operations",
    probe=named_isolation_probe("_drive_side_effect_operations"),
)
@dataclass(slots=True)
class SideEffectOperationStore:
    """Persists one row per logical operation and converges duplicate reports."""

    database: AsyncDatabase
    scope_context: ScopeContext | NullWorkspaceScopeContext
    max_reconciliation_attempts: int = 3
    metrics_collector: Any | None = None
    table: ScopedTable = field(init=False)

    def __post_init__(self) -> None:
        self.table = ScopedTable(
            self.database,
            SERVICE_SCOPE_REGISTRY,
            "service.side_effect_operations",
            self.scope_context,
        )

    @classmethod
    def for_default_compatibility(
        cls,
        database: AsyncDatabase,
        *,
        max_reconciliation_attempts: int = 3,
        metrics_collector: Any | None = None,
    ) -> Self:
        """Construct the explicitly named legacy default/null-workspace binding."""
        return cls(
            database,
            NullWorkspaceScopeContext.for_default_compatibility(),
            max_reconciliation_attempts=max_reconciliation_attempts,
            metrics_collector=metrics_collector,
        )

    def _encrypt(self, receipt: str | None) -> str | None:
        """Encrypt a receipt at rest when the database exposes an encrypted field.

        A receipt is the target's own response to a side effect -- a charge
        confirmation, a message id, whatever the integration returned -- so it is
        the same class of content as a run checkpoint and is protected the same
        way. Passthrough when no encrypted_field is configured, matching
        ``CheckpointStore``.
        """
        encrypted_field = getattr(self.database, "encrypted_field", None)
        if encrypted_field is None or receipt is None:
            return receipt
        return encrypted_field.encrypt(receipt)

    def _decrypt(self, receipt: str | None) -> str | None:
        """Reverse of :meth:`_encrypt`; passthrough when no encrypted_field."""
        encrypted_field = getattr(self.database, "encrypted_field", None)
        if encrypted_field is None or receipt is None:
            return receipt
        return encrypted_field.decrypt(receipt)

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

    @persistence_operation(ResourceOperation.READ)
    async def get(self, operation_key: str) -> dict[str, Any] | None:
        """Return the stored record, with ``dedupe_supported`` derived."""
        row = await self.table.select_one(where={"operation_key": operation_key})
        if row is None:
            return None
        record = dict(row)
        record["receipt"] = self._decrypt(record.get("receipt"))
        record["dedupe_supported"] = record["support"] != SUPPORT_AT_LEAST_ONCE
        return record

    @persistence_operation(ResourceOperation.READ)
    async def state_of(self, operation_key: str) -> OperationState:
        record = await self.get(operation_key)
        if record is None:
            return OperationState.NOT_STARTED
        return OperationState(record["state"])

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def pending_reconciliation(self, run_id: str) -> list[dict[str, Any]]:
        """Ambiguous operations for a run -- durable work, not an in-memory flag."""
        async with self.table.transaction() as operations:
            rows = await operations.select(
                where={"run_id": run_id, "state": OperationState.AMBIGUOUS.value},
                order_by=("created_at",),
            )
        return [dict(row) | {"receipt": self._decrypt(row["receipt"])} for row in rows]

    # -----------------------------------------------------------------------
    # Claim
    # -----------------------------------------------------------------------

    @persistence_operation(
        ResourceOperation.CREATE, ResourceOperation.READ, ResourceOperation.UPDATE
    )
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
        async with self.table.transaction(write_lock=True) as operations:
            row = await operations.select_one(where={"operation_key": operation_key})

            if row is None:
                # ON CONFLICT DO NOTHING ... RETURNING is the compare-and-set:
                # a concurrent claimer that lost the race gets no row back
                # instead of a uniqueness error, and only the winner is told it
                # may perform the effect.
                won = await operations.insert_if_absent(
                    {
                        "operation_key": operation_key,
                        "run_id": run_id,
                        "dispatch_id": dispatch_id,
                        "idempotency_key": idempotency_key,
                        "target_ref": target_ref,
                        "attempt": attempt,
                        "state": OperationState.IN_FLIGHT.value,
                        "support": support,
                        "reconciliation_attempts": 0,
                        "created_at": now,
                        "updated_at": now,
                    },
                    conflict_columns=("tenant_id", "workspace_scope", "operation_key"),
                )
                if won:
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
                # Replay suppression: the effect is known to have landed. The
                # receipt is decrypted here because the caller replays it as
                # JSON -- returning the raw column hands ciphertext to
                # json.loads whenever encryption is configured.
                self._count("zeroth_side_effect_replay_suppressed_total")
                return OperationClaim(
                    state=state,
                    first_execution=False,
                    receipt=self._decrypt(row["receipt"]),
                    attempts=attempts,
                )

            if state is OperationState.FAILED:
                # Guarded on the observed state so two claimers retrying the
                # same failed operation cannot both be authorised.
                won = await operations.update_if_matches(
                    {
                        "state": OperationState.IN_FLIGHT.value,
                        "attempt": attempt,
                        "error": None,
                        "updated_at": now,
                    },
                    where={
                        "operation_key": operation_key,
                        "state": OperationState.FAILED.value,
                    },
                    returning="operation_key",
                )
                if won:
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
                await operations.update_if_matches(
                    {
                        "state": OperationState.AMBIGUOUS.value,
                        "attempt": attempt,
                        "ambiguity_reason": (
                            "claimed while a previous attempt was still in flight"
                        ),
                        "updated_at": now,
                    },
                    where={
                        "operation_key": operation_key,
                        "state": OperationState.IN_FLIGHT.value,
                    },
                    returning="operation_key",
                )
                # The guard may have refused because the in-flight attempt
                # settled first -- either way. Report what is actually stored
                # rather than asserting an ambiguity we no longer have.
                settled = await operations.select_one(
                    where={"operation_key": operation_key},
                    columns=("state", "receipt"),
                )
                settled_state = None if settled is None else OperationState(settled["state"])
                if settled_state is OperationState.COMPLETED:
                    self._count("zeroth_side_effect_replay_suppressed_total")
                    return OperationClaim(
                        state=OperationState.COMPLETED,
                        first_execution=False,
                        receipt=self._decrypt(settled["receipt"]),
                        attempts=attempts,
                    )
                if settled_state is OperationState.FAILED:
                    # A concurrent fail() won. That is a *confirmed* outcome, so
                    # calling it ambiguous would invent uncertainty and send the
                    # caller down a reconciliation path with nothing to resolve.
                    return OperationClaim(
                        state=OperationState.FAILED,
                        first_execution=False,
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

    @persistence_operation(ResourceOperation.UPDATE)
    async def complete(self, operation_key: str, *, receipt: str) -> bool:
        """Store the result.  Returns False if a result was already stored.

        The first COMPLETED result wins.  Letting a later report overwrite it
        would make the stored outcome depend on arrival order between two
        workers that both believe they own the operation.
        """
        now = _utc_now()
        async with self.table.transaction(write_lock=True) as operations:
            # One guarded statement, not read-then-write. The guard is the
            # state itself, so exactly one of two concurrent completers gets a
            # row back -- the earlier version read the state and then updated
            # separately, which let both observe a non-completed row.
            won = await operations.update_if_matches(
                {
                    "state": OperationState.COMPLETED.value,
                    "receipt": self._encrypt(receipt),
                    "error": None,
                    "updated_at": now,
                },
                where={"operation_key": operation_key},
                where_not_in={"state": (OperationState.COMPLETED.value,)},
                returning="operation_key",
            )
        return won

    @persistence_operation(ResourceOperation.UPDATE)
    async def fail(self, operation_key: str, *, error: str) -> None:
        """Record a *confirmed* failure.

        Neither a COMPLETED nor an AMBIGUOUS operation is overwritten. Completed
        is obvious. Ambiguous is the subtler one: it means an earlier attempt may
        have applied the effect and nobody can say, so demoting it to FAILED
        would assert it did not happen -- and would discard the durable
        reconciliation work that exists precisely because the answer is unknown.
        """
        now = _utc_now()
        async with self.table.transaction(write_lock=True) as operations:
            await operations.update_if_matches(
                {
                    "state": OperationState.FAILED.value,
                    "error": error,
                    "updated_at": now,
                },
                where={"operation_key": operation_key},
                where_not_in={
                    "state": (
                        OperationState.COMPLETED.value,
                        OperationState.AMBIGUOUS.value,
                    )
                },
                returning="operation_key",
            )

    @persistence_operation(ResourceOperation.UPDATE)
    async def mark_ambiguous(self, operation_key: str, *, reason: str) -> None:
        """Record that the outcome is unknown -- distinct from known-failed."""
        self._count("zeroth_side_effect_ambiguous_total")
        now = _utc_now()
        async with self.table.transaction(write_lock=True) as operations:
            await operations.update_if_matches(
                {
                    "state": OperationState.AMBIGUOUS.value,
                    "ambiguity_reason": reason,
                    "updated_at": now,
                },
                where={"operation_key": operation_key},
                where_not_in={"state": (OperationState.COMPLETED.value,)},
                returning="operation_key",
            )

    @persistence_operation(ResourceOperation.UPDATE)
    async def begin_outcome_lookup(self, operation_key: str) -> bool:
        """Claim the operation's single automatic outcome lookup.

        The reconciliation counter is the durable once-token.  Claiming it
        before calling the external integration prevents two workers from both
        asking and then independently acting on an unknown result.
        """
        now = _utc_now()
        async with self.table.transaction(write_lock=True) as operations:
            return await operations.update_if_matches(
                {"updated_at": now},
                where={
                    "operation_key": operation_key,
                    "state": OperationState.AMBIGUOUS.value,
                    "reconciliation_attempts": 0,
                },
                increment=("reconciliation_attempts",),
                returning="operation_key",
            )

    @persistence_operation(ResourceOperation.READ, ResourceOperation.UPDATE)
    async def finish_outcome_lookup(
        self,
        operation_key: str,
        *,
        receipt: str | None,
        error: str | None,
    ) -> OperationState:
        """Persist the result of the already-counted automatic lookup."""
        now = _utc_now()
        async with self.table.transaction(write_lock=True) as operations:
            if receipt is not None:
                settled = await operations.update_if_matches(
                    {
                        "state": OperationState.COMPLETED.value,
                        "receipt": self._encrypt(receipt),
                        "error": None,
                        "updated_at": now,
                    },
                    where={
                        "operation_key": operation_key,
                        "state": OperationState.AMBIGUOUS.value,
                    },
                    returning="operation_key",
                )
                if settled:
                    self._count("zeroth_side_effect_reconciliation_succeeded_total")
                    return OperationState.COMPLETED
            else:
                await operations.update_if_matches(
                    {"error": error, "updated_at": now},
                    where={
                        "operation_key": operation_key,
                        "state": OperationState.AMBIGUOUS.value,
                    },
                    returning="operation_key",
                )
        if receipt is None:
            self._count("zeroth_side_effect_reconciliation_failed_total")
        return await self.state_of(operation_key)

    @persistence_operation(ResourceOperation.UPDATE)
    async def resolve_ambiguous(
        self,
        operation_key: str,
        *,
        state: OperationState,
        reason: str,
        receipt: str | None = None,
    ) -> bool:
        """Apply an explicit operator verdict to an ambiguous operation only."""
        if state not in {OperationState.COMPLETED, OperationState.FAILED}:
            raise ValueError("operator resolution must be COMPLETED or FAILED")
        now = _utc_now()
        values: dict[str, Any] = {
            "state": state.value,
            "error": None if state is OperationState.COMPLETED else reason,
            "ambiguity_reason": reason,
            "updated_at": now,
        }
        if state is OperationState.COMPLETED:
            values["receipt"] = self._encrypt(receipt)
        async with self.table.transaction(write_lock=True) as operations:
            return await operations.update_if_matches(
                values,
                where={
                    "operation_key": operation_key,
                    "state": OperationState.AMBIGUOUS.value,
                },
                returning="operation_key",
            )

    @persistence_operation(ResourceOperation.READ, ResourceOperation.UPDATE)
    async def record_reconciliation(
        self,
        operation_key: str,
        *,
        resolved: bool,
        receipt: str | None = None,
        error: str | None = None,
        confirmed_failed: bool = False,
    ) -> OperationState:
        """Fold one reconciliation attempt into the record.

        ``confirmed_failed`` is the settle path for the opposite discovery: the
        integration was asked and answered that the effect did **not** land.
        Without it an ambiguous operation could never reach FAILED -- ``fail()``
        refuses to demote AMBIGUOUS, and reconciliation could only resolve to
        COMPLETED -- so a genuinely failed operation wedged as ambiguous forever.
        The distinction that matters is *who* is asserting: an unresolved attempt
        still knows nothing, whereas this is the target's own answer.

        An unresolved attempt burns budget and leaves the operation AMBIGUOUS --
        exhausting the budget must not manufacture a verdict, because the runtime
        still does not know what the external system did.
        """
        now = _utc_now()
        async with self.table.transaction(write_lock=True) as operations:
            if resolved:
                # Guarded on "not already completed", so the first reconciler to
                # discover the outcome wins and a later one cannot overwrite it.
                won = await operations.update_if_matches(
                    {
                        "state": OperationState.COMPLETED.value,
                        "receipt": self._encrypt(receipt),
                        "error": None,
                        "updated_at": now,
                    },
                    where={"operation_key": operation_key},
                    where_not_in={"state": (OperationState.COMPLETED.value,)},
                    increment=("reconciliation_attempts",),
                    returning="operation_key",
                )
                if won:
                    self._count("zeroth_side_effect_reconciliation_succeeded_total")
                    return OperationState.COMPLETED
                # Either it was already completed, or the row is gone.
                return await self.state_of(operation_key)

            if confirmed_failed:
                # The target answered "no effect". That is knowledge, not a
                # timeout, so it may settle an ambiguous operation.
                settled = await operations.update_if_matches(
                    {
                        "state": OperationState.FAILED.value,
                        "error": error,
                        "updated_at": now,
                    },
                    where={"operation_key": operation_key},
                    where_not_in={"state": (OperationState.COMPLETED.value,)},
                    increment=("reconciliation_attempts",),
                    returning="operation_key",
                )
                if settled:
                    self._count("zeroth_side_effect_reconciliation_succeeded_total")
                    return OperationState.FAILED
                return await self.state_of(operation_key)

            # An unresolved attempt burns budget and leaves the operation
            # AMBIGUOUS. Exhausting the budget must not manufacture a verdict:
            # the runtime still does not know what the external system did.
            moved = await operations.update_if_matches(
                {
                    "state": OperationState.AMBIGUOUS.value,
                    "error": error,
                    "updated_at": now,
                },
                where={"operation_key": operation_key},
                where_not_in={"state": (OperationState.COMPLETED.value,)},
                increment=("reconciliation_attempts",),
                returning="operation_key",
            )
        if not moved:
            return await self.state_of(operation_key)
        self._count("zeroth_side_effect_reconciliation_failed_total")
        return OperationState.AMBIGUOUS

    @persistence_operation(ResourceOperation.DELETE, ResourceOperation.ENUMERATE)
    async def erase_for_run(self, run_id: str) -> int:
        """Delete this scope's receipts for one run."""
        async with self.table.transaction(write_lock=True) as operations:
            return await self.erase_for_run_in_transaction(operations, run_id)

    @persistence_operation(ResourceOperation.DELETE, ResourceOperation.ENUMERATE)
    async def erase_for_run_in_transaction(
        self,
        transaction: BoundStructuredTable,
        run_id: str,
    ) -> int:
        """Delete this scope's receipts inside a caller-owned transaction."""
        operations = self.table.in_transaction(transaction)
        rows = await operations.select(where={"run_id": run_id}, columns=("operation_key",))
        if rows:
            await operations.delete(where={"run_id": run_id})
        return len(rows)


def record_is_dedupe_supported(record: Mapping[str, Any]) -> bool:
    """Whether a stored record's target can collapse a repeat."""
    return record["support"] != SUPPORT_AT_LEAST_ONCE
