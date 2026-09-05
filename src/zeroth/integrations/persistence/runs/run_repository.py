"""Concrete SQL persistence for runs.

The low-level store owns the raw ``runs`` and ``threads`` reads and writes and
the transaction boundaries around them; :class:`RunRepository` layers status
transitions, history recording, and checkpoint management on top.

The store deliberately spans both tables. Writing a checkpoint updates the
thread record, and registering a run creates its thread, so a run-only store
would have to reach across a boundary on nearly every write. The pieces that
*could* be separated without moving a transaction — row conversion, the
``run_checkpoints`` table, and the retention queries — live in their own
modules.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshot
from zeroth.governance.guardrails.policy import EffectiveGuardrailSettings
from zeroth.governance.guardrails.rate_limit import (
    QuotaDecision,
    QuotaEnforcer,
    TokenBucketRateLimiter,
    guardrail_identity_key,
)
from zeroth.integrations.persistence.runs import retention_queries
from zeroth.integrations.persistence.runs.checkpoint_store import (
    CheckpointRowStore,
)
from zeroth.integrations.persistence.runs.checkpoint_store import (
    new_checkpoint_id as _new_checkpoint_id,
)
from zeroth.integrations.persistence.runs.serialization import (
    dump_list as _dump_list,
)
from zeroth.integrations.persistence.runs.serialization import (
    dump_model as _dump_model,
)
from zeroth.integrations.persistence.runs.serialization import (
    row_to_run,
    row_to_thread,
)
from zeroth.integrations.persistence.runs.token_snapshot_store import (
    TokenSnapshotRowStore,
)
from zeroth.platform.dispatch.lease import FencedRunWriteRejectedError
from zeroth.platform.primitives import utc_now
from zeroth.platform.storage import (
    SERVICE_SCOPE_REGISTRY,
    AsyncConnection,
    AsyncDatabase,
    NullWorkspaceScopeContext,
    ScopeContext,
    ScopedTable,
)
from zeroth.platform.storage.json import to_json_value
from zeroth.platform.storage.scoped_table import BoundStructuredTable
from zeroth.platform.storage.scoping import (
    ResourceOperation,
    named_isolation_probe,
    persistence_operation,
    persistence_surface,
)
from zeroth.runtime.runs import (
    Run,
    RunConditionResult,
    RunFailureState,
    RunHistoryEntry,
    RunStatus,
    Thread,
    ThreadMemoryBinding,
    ThreadStatus,
)

ALLOWED_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    RunStatus.PENDING: {RunStatus.RUNNING, RunStatus.FAILED},
    RunStatus.RUNNING: {
        RunStatus.WAITING_APPROVAL,
        RunStatus.WAITING_INTERRUPT,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
    },
    RunStatus.WAITING_APPROVAL: {
        RunStatus.PENDING,  # durable worker re-queue after approval resolution
        RunStatus.RUNNING,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
    },
    RunStatus.WAITING_INTERRUPT: {
        RunStatus.RUNNING,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
    },
    RunStatus.COMPLETED: set(),
    RunStatus.FAILED: {RunStatus.PENDING},  # operator replay from dead-letter
}

# Sentinel stored in failure_state.reason to mark dead-letter runs.
DEAD_LETTER_REASON = "dead_letter"
_UNSCOPED_WORKSPACE = object()

# A02-12: defensive ceiling for list_runs, independent of any route-level bound.
_LIST_RUNS_MAX_LIMIT = 1000
# Queue depth has no completion-rate signal, so it must not translate a count
# of excess batches into seconds. One second is the explicit control-plane
# recheck interval; rate and quota decisions retain their state-derived waits.
_QUEUE_RETRY_INTERVAL_SECONDS = 1


class GuardrailAdmissionRejectedError(RuntimeError):
    """A shared ingress decision refused a run without consuming capacity."""

    def __init__(
        self,
        reason: str,
        *,
        retry_after_seconds: int,
        utilization: float,
    ) -> None:
        super().__init__(f"guardrail admission rejected: {reason}")
        self.reason = reason
        self.retry_after_seconds = max(1, retry_after_seconds)
        self.utilization = max(0, min(1, utilization))


def _guardrail_key(kind: str, run: Run) -> str:
    subject = None if run.submitted_by is None else run.submitted_by.subject
    return guardrail_identity_key(
        kind,
        tenant_id=run.tenant_id,
        workspace_id=run.workspace_id,
        deployment_ref=run.deployment_ref,
        subject=subject,
    )


async def _quota_decision(
    runs: BoundStructuredTable,
    run: Run,
    settings: EffectiveGuardrailSettings,
    enforcer: QuotaEnforcer,
) -> QuotaDecision | None:
    limit = settings.quota_daily_limit
    if limit is None:
        return None
    return await enforcer._decide_bound(  # noqa: SLF001 - shared transaction seam
        runs.bind(enforcer.table),
        _guardrail_key("daily-quota", run),
        limit=limit,
        window_seconds=86_400,
    )


def _validate_transition(current: RunStatus, new: RunStatus) -> None:
    """Check that moving from one run status to another is valid.

    Raises ValueError if the transition is not allowed (for example,
    you can't go from COMPLETED back to RUNNING).
    """
    if new == current:
        return
    if new not in ALLOWED_TRANSITIONS[current]:
        msg = f"invalid run transition: {current.value} -> {new.value}"
        raise ValueError(msg)


def _merge(existing: list[str], updates: Sequence[str] | None) -> list[str]:
    """Merge new string items into an existing list, skipping duplicates."""
    merged = list(existing)
    for item in updates or []:
        if item not in merged:
            merged.append(item)
    return merged


def _merge_models(
    existing: list[ThreadMemoryBinding],
    updates: Sequence[ThreadMemoryBinding] | None,
) -> list[ThreadMemoryBinding]:
    """Merge new ThreadMemoryBinding items into an existing list, skipping duplicates."""
    merged = list(existing)
    for item in updates or []:
        if item not in merged:
            merged.append(item)
    return merged


def _run_values(run: Run) -> dict[str, object]:
    return {
        "run_id": run.run_id,
        "checkpoint_id": run.checkpoint_id,
        "parent_checkpoint_id": run.parent_checkpoint_id,
        "epoch": run.epoch,
        "workflow_name": run.workflow_name,
        "status": run.status.value,
        "current_step": run.current_step,
        "completed_steps": to_json_value(run.completed_steps),
        "artifacts": to_json_value(run.artifacts),
        "channels": to_json_value(run.channels),
        "pending_approval": _dump_model(run.pending_approval),
        "pending_interrupt_id": run.pending_interrupt_id,
        "started_at": run.started_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
        "error": run.error,
        "metadata": to_json_value(run.metadata),
        "graph_version_ref": run.graph_version_ref,
        "deployment_ref": run.deployment_ref,
        "parent_run_id": run.parent_run_id,
        "submitted_by": _dump_model(run.submitted_by),
        "thread_id": run.thread_id,
        "current_node_ids": to_json_value(run.current_node_ids),
        "pending_node_ids": to_json_value(run.pending_node_ids),
        "execution_history": _dump_list(run.execution_history),
        "node_visit_counts": to_json_value(run.node_visit_counts),
        "condition_results": _dump_list(run.condition_results),
        "audit_refs": to_json_value(run.audit_refs),
        "final_output": _dump_model(run.final_output),
        "failure_state": _dump_model(run.failure_state),
    }


def _thread_values(thread: Thread) -> dict[str, object]:
    return {
        "thread_id": thread.thread_id,
        "graph_version_ref": thread.graph_version_ref,
        "deployment_ref": thread.deployment_ref,
        "status": thread.status.value,
        "participating_agent_refs": to_json_value(thread.participating_agent_refs),
        "state_snapshot_refs": to_json_value(thread.state_snapshot_refs),
        "checkpoint_refs": to_json_value(thread.checkpoint_refs),
        "memory_bindings": _dump_list(thread.memory_bindings),
        "run_ids": to_json_value(thread.run_ids),
        "active_run_id": thread.active_run_id,
        "last_run_id": thread.last_run_id,
        "created_at": thread.created_at.isoformat(),
        "updated_at": thread.updated_at.isoformat(),
    }


@persistence_surface(
    "service.guardrail_admission_state",
    probe=named_isolation_probe("_drive_guardrail_admission_state"),
)
@dataclass(slots=True)
class GuardrailAdmissionCoordinator:
    """Serialize ingress and lease decisions for one trusted tenant scope."""

    database: AsyncDatabase
    scope_context: ScopeContext | NullWorkspaceScopeContext
    table: ScopedTable = field(init=False)

    def __post_init__(self) -> None:
        self.table = ScopedTable(
            self.database,
            SERVICE_SCOPE_REGISTRY,
            "service.guardrail_admission_state",
            self.scope_context,
        )

    @persistence_operation(
        ResourceOperation.CREATE,
        ResourceOperation.READ,
    )
    async def coordinate(
        self,
        deployment_ref: str,
        *,
        transaction: BoundStructuredTable | None = None,
    ) -> None:
        """Create and lock a deployment's shared coordination row."""
        if transaction is not None:
            await self._coordinate_bound(transaction.bind(self.table), deployment_ref)
            return
        async with self.table.transaction(write_lock=True) as admission:
            await self._coordinate_bound(admission, deployment_ref)

    @persistence_operation(ResourceOperation.READ)
    async def exists(self, deployment_ref: str) -> bool:
        """Report whether one coordination row exists in this exact scope."""
        async with self.table.transaction() as admission:
            row = await admission.select_one(
                where={"deployment_ref": deployment_ref}, columns=("deployment_ref",)
            )
        return row is not None

    async def _coordinate_bound(
        self,
        admission: BoundStructuredTable,
        deployment_ref: str,
    ) -> None:
        now = utc_now().isoformat()
        await admission.insert_if_absent(
            {"deployment_ref": deployment_ref, "created_at": now},
            conflict_columns=("tenant_id", "workspace_scope", "deployment_ref"),
        )
        await admission.select_one(where={"deployment_ref": deployment_ref}, for_update=True)


@dataclass(slots=True)
class _RunThreadStore:
    """Low-level async store that handles raw read/write operations for runs and threads.

    This is an internal class used by RunRepository and ThreadRepository.
    It owns the database reference.
    """

    database: AsyncDatabase
    scope_context: ScopeContext | NullWorkspaceScopeContext
    checkpoints: CheckpointRowStore = field(init=False)
    runs: ScopedTable = field(init=False)
    threads: ScopedTable = field(init=False)
    token_snapshots: ScopedTable = field(init=False)
    admission_state: GuardrailAdmissionCoordinator = field(init=False)
    # ZER-26/AUD-004: per-run write fences. While a fence is installed for a
    # run id, every save of that run's row carries the live lease predicate, so
    # a displaced or expired worker's write is refused *inside* the statement
    # rather than by a check that races it.
    _fences: dict[str, tuple[str, int]] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        """Bind the ``run_checkpoints`` adapter to the same database."""
        self.checkpoints = CheckpointRowStore(self.database, self.scope_context)
        self.runs = ScopedTable(
            self.database, SERVICE_SCOPE_REGISTRY, "service.runs", self.scope_context
        )
        self.threads = ScopedTable(
            self.database, SERVICE_SCOPE_REGISTRY, "service.threads", self.scope_context
        )
        self.token_snapshots = ScopedTable(
            self.database,
            SERVICE_SCOPE_REGISTRY,
            "service.token_engine_snapshots",
            self.scope_context,
        )
        self.admission_state = GuardrailAdmissionCoordinator(self.database, self.scope_context)

    def validate_owner(self, tenant_id: str, workspace_id: str | None) -> None:
        if tenant_id != self.scope_context.tenant_id:
            raise ValueError("tenant_id does not match bound scope")
        expected_workspace = (
            self.scope_context.workspace_id if type(self.scope_context) is ScopeContext else None
        )
        if workspace_id != expected_workspace:
            raise ValueError("workspace_id does not match bound scope")

    def install_fence(self, run_id: str, worker_id: str, generation: int) -> None:
        """Fence every subsequent save of this run on (worker_id, generation)."""
        self._fences[run_id] = (worker_id, generation)

    def clear_fence(self, run_id: str, worker_id: str, generation: int) -> bool:
        """Remove only the exact generation's write fence."""
        if self._fences.get(run_id) != (worker_id, generation):
            return False
        del self._fences[run_id]
        return True

    async def save_run(self, run: Run) -> None:
        """Insert or update a run record in the database.

        When a fence is installed for this run, persistence is update-only and
        carries the owner, generation, and database-time expiry predicates. No
        row back means ownership moved or expired and the write was refused,
        which raises :class:`FencedRunWriteRejectedError`.
        """
        self.validate_owner(run.tenant_id, run.workspace_id)
        fence = self._fences.get(run.run_id)
        async with self.runs.transaction() as runs:
            await self._save_run_bound(runs, run, fence)

    async def _save_run_bound(
        self,
        runs: BoundStructuredTable,
        run: Run,
        fence: tuple[str, int] | None,
        *,
        insert_only: bool = False,
        expected_status: RunStatus | None = None,
    ) -> None:
        values = _run_values(run)
        if insert_only:
            written = await runs.insert_if_absent(
                values,
                conflict_columns=("tenant_id", "workspace_scope", "run_id"),
            )
        elif fence is not None or expected_status is not None:
            where: dict[str, object] = {"run_id": run.run_id}
            if fence is not None:
                where.update(
                    lease_worker_id=fence[0],
                    lease_generation=fence[1],
                )
            if expected_status is not None:
                where["status"] = expected_status.value
            written = await runs.update_if_matches(
                {column: value for column, value in values.items() if column != "run_id"},
                where=where,
                returning="run_id",
                where_gte_database_now=("lease_expires_at",) if fence is not None else (),
            )
        else:
            written = await runs.upsert(
                values,
                conflict_columns=("tenant_id", "workspace_scope", "run_id"),
                update_columns=tuple(
                    column for column in values if column not in {"run_id", "workspace_scope"}
                ),
                returning="run_id",
            )
        if insert_only and not written:
            raise KeyError(f"run already exists: {run.run_id}")
        if fence is not None and not written:
            worker_id, generation = fence
            if expected_status is not None:
                # A status CAS refusal alone does not imply lease loss. Recheck
                # the fence under a row lock without retrying the refused write.
                current = await runs.select_one(
                    where={
                        "run_id": run.run_id,
                        "lease_worker_id": worker_id,
                        "lease_generation": generation,
                    },
                    columns=("lease_expires_at",),
                    for_update=True,
                )
                if current is not None:
                    try:
                        expires_at = datetime.fromisoformat(current["lease_expires_at"])
                    except (TypeError, ValueError):
                        expires_at = None
                    now = await runs._database_now()  # noqa: SLF001 - bound transaction clock
                    if (
                        expires_at is not None
                        and expires_at.tzinfo is not None
                        and expires_at >= now
                    ):
                        raise ValueError(
                            f"run {run.run_id!r} no longer has status {expected_status.value}"
                        )
            raise FencedRunWriteRejectedError(run.run_id, worker_id, generation)
        if expected_status is not None and not written:
            raise ValueError(f"run {run.run_id!r} no longer has status {expected_status.value}")

    async def create_run(
        self,
        run: Run,
        *,
        settings: EffectiveGuardrailSettings | None = None,
        rate_limiter: TokenBucketRateLimiter | None = None,
        quota_enforcer: QuotaEnforcer | None = None,
    ) -> None:
        """Atomically insert a new run and its thread/checkpoint state."""
        self.validate_owner(run.tenant_id, run.workspace_id)
        checkpoint_id = run.checkpoint_id or _new_checkpoint_id()
        run.checkpoint_id = checkpoint_id
        run.touch()
        async with self.runs.transaction(write_lock=True) as runs:
            if settings is not None:
                if rate_limiter is None or quota_enforcer is None:
                    raise ValueError("guarded admission requires rate and quota enforcers")
                await self._guarded_admission_bound(
                    runs,
                    run,
                    settings,
                    rate_limiter,
                    quota_enforcer,
                )
            await self._insert_run_state_bound(runs, run, checkpoint_id)

    async def _guarded_admission_bound(
        self,
        runs: BoundStructuredTable,
        run: Run,
        settings: EffectiveGuardrailSettings,
        rate_limiter: TokenBucketRateLimiter,
        quota_enforcer: QuotaEnforcer,
    ) -> None:
        """Reserve queue, rate, and quota capacity under one replica-shared lock."""
        await self.admission_state.coordinate(run.deployment_ref, transaction=runs)
        pending = await runs.count(
            where={"deployment_ref": run.deployment_ref, "status": RunStatus.PENDING.value}
        )
        if pending >= settings.backpressure_queue_depth:
            raise GuardrailAdmissionRejectedError(
                "queue",
                retry_after_seconds=_QUEUE_RETRY_INTERVAL_SECONDS,
                utilization=pending / settings.backpressure_queue_depth,
            )
        rate = await rate_limiter._decide_bound(  # noqa: SLF001 - shared transaction seam
            runs.bind(rate_limiter.table),
            _guardrail_key("token-bucket", run),
            capacity=settings.bucket_capacity,
            refill_rate=settings.rate_limit_refill_rate,
        )
        if not rate.allowed:
            raise GuardrailAdmissionRejectedError(
                "rate",
                retry_after_seconds=rate.retry_after_seconds,
                utilization=rate.utilization,
            )
        quota = await _quota_decision(runs, run, settings, quota_enforcer)
        if quota is not None and not quota.allowed:
            raise GuardrailAdmissionRejectedError(
                "quota",
                retry_after_seconds=quota.retry_after_seconds,
                utilization=quota.utilization,
            )
        run.metadata["guardrail_admission"] = {
            "queue_depth": pending + 1,
            "queue_utilization": (pending + 1) / settings.backpressure_queue_depth,
            "rate_utilization": rate.utilization,
            "quota_utilization": None if quota is None else quota.utilization,
        }

    async def _insert_run_state_bound(
        self,
        runs: BoundStructuredTable,
        run: Run,
        checkpoint_id: str,
    ) -> None:
        """Insert the run, thread, and initial checkpoint in one transaction."""
        await self._save_run_bound(runs, run, None, insert_only=True)
        threads = runs.bind(self.threads)
        seed = Thread(
            thread_id=run.thread_id,
            graph_version_ref=run.graph_version_ref,
            deployment_ref=run.deployment_ref,
            tenant_id=run.tenant_id,
            workspace_id=run.workspace_id,
            status=ThreadStatus.ACTIVE,
        )
        await threads.insert_if_absent(
            _thread_values(seed),
            conflict_columns=("tenant_id", "workspace_scope", "thread_id"),
        )
        row = await threads.select_one(where={"thread_id": run.thread_id}, for_update=True)
        if row is None:  # pragma: no cover - insert/read transaction contract
            raise RuntimeError("thread row unavailable after scoped insert")
        thread = row_to_thread(row)
        if thread.tenant_id != run.tenant_id or thread.workspace_id != run.workspace_id:
            raise ValueError("thread identity mismatch")
        thread.run_ids = _merge(thread.run_ids, [run.run_id])
        thread.last_run_id = run.run_id
        checkpoint_order = len(thread.checkpoint_refs)
        thread.checkpoint_refs = _merge(thread.checkpoint_refs, [checkpoint_id])
        thread.updated_at = utc_now()
        await self._save_thread_bound(threads, thread)
        await self.checkpoints.write_row_bound(
            runs.bind(self.checkpoints.table),
            checkpoint_id=checkpoint_id,
            run_id=run.run_id,
            thread_id=run.thread_id,
            checkpoint_order=checkpoint_order,
            state_json=to_json_value(run.model_dump(mode="json")),
            created_at=run.updated_at.isoformat(),
        )

    async def save_thread(self, thread: Thread) -> None:
        """Insert or update a thread record in the database."""
        self.validate_owner(thread.tenant_id, thread.workspace_id)
        async with self.threads.transaction() as threads:
            await self._save_thread_bound(threads, thread)

    async def create_thread(self, thread: Thread) -> None:
        """Insert a thread without allowing a global-ID ownership overwrite."""
        self.validate_owner(thread.tenant_id, thread.workspace_id)
        async with self.threads.transaction(write_lock=True) as threads:
            await self._save_thread_bound(threads, thread, insert_only=True)

    async def _save_thread_bound(
        self,
        threads: BoundStructuredTable,
        thread: Thread,
        *,
        insert_only: bool = False,
    ) -> None:
        values = _thread_values(thread)
        if insert_only:
            written = await threads.insert_if_absent(
                values,
                conflict_columns=("tenant_id", "workspace_scope", "thread_id"),
            )
        else:
            written = await threads.upsert(
                values,
                conflict_columns=("tenant_id", "workspace_scope", "thread_id"),
                update_columns=tuple(
                    column
                    for column in values
                    if column not in {"thread_id", "workspace_scope", "created_at"}
                ),
                returning="thread_id",
            )
        if insert_only and not written:
            raise KeyError(f"thread already exists: {thread.thread_id}")

    async def get_run(
        self,
        run_id: str,
    ) -> Run | None:
        """Load a run from the database by its ID, or return None if not found.

        WS-B: when ``tenant_id`` is supplied, a run owned by another tenant is
        invisible (returns ``None``). ``None`` = no tenant filter, the default
        for internal orchestrator lookups; API read paths pass the principal's
        tenant as defense-in-depth atop their existing scope checks.
        """
        row = await self.runs.select_one(where={"run_id": run_id})
        if row is None:
            return None
        return row_to_run(row)

    async def merge_terminal_metadata(self, run_id: str, patch: dict[str, Any]) -> Run:
        """Merge annotation metadata without rewriting execution state.

        Review verdicts interpret an already-terminal outcome. They must not
        advance the run timestamp, thread, checkpoint, or replay state. The
        terminal check and metadata-only update share one write transaction so
        a failed run cannot be requeued between eligibility and annotation.
        """
        if not patch:
            raise ValueError("metadata patch must not be empty")
        async with self.runs.transaction(write_lock=True) as runs:
            row = await runs.select_one(where={"run_id": run_id}, for_update=True)
            if row is None:
                raise KeyError(run_id)
            run = row_to_run(row)
            if run.status not in {RunStatus.COMPLETED, RunStatus.FAILED}:
                raise ValueError("run is not terminal yet")
            merged = {**(run.metadata or {}), **patch}
            await runs.update(
                {"metadata": to_json_value(merged)},
                where={"run_id": run_id},
            )
        persisted = await self.get_run(run_id)
        if persisted is None:  # pragma: no cover - row is locked through update
            raise KeyError(run_id)
        return persisted

    async def get_thread(
        self,
        thread_id: str,
    ) -> Thread | None:
        """Load a thread, optionally requiring its tenant/workspace in SQL."""
        row = await self.threads.select_one(where={"thread_id": thread_id})
        return None if row is None else row_to_thread(row)

    async def delete_run(self, run_id: str) -> None:
        """Remove a run from the database and update its parent thread."""
        run = await self.get_run(run_id)
        if run is None:
            return
        async with self.runs.transaction(write_lock=True) as runs:
            await runs.bind(self.token_snapshots).delete(where={"run_id": run_id})
            await runs.delete(where={"run_id": run_id})
        thread = await self.get_thread(run.thread_id)
        if thread is None:
            return
        thread.run_ids = [current for current in thread.run_ids if current != run_id]
        if thread.active_run_id == run_id:
            thread.active_run_id = None
        if thread.last_run_id == run_id:
            thread.last_run_id = thread.run_ids[-1] if thread.run_ids else None
        thread.updated_at = utc_now()
        await self.save_thread(thread)

    async def write_checkpoint(self, run: Run) -> str:
        """Save a snapshot of the run's current state as a checkpoint.

        Returns the checkpoint ID. Checkpoints let you restore a run
        to a previous point in its execution. When the underlying database
        was configured with an encryption_key, the serialized state_json is
        encrypted at rest so thread-state checkpoints cannot leak secrets.
        """
        checkpoint_id = run.checkpoint_id or _new_checkpoint_id()
        run.checkpoint_id = checkpoint_id
        self.validate_owner(run.tenant_id, run.workspace_id)
        run.touch()
        snapshot = run.model_dump(mode="json")
        # Lock the thread before deriving order or merging references. The
        # checkpoint and its thread reference must commit or roll back together.
        async with self.threads.transaction(write_lock=True) as threads:
            row = await threads.select_one(where={"thread_id": run.thread_id}, for_update=True)
            was_missing = row is None
            if was_missing:
                seed = Thread(
                    thread_id=run.thread_id,
                    graph_version_ref=run.graph_version_ref,
                    deployment_ref=run.deployment_ref,
                    tenant_id=run.tenant_id,
                    workspace_id=run.workspace_id,
                    status=ThreadStatus.ACTIVE,
                    run_ids=[run.run_id],
                    last_run_id=run.run_id,
                )
                await threads.insert_if_absent(
                    _thread_values(seed),
                    conflict_columns=("tenant_id", "workspace_scope", "thread_id"),
                )
                row = await threads.select_one(
                    where={"thread_id": run.thread_id}, for_update=True
                )
            if row is None:  # pragma: no cover - insert/read transaction contract
                raise RuntimeError("thread row unavailable after scoped insert")
            thread = row_to_thread(row)
            if thread.tenant_id != run.tenant_id or thread.workspace_id != run.workspace_id:
                raise ValueError("thread identity mismatch")
            if was_missing:
                thread.run_ids = _merge(thread.run_ids, [run.run_id])
                thread.last_run_id = run.run_id
            checkpoint_order = len(thread.checkpoint_refs)
            await self.checkpoints.write_row_bound(
                threads.bind(self.checkpoints.table),
                checkpoint_id=checkpoint_id,
                run_id=run.run_id,
                thread_id=run.thread_id,
                checkpoint_order=checkpoint_order,
                state_json=to_json_value(snapshot),
                created_at=run.updated_at.isoformat(),
            )
            thread.checkpoint_refs = _merge(thread.checkpoint_refs, [checkpoint_id])
            thread.updated_at = utc_now()
            await self._save_thread_bound(threads, thread)
        return checkpoint_id

    async def get_checkpoint(self, checkpoint_id: str) -> Run | None:
        """Load a previously saved checkpoint by its ID."""
        return await self.checkpoints.get(checkpoint_id)

    async def get_latest_checkpoint(self, thread_id: str) -> Run | None:
        """Load the most recent checkpoint for a given thread."""
        checkpoint_ids = await self._checkpoint_ids(thread_id)
        if not checkpoint_ids:
            return None
        return await self.get_checkpoint(checkpoint_ids[-1])

    async def list_checkpoints(self, thread_id: str) -> list[Run]:
        """Return all checkpoints for a thread, in order."""
        results: list[Run] = []
        for checkpoint_id in await self._checkpoint_ids(thread_id):
            checkpoint = await self.get_checkpoint(checkpoint_id)
            if checkpoint is not None:
                results.append(checkpoint)
        return results

    async def get_active_run_id(self, thread_id: str) -> str | None:
        """Return the currently active run ID for a thread, or None."""
        thread = await self.get_thread(thread_id)
        return None if thread is None else thread.active_run_id

    async def get_latest_run_id(self, thread_id: str) -> str | None:
        """Return the most recently added run ID for a thread, or None."""
        run_ids = await self.list_run_ids(thread_id)
        return run_ids[-1] if run_ids else None

    async def list_run_ids(self, thread_id: str) -> list[str]:
        """Return all run IDs belonging to a thread."""
        thread = await self.get_thread(thread_id)
        if thread is None:
            return []
        return list(thread.run_ids)

    async def set_active_run_id(
        self,
        thread_id: str,
        run_id: str,
    ) -> None:
        """Mark a run as the active run for its thread."""
        thread = await self.get_thread(thread_id)
        if thread is None:
            raise KeyError(thread_id)
        run = await self.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        thread.active_run_id = run_id
        if run_id not in thread.run_ids:
            thread.run_ids.append(run_id)
        thread.last_run_id = run_id
        thread.updated_at = utc_now()
        await self.save_thread(thread)

    async def clear_active_run_id(self, thread_id: str, run_id: str) -> None:
        """Clear the active run for a thread (only if it matches the given run_id)."""
        thread = await self.get_thread(thread_id)
        if thread is None or thread.active_run_id != run_id:
            return
        thread.active_run_id = None
        thread.updated_at = utc_now()
        await self.save_thread(thread)

    async def put_run(self, run: Run) -> None:
        """Save a run's thread, checkpoint and row, creating the thread if needed.

        ZER-49/A07-16: all three writes share ONE transaction, fenced or not.
        The unfenced path used to open a transaction per write, so a failure of
        the last one — a fence rejection, a constraint violation, a dropped
        connection — left the thread and the checkpoint durable and the runs row
        behind them, and a restore replayed state the run never committed.
        """
        fence = self._fences.get(run.run_id)
        # An unfenced put touches twice, so the checkpoint's created_at stays
        # strictly older than the runs row it precedes; the fenced put has
        # always touched once. Sharing a transaction must not change either.
        await self._put_run_atomic(
            run,
            fence,
            touch_before_run_save=fence is None,
        )

    async def put_run_if_status(self, run: Run, expected_status: RunStatus) -> None:
        """Atomically save a transition only while its observed status is current."""
        fence = self._fences.get(run.run_id)
        await self._put_run_atomic(
            run,
            fence,
            touch_before_run_save=fence is None,
            expected_status=expected_status,
        )

    async def _put_run_atomic(
        self,
        run: Run,
        fence: tuple[str, int] | None,
        *,
        touch_before_run_save: bool,
        expected_status: RunStatus | None = None,
    ) -> None:
        """Write the thread, the checkpoint and the runs row in ONE transaction.

        Nothing lands unless all three do. Under a fence that makes the
        rejection total — a displaced worker used to overwrite durable
        checkpoint state before the fence raised (ZER-26/AUD-004) — and
        unfenced it makes any mid-path failure total too (ZER-49/A07-16).

        ZER-49/F-03: the thread row is read, merged and written back here and
        nowhere else — collapsing the path removed the compensating second
        read-modify-write that used to follow the checkpoint write. One
        transaction therefore has to carry isolation as well as atomicity, or
        two puts of one thread read the same snapshot, each append their own
        checkpoint to it, and the later write drops the earlier reference: the
        checkpoint stays durable in ``run_checkpoints`` while every
        thread-level reader (``_checkpoint_ids``, ``get_latest_checkpoint``,
        ``_next_checkpoint_order``) stops seeing it, and a restore resumes from
        a stale checkpoint. The locking below is ``create_run``'s, statement for
        statement — a write-locked transaction, the thread row seeded before it
        is read so ``for_update`` has a row to lock, and the read taken with
        that lock held.
        """
        self.validate_owner(run.tenant_id, run.workspace_id)
        checkpoint_id = run.checkpoint_id or _new_checkpoint_id()
        run.checkpoint_id = checkpoint_id
        run.touch()
        snapshot = run.model_dump(mode="json")
        async with self.runs.transaction(write_lock=True) as runs:
            threads = runs.bind(self.threads)
            seed = Thread(
                thread_id=run.thread_id,
                graph_version_ref=run.graph_version_ref,
                deployment_ref=run.deployment_ref,
                tenant_id=run.tenant_id,
                workspace_id=run.workspace_id,
                status=ThreadStatus.ACTIVE,
            )
            await threads.insert_if_absent(
                _thread_values(seed),
                conflict_columns=("tenant_id", "workspace_scope", "thread_id"),
            )
            row = await threads.select_one(
                where={"thread_id": run.thread_id},
                for_update=True,
            )
            if row is None:  # pragma: no cover - insert/read transaction contract
                raise RuntimeError("thread row unavailable after scoped insert")
            thread = row_to_thread(row)
            if thread.tenant_id != run.tenant_id or thread.workspace_id != run.workspace_id:
                raise ValueError("thread identity mismatch")
            thread.run_ids = _merge(thread.run_ids, [run.run_id])
            thread.last_run_id = run.run_id
            # Same ordering rule as _next_checkpoint_order: the order is the
            # count of checkpoints BEFORE this one joins the list.
            checkpoint_order = len(thread.checkpoint_refs)
            thread.checkpoint_refs = _merge(thread.checkpoint_refs, [checkpoint_id])
            thread.updated_at = utc_now()
            await self._save_thread_bound(threads, thread)
            await self.checkpoints.write_row_bound(
                runs.bind(self.checkpoints.table),
                checkpoint_id=checkpoint_id,
                run_id=run.run_id,
                thread_id=run.thread_id,
                checkpoint_order=checkpoint_order,
                state_json=to_json_value(snapshot),
                created_at=run.updated_at.isoformat(),
            )
            if touch_before_run_save:
                run.touch()
            await self._save_run_bound(
                runs,
                run,
                fence,
                expected_status=expected_status,
            )

    async def _ensure_thread(self, thread_id: str) -> Thread:
        """Load a thread by ID, raising KeyError if it doesn't exist."""
        thread = await self.get_thread(thread_id)
        if thread is None:
            raise KeyError(thread_id)
        return thread

    async def _record_thread_run(
        self,
        thread_id: str,
        run_id: str,
        graph_version_ref: str,
        deployment_ref: str,
        tenant_id: str,
        workspace_id: str | None,
    ) -> None:
        """Register a run with its thread, creating the thread if needed."""
        self.validate_owner(tenant_id, workspace_id)
        thread = await self.get_thread(thread_id)
        if thread is None:
            thread = Thread(
                thread_id=thread_id,
                graph_version_ref=graph_version_ref,
                deployment_ref=deployment_ref,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                status=ThreadStatus.ACTIVE,
                run_ids=[run_id],
                last_run_id=run_id,
            )
        else:
            if thread.tenant_id != tenant_id or thread.workspace_id != workspace_id:
                raise ValueError("thread identity mismatch")
            thread.run_ids = _merge(thread.run_ids, [run_id])
            thread.last_run_id = run_id
            thread.updated_at = utc_now()
        await self.save_thread(thread)

    async def _record_thread_checkpoint(
        self,
        thread_id: str,
        checkpoint_id: str,
        *,
        tenant_id: str,
        workspace_id: str | None,
    ) -> None:
        """Add a checkpoint reference to a thread's list of checkpoints."""
        self.validate_owner(tenant_id, workspace_id)
        thread = await self.get_thread(thread_id)
        if thread is None:
            return
        thread.checkpoint_refs = _merge(thread.checkpoint_refs, [checkpoint_id])
        thread.updated_at = utc_now()
        await self.save_thread(thread)

    async def _checkpoint_ids(
        self,
        thread_id: str,
    ) -> list[str]:
        """Return all checkpoint IDs for a thread."""
        thread = await self.get_thread(thread_id)
        if thread is None:
            return []
        return list(thread.checkpoint_refs)

    async def _next_checkpoint_order(
        self,
        thread_id: str,
    ) -> int:
        """Return the next checkpoint order number for a thread."""
        return len(await self._checkpoint_ids(thread_id))

    async def get_latest_checkpoint_id_for_run(self, run_id: str) -> str | None:
        """Return the checkpoint_id for the most recent checkpoint of a run."""
        return await self.checkpoints.latest_id_for_run(run_id)

    async def count_pending(self, deployment_ref: str) -> int:
        """Count runs with PENDING status for a deployment."""
        async with self.runs.transaction() as runs:
            return await runs.count(
                where={
                    "status": RunStatus.PENDING.value,
                    "deployment_ref": deployment_ref,
                }
            )

    async def increment_failure_count(self, run_id: str) -> int:
        """Atomically increment failure_count for a run; returns the new count."""
        async with self.runs.transaction(write_lock=True) as runs:
            value = await runs.increment_and_get("failure_count", where={"run_id": run_id})
        return 0 if value is None else value

    async def list_runs(
        self,
        deployment_ref: str,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Run]:
        """Return runs for a deployment, optionally filtered by status.

        ``limit``/``offset`` are clamped defensively (A02-12): this method is
        reachable beyond the FastAPI route layer's own ``Query(ge=..., le=...)``
        bound, and SQLite treats a non-positive ``LIMIT`` as "no limit at all"
        rather than an error, which would silently turn a caller bug into an
        unbounded scan.
        """
        limit = max(1, min(limit, _LIST_RUNS_MAX_LIMIT))
        offset = max(0, offset)
        where = {"deployment_ref": deployment_ref}
        if status is not None:
            where["status"] = status
        async with self.runs.transaction() as runs:
            rows = await runs.select(
                where=where,
                order_by_desc=("started_at",),
                limit=limit,
                offset=offset,
            )
        return [row_to_run(row) for row in rows]

    async def list_runs_for_scope(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Run]:
        """Return runs across deployments in this repository's tenant scope."""
        limit = max(1, min(limit, _LIST_RUNS_MAX_LIMIT))
        offset = max(0, offset)
        where = {"status": status} if status is not None else None
        async with self.runs.transaction() as runs:
            rows = await runs.select(
                where=where,
                order_by_desc=("started_at",),
                limit=limit,
                offset=offset,
            )
        return [row_to_run(row) for row in rows]

    async def list_child_runs(self, parent_run_id: str) -> list[Run]:
        """Return direct children of one parent within this repository's scope."""
        async with self.runs.transaction() as runs:
            rows = await runs.select(
                where={"parent_run_id": parent_run_id},
                order_by_desc=("started_at",),
                limit=_LIST_RUNS_MAX_LIMIT,
            )
        return [row_to_run(row) for row in rows]

    async def list_dead_letter_runs(self, deployment_ref: str) -> list[Run]:
        """Return runs that have been dead-lettered (failed with dead_letter reason)."""
        async with self.runs.transaction() as runs:
            rows = await runs.select(
                where={
                    "deployment_ref": deployment_ref,
                    "status": RunStatus.FAILED.value,
                },
                order_by_desc=("updated_at",),
            )
        return [
            r
            for r in (row_to_run(row) for row in rows)
            if r.failure_state is not None and r.failure_state.reason == DEAD_LETTER_REASON
        ]


@persistence_surface(
    "service.runs",
    probe=named_isolation_probe("_drive_runs"),
    non_persistence_public_methods=frozenset({"install_fence", "clear_fence"}),
    method_names=frozenset(
        {
            "create",
            "create_guarded",
            "get",
            "list_runs",
            "list_runs_for_scope",
            "list_child_runs",
            "list_dead_letter_runs",
            "put",
            "merge_terminal_metadata",
            "put_if_status",
            "transition",
            "record_history",
            "record_condition_result",
            "increment_failure_count",
            "replay_failed",
            "interrupt",
            "cancel",
            "delete",
            "count_pending",
            "redact_run",
            "redact_run_in_transaction",
            "erasure_payloads_in_transaction",
            "tenant_id_for_run_in_transaction",
            "list_erasable_run_ids",
            "lock_and_recheck_erasable_run",
            "fence_token_snapshot_writes_in_transaction",
            "schedule_child_approval_continuation_in_transaction",
        }
    ),
)
@persistence_surface(
    "service.threads",
    method_names=frozenset(
        {
            "get_active_run_id",
            "get_latest_run_id",
            "list_run_ids",
            "set_active_run_id",
            "clear_active_run_id",
        }
    ),
)
@persistence_surface(
    "service.run_checkpoints",
    method_names=frozenset(
        {
            "write_checkpoint",
            "get_checkpoint",
            "get_latest_checkpoint",
            "get_latest_checkpoint_id_for_run",
            "list_checkpoints",
            "erase_checkpoints_for_run",
            "erase_checkpoints_for_run_in_transaction",
        }
    ),
)
@persistence_surface(
    "service.token_engine_snapshots",
    probe=named_isolation_probe("_drive_snapshots"),
    method_names=frozenset(
        {
            "get_token_snapshot",
            "compare_and_swap_token_snapshot",
            "erase_token_snapshot_for_run_in_transaction",
            "fence_and_erase_token_snapshot_for_run_in_transaction",
        }
    ),
)
class RunRepository:
    """High-level async interface for saving and loading runs.

    Wraps the low-level store and adds business logic like status transitions,
    history recording, and checkpoint management.
    """

    def __init__(
        self,
        database: AsyncDatabase,
        scope_context: ScopeContext | NullWorkspaceScopeContext,
    ):
        if type(scope_context) not in {ScopeContext, NullWorkspaceScopeContext}:
            raise TypeError("scope_context must be a trusted workspace scope")
        self._store = _RunThreadStore(database, scope_context)
        self._token_snapshots = TokenSnapshotRowStore(database, scope_context)

    @classmethod
    def for_default_compatibility(cls, database: AsyncDatabase) -> RunRepository:
        return cls(database, NullWorkspaceScopeContext.for_default_compatibility())

    @property
    def database(self) -> AsyncDatabase:
        """Database backing this repository, for coordinated service transactions."""
        return self._store.database

    @property
    def scope_context(self) -> ScopeContext | NullWorkspaceScopeContext:
        """Trusted owner bound to every repository operation."""
        return self._store.scope_context

    @persistence_operation(ResourceOperation.CREATE)
    async def create(self, run: Run) -> Run:
        """Save a new run and return the persisted version."""
        await self._store.create_run(run)
        created = await self.get(run.run_id)
        assert created is not None
        return created

    @persistence_operation(ResourceOperation.CREATE)
    async def create_guarded(
        self,
        run: Run,
        *,
        settings: EffectiveGuardrailSettings,
        rate_limiter: TokenBucketRateLimiter,
        quota_enforcer: QuotaEnforcer,
    ) -> Run:
        """Atomically reserve guardrail capacity and persist a new run."""
        await self._store.create_run(
            run,
            settings=settings,
            rate_limiter=rate_limiter,
            quota_enforcer=quota_enforcer,
        )
        created = await self.get(run.run_id)
        assert created is not None
        return created

    @persistence_operation(ResourceOperation.CREATE, ResourceOperation.UPDATE)
    async def put(self, run: Run) -> Run:
        """Save (insert or update) a run, including its checkpoint and thread."""
        await self._store.put_run(run)
        return await self.get(run.run_id)

    @persistence_operation(ResourceOperation.READ, ResourceOperation.UPDATE)
    async def put_if_status(self, run: Run, expected_status: RunStatus) -> Run:
        """Save a run only while its persisted status matches the caller's snapshot."""
        await self._store.put_run_if_status(run, expected_status)
        stored = await self.get(run.run_id)
        assert stored is not None
        return stored

    def install_fence(self, run_id: str, worker_id: str, generation: int) -> None:
        """Fence this run's saves on its unexpired owner and generation.

        While installed, every save of the run's row — the worker's own status
        transitions and the orchestrator's drive-time writes alike, since both
        share this repository — is refused in-statement once lease ownership
        moves or expires, raising :class:`FencedRunWriteRejectedError`.
        """
        self._store.install_fence(run_id, worker_id, generation)

    def clear_fence(self, run_id: str, worker_id: str, generation: int) -> bool:
        """Remove only the exact generation's write fence."""
        return self._store.clear_fence(run_id, worker_id, generation)

    @persistence_operation(ResourceOperation.READ)
    async def get(
        self,
        run_id: str,
    ) -> Run | None:
        """Load a run by its ID, or return None if not found.

        WS-B: optional ``tenant_id`` filter (defense-in-depth). Default ``None``
        preserves the no-filter behaviour internal callers rely on.
        """
        return await self._store.get_run(run_id)

    @persistence_operation(ResourceOperation.UPDATE)
    async def merge_terminal_metadata(self, run_id: str, patch: dict[str, Any]) -> Run:
        """Merge out-of-band metadata without touching execution persistence."""
        return await self._store.merge_terminal_metadata(run_id, patch)

    @persistence_operation(ResourceOperation.DELETE)
    async def delete(self, run_id: str) -> None:
        """Remove a run from the database."""
        await self._store.delete_run(run_id)

    @persistence_operation(ResourceOperation.READ)
    async def get_token_snapshot(self, run_id: str) -> TokenEngineSnapshot | None:
        """Load the exact durable token-engine state for a run."""
        return await self._token_snapshots.get(run_id)

    @persistence_operation(ResourceOperation.CREATE, ResourceOperation.UPDATE)
    async def compare_and_swap_token_snapshot(
        self,
        run_id: str,
        *,
        expected_revision: int | None,
        snapshot: TokenEngineSnapshot,
    ) -> TokenEngineSnapshot:
        """Publish one coherent next token-engine revision atomically."""
        return await self._token_snapshots.compare_and_swap(
            run_id,
            expected_revision=expected_revision,
            snapshot=snapshot,
        )

    @persistence_operation(ResourceOperation.CREATE, ResourceOperation.UPDATE)
    async def write_checkpoint(self, run: Run) -> str:
        """Save a snapshot of the run and return the checkpoint ID."""
        return await self._store.write_checkpoint(run)

    @persistence_operation(ResourceOperation.READ)
    async def get_checkpoint(
        self,
        checkpoint_id: str,
    ) -> Run | None:
        """Load a checkpoint, optionally constrained by its run tenant."""
        return await self._store.checkpoints.get(checkpoint_id)

    @persistence_operation(ResourceOperation.READ)
    async def get_latest_checkpoint(self, thread_id: str) -> Run | None:
        """Load the most recent checkpoint for a thread."""
        return await self._store.get_latest_checkpoint(thread_id)

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list_checkpoints(self, thread_id: str) -> list[Run]:
        """Return all checkpoints for a thread, in order."""
        return await self._store.list_checkpoints(thread_id)

    @persistence_operation(ResourceOperation.READ)
    async def get_active_run_id(self, thread_id: str) -> str | None:
        """Return the currently active run ID for a thread."""
        return await self._store.get_active_run_id(thread_id)

    @persistence_operation(ResourceOperation.READ)
    async def get_latest_run_id(self, thread_id: str) -> str | None:
        """Return the most recently added run ID for a thread."""
        return await self._store.get_latest_run_id(thread_id)

    @persistence_operation(ResourceOperation.READ)
    async def list_run_ids(self, thread_id: str) -> list[str]:
        """Return all run IDs belonging to a thread."""
        return await self._store.list_run_ids(thread_id)

    @persistence_operation(ResourceOperation.UPDATE)
    async def set_active_run_id(
        self,
        thread_id: str,
        run_id: str,
    ) -> None:
        """Mark a run as the active run for its thread."""
        await self._store.set_active_run_id(thread_id, run_id)

    @persistence_operation(ResourceOperation.UPDATE)
    async def clear_active_run_id(self, thread_id: str, run_id: str) -> None:
        """Clear the active run for a thread if it matches the given run_id."""
        await self._store.clear_active_run_id(thread_id, run_id)

    @persistence_operation(ResourceOperation.READ, ResourceOperation.UPDATE)
    async def transition(
        self,
        run_id: str,
        new_status: RunStatus,
        *,
        current_node_ids: Sequence[str] | None = None,
        pending_node_ids: Sequence[str] | None = None,
        current_step: str | None = None,
        completed_steps: Sequence[str] | None = None,
        final_output: object | None = None,
        failure_state: RunFailureState | None = None,
        audit_refs: Sequence[str] | None = None,
        error: str | None = None,
    ) -> Run:
        """Change a run's status, validating that the transition is allowed.

        Also lets you update node IDs, steps, output, and failure info
        in the same operation. Raises KeyError if the run doesn't exist,
        or ValueError if the status transition is invalid.
        """
        run = await self.get(run_id)
        if run is None:
            raise KeyError(run_id)
        expected_status = run.status
        _validate_transition(expected_status, new_status)
        run.status = new_status
        if current_node_ids is not None:
            run.current_node_ids = list(current_node_ids)
            run.current_step = run.current_node_ids[0] if run.current_node_ids else None
        if pending_node_ids is not None:
            run.pending_node_ids = list(pending_node_ids)
        if current_step is not None:
            run.current_step = current_step
        if completed_steps is not None:
            run.completed_steps = list(completed_steps)
        if audit_refs is not None:
            run.audit_refs = list(audit_refs)
        if final_output is not None:
            run.final_output = final_output
        if failure_state is not None:
            run.failure_state = failure_state
            run.error = failure_state.message or failure_state.reason
        if error is not None:
            run.error = error
        run.touch()
        await self._store.put_run_if_status(run, expected_status)
        stored = await self.get(run_id)
        assert stored is not None
        return stored

    @persistence_operation(ResourceOperation.UPDATE)
    async def replay_failed(self, run_id: str, deployment_ref: str) -> bool:
        """Atomically requeue one failed run inside this repository's exact scope."""
        async with self._store.runs.transaction(write_lock=True) as runs:
            return await runs.update_if_matches(
                {
                    "status": RunStatus.PENDING.value,
                    "error": None,
                    "failure_state": None,
                    "failure_count": 0,
                    "lease_worker_id": None,
                    "lease_expires_at": None,
                    "updated_at": utc_now().isoformat(),
                },
                where={
                    "run_id": run_id,
                    "deployment_ref": deployment_ref,
                    "status": RunStatus.FAILED.value,
                },
                returning="run_id",
            )

    @persistence_operation(ResourceOperation.READ, ResourceOperation.UPDATE)
    async def interrupt(self, run_id: str, deployment_ref: str) -> Run:
        """Atomically interrupt one RUNNING run inside the bound scope."""
        async with self._store.runs.transaction(write_lock=True) as runs:
            written = await runs.update_if_matches(
                {
                    "status": RunStatus.WAITING_INTERRUPT.value,
                    "updated_at": utc_now().isoformat(),
                },
                where={
                    "run_id": run_id,
                    "deployment_ref": deployment_ref,
                    "status": RunStatus.RUNNING.value,
                },
                returning="run_id",
            )
            row = await runs.select_one(where={"run_id": run_id})
        if row is None or row["deployment_ref"] != deployment_ref:
            raise KeyError(run_id)
        run = row_to_run(row)
        if written or run.status is RunStatus.WAITING_INTERRUPT:
            return run
        raise ValueError("only running runs can be interrupted")

    @persistence_operation(ResourceOperation.READ, ResourceOperation.UPDATE)
    async def cancel(
        self,
        run_id: str,
        deployment_ref: str,
        *,
        failure_state: RunFailureState,
    ) -> Run:
        """Atomically fail a run and revoke its lease inside the bound scope."""
        async with self._store.runs.transaction(write_lock=True) as runs:
            written = await runs.update_if_matches(
                {
                    "status": RunStatus.FAILED.value,
                    "error": failure_state.message or failure_state.reason,
                    "failure_state": _dump_model(failure_state),
                    "lease_worker_id": None,
                    "lease_acquired_at": None,
                    "lease_expires_at": None,
                    "recovery_checkpoint_id": None,
                    "updated_at": utc_now().isoformat(),
                },
                where={"run_id": run_id, "deployment_ref": deployment_ref},
                where_not_in={"status": (RunStatus.COMPLETED.value, RunStatus.FAILED.value)},
                returning="run_id",
            )
            row = await runs.select_one(where={"run_id": run_id})
        if row is None or row["deployment_ref"] != deployment_ref:
            raise KeyError(run_id)
        run = row_to_run(row)
        if written or run.status in {RunStatus.COMPLETED, RunStatus.FAILED}:
            return run
        raise RuntimeError("run cancellation did not reach a terminal state")

    @persistence_operation(ResourceOperation.READ, ResourceOperation.UPDATE)
    async def record_history(self, run_id: str, entry: RunHistoryEntry) -> Run:
        """Append a node execution entry to a run's history."""
        run = await self.get(run_id)
        if run is None:
            raise KeyError(run_id)
        run.execution_history.append(entry)
        run.completed_steps = [item.node_id for item in run.execution_history]
        run.touch()
        return await self.put(run)

    @persistence_operation(ResourceOperation.READ, ResourceOperation.UPDATE)
    async def record_condition_result(self, run_id: str, result: RunConditionResult) -> Run:
        """Append a condition evaluation result to a run's records."""
        run = await self.get(run_id)
        if run is None:
            raise KeyError(run_id)
        run.condition_results.append(result)
        run.touch()
        return await self.put(run)

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def count_pending(self, deployment_ref: str) -> int:
        """Count PENDING runs for a deployment (for backpressure checks)."""
        return await self._store.count_pending(deployment_ref)

    @persistence_operation(ResourceOperation.UPDATE)
    async def increment_failure_count(self, run_id: str) -> int:
        """Increment and return the failure_count for a run."""
        return await self._store.increment_failure_count(run_id)

    @persistence_operation(ResourceOperation.READ)
    async def get_latest_checkpoint_id_for_run(self, run_id: str) -> str | None:
        """Return the most recent checkpoint ID for a run."""
        return await self._store.get_latest_checkpoint_id_for_run(run_id)

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list_runs(
        self,
        deployment_ref: str,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Run]:
        """Return runs for a deployment, optionally filtered by status."""
        return await self._store.list_runs(
            deployment_ref, status=status, limit=limit, offset=offset
        )

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list_runs_for_scope(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Run]:
        """Return runs across deployments within the bound tenant scope."""
        return await self._store.list_runs_for_scope(
            status=status, limit=limit, offset=offset
        )

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list_child_runs(self, parent_run_id: str) -> list[Run]:
        """Return direct child runs within the bound tenant/workspace scope."""
        return await self._store.list_child_runs(parent_run_id)

    @persistence_operation(ResourceOperation.READ, ResourceOperation.UPDATE)
    async def schedule_child_approval_continuation_in_transaction(
        self,
        connection: AsyncConnection,
        *,
        run_id: str,
        direct_child_run_id: str,
        approval_id: str,
        approval_child_run_id: str,
        approval_child_deployment_ref: str,
        deployment_ref: str,
        graph_version_ref: str,
    ) -> tuple[Run, bool]:
        """CAS one waiting ancestor back to its deployment worker's queue.

        The parent row and the signed notification audit are committed by the
        caller through the same ``connection``.  Only the exact paused child
        named in the parent's durable metadata is eligible; a sibling approval
        therefore cannot wake or replay this branch.  Repeating the same
        notification is an idempotent read, while any conflicting marker fails
        closed.
        """
        runs = self._store.runs.in_transaction(connection)
        row = await runs.select_one(where={"run_id": run_id}, for_update=True)
        if row is None:
            raise KeyError(run_id)
        run = row_to_run(row)
        self._store.validate_owner(run.tenant_id, run.workspace_id)
        if run.deployment_ref != deployment_ref or run.graph_version_ref != graph_version_ref:
            raise ValueError("continuation ancestor does not match the active deployment")

        marker = {
            "approval_id": approval_id,
            "child_run_id": approval_child_run_id,
            "child_deployment_ref": approval_child_deployment_ref,
        }
        current_marker = run.metadata.get("child_approval_continuation")
        if current_marker is not None:
            if current_marker != marker:
                raise ValueError("a different child approval continuation is already scheduled")
            if run.status in {RunStatus.PENDING, RunStatus.RUNNING}:
                return run, False

        pending_subgraph = run.metadata.get("pending_subgraph")
        pending_parallel = run.metadata.get("pending_parallel_subgraph")
        paused_child_id = None
        if isinstance(pending_subgraph, dict):
            paused_child_id = pending_subgraph.get("child_run_id")
        if paused_child_id is None and isinstance(pending_parallel, dict):
            paused_branch = pending_parallel.get("paused_branch")
            if isinstance(paused_branch, dict):
                paused_child_id = paused_branch.get("child_run_id")
        if run.status is not RunStatus.WAITING_APPROVAL or paused_child_id != direct_child_run_id:
            raise ValueError("ancestor is not waiting for the approved child continuation")

        run.metadata["child_approval_continuation"] = marker
        run.status = RunStatus.PENDING
        run.touch()
        updated = await runs.update_if_matches(
            {
                "status": run.status.value,
                "metadata": to_json_value(run.metadata),
                "updated_at": run.updated_at.isoformat(),
            },
            where={
                "run_id": run.run_id,
                "status": RunStatus.WAITING_APPROVAL.value,
                "deployment_ref": deployment_ref,
                "graph_version_ref": graph_version_ref,
            },
            returning="run_id",
        )
        if not updated:
            raise ValueError("ancestor continuation lost its compare-and-set claim")
        return run, True

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list_dead_letter_runs(self, deployment_ref: str) -> list[Run]:
        """Return dead-lettered runs for a deployment."""
        return await self._store.list_dead_letter_runs(deployment_ref)

    @persistence_operation(ResourceOperation.DELETE)
    async def erase_checkpoints_for_run(self, run_id: str) -> int:
        """WS-E: delete a run's checkpoints — the missing retention cascade.

        ``run_checkpoints.state_json`` holds the full serialized run state (the
        richest plaintext PII surface), and neither ``delete_run`` nor
        ``redact_run`` reaches it. Right-to-erasure / TTL purge calls this to
        remove that snapshot. Returns the number of checkpoint rows deleted;
        idempotent (a second call deletes nothing and returns 0).
        """
        async with self._store.checkpoints.table.transaction() as checkpoints:
            return await retention_queries.erase_checkpoints_for_run(checkpoints, run_id)

    @persistence_operation(ResourceOperation.DELETE)
    async def erase_checkpoints_for_run_in_transaction(
        self,
        connection: AsyncConnection,
        run_id: str,
    ) -> int:
        """Delete checkpoints through an existing transaction."""
        return await retention_queries.erase_checkpoints_for_run(
            self._store.checkpoints.table.in_transaction(connection), run_id
        )

    @persistence_operation(ResourceOperation.DELETE)
    async def erase_token_snapshot_for_run_in_transaction(
        self,
        connection: AsyncConnection,
        run_id: str,
    ) -> int:
        """Delete token-engine state through an existing erasure transaction."""
        return await retention_queries.erase_token_snapshot_for_run(
            self._store.token_snapshots.in_transaction(connection), run_id
        )

    @persistence_operation(ResourceOperation.READ, ResourceOperation.UPDATE)
    async def fence_token_snapshot_writes_in_transaction(
        self,
        connection: AsyncConnection,
        run_id: str,
    ) -> bool:
        """Lock the run row and fence token writes during erasure."""
        return await retention_queries.fence_token_snapshot_writes(
            self._store.runs.in_transaction(connection), run_id
        )

    @persistence_operation(ResourceOperation.DELETE)
    async def fence_and_erase_token_snapshot_for_run_in_transaction(
        self,
        connection: AsyncConnection,
        run_id: str,
    ) -> int:
        """Fence future writes and delete token state through one transaction."""
        return await retention_queries.fence_and_erase_token_snapshot_for_run(
            self._store.runs.in_transaction(connection),
            self._store.token_snapshots.in_transaction(connection),
            run_id,
        )

    @persistence_operation(ResourceOperation.READ, ResourceOperation.UPDATE)
    async def redact_run(self, run_id: str) -> bool:
        """WS-E: null a run's PII-bearing output columns, keeping the row.

        Nulls ``final_output``/``artifacts``/``metadata``/``error`` in place so
        the run row survives for chain/thread continuity while its plaintext
        payloads are gone. ``artifacts``/``metadata`` are NOT NULL columns, so
        they are reset to the empty-object sentinel rather than NULL. Idempotent.
        Returns True if the run existed.
        """
        async with self._store.runs.transaction() as runs:
            return await retention_queries.redact_run(runs, run_id)

    @persistence_operation(ResourceOperation.READ, ResourceOperation.UPDATE)
    async def redact_run_in_transaction(
        self,
        connection: AsyncConnection,
        run_id: str,
    ) -> bool:
        """Redact a run through an existing transaction."""
        return await retention_queries.redact_run(
            self._store.runs.in_transaction(connection), run_id
        )

    @persistence_operation(ResourceOperation.READ)
    async def erasure_payloads_in_transaction(
        self,
        connection: AsyncConnection,
        run_id: str,
    ) -> list[Any]:
        """Load database-resident run/checkpoint/token payloads before erasure."""
        return await retention_queries.erasure_payloads(
            self._store.runs.in_transaction(connection),
            self._store.checkpoints.table.in_transaction(connection),
            self._store.token_snapshots.in_transaction(connection),
            run_id,
            decrypt=self._store.checkpoints.decrypt_state_json,
        )

    @persistence_operation(ResourceOperation.READ)
    async def tenant_id_for_run_in_transaction(
        self,
        connection: AsyncConnection,
        run_id: str,
    ) -> str | None:
        """Resolve a run's persisted tenant inside a caller transaction."""
        return await retention_queries.tenant_id_for_run(
            self._store.runs.in_transaction(connection), run_id
        )

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list_erasable_run_ids(
        self,
        older_than: datetime,
        *,
        terminal_statuses: frozenset[RunStatus] | set[RunStatus] | None = None,
    ) -> list[str]:
        """Select TTL-erasable run ids: terminal status AND stale ``updated_at``.

        Only COMPLETED/FAILED runs qualify by default — PENDING, RUNNING, and
        the WAITING_* states are live work regardless of age. Selection is an
        unlocked snapshot; the destructive path must re-check via
        :meth:`lock_and_recheck_erasable_run` inside its own transaction.
        """
        async with self._store.runs.transaction() as runs:
            return await retention_queries.select_erasable_run_ids(
                runs,
                older_than,
                terminal_statuses=terminal_statuses,
            )

    @persistence_operation(ResourceOperation.READ)
    async def lock_and_recheck_erasable_run(
        self,
        connection: AsyncConnection,
        run_id: str,
        cutoff: datetime,
        *,
        terminal_statuses: frozenset[RunStatus] | set[RunStatus] | None = None,
    ) -> str | None:
        """Lock the run row and re-verify TTL eligibility before destruction.

        On PostgreSQL the row is locked with ``FOR UPDATE``; on SQLite the
        caller's write transaction already serializes writers. Returns ``None``
        when a replay/resume/update between selection and erasure made the run
        ineligible (wrong tenant, non-terminal status, or fresh ``updated_at``).
        """
        return await retention_queries.lock_and_recheck_erasable_run(
            self._store.runs.in_transaction(connection),
            run_id,
            cutoff,
            terminal_statuses=terminal_statuses,
            lock=self._store.database.backend == "postgres",
        )
