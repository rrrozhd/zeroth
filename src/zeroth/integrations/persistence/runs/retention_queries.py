"""Right-to-erasure and TTL queries over the run tables.

Every function here takes a caller-supplied connection and opens no
transaction of its own. That is deliberate: retention erases runs,
checkpoints, artifacts, and its own audit log as one atomic unit, so the
transaction has to be owned by the orchestration above rather than by each
individual query.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from zeroth.platform.storage.json import from_json_value
from zeroth.platform.storage.scoped_table import BoundStructuredTable
from zeroth.runtime.runs import RunStatus

#: TTL erasure only ever considers runs that have finished. PENDING, RUNNING,
#: and the WAITING_* states are live work regardless of age.
TERMINAL_STATUSES: frozenset[RunStatus] = frozenset({RunStatus.COMPLETED, RunStatus.FAILED})


async def erase_checkpoints_for_run(checkpoints: BoundStructuredTable, run_id: str) -> int:
    """Delete a run's checkpoints, returning how many rows were removed.

    ``run_checkpoints.state_json`` holds the full serialized run state — the
    richest plaintext PII surface — and neither deleting nor redacting the run
    row reaches it. Idempotent: a second call deletes nothing and returns 0.
    """
    rows = await checkpoints.select(where={"run_id": run_id}, columns=("checkpoint_id",))
    if rows:
        await checkpoints.delete(where={"run_id": run_id})
    return len(rows)


async def erase_token_snapshot_for_run(snapshots: BoundStructuredTable, run_id: str) -> int:
    """Delete the token-engine payload retained for a redacted run."""
    row = await snapshots.select_one(where={"run_id": run_id}, columns=("run_id",))
    if row is not None:
        await snapshots.delete(where={"run_id": run_id})
    return int(row is not None)


async def fence_token_snapshot_writes(runs: BoundStructuredTable, run_id: str) -> bool:
    """Lock a run and durably prevent token state from being recreated."""
    existing = await runs.select_one(where={"run_id": run_id}, columns=("run_id",), for_update=True)
    if existing is None:
        return False
    await runs.update({"token_snapshot_write_disabled": 1}, where={"run_id": run_id})
    return True


async def fence_and_erase_token_snapshot_for_run(
    runs: BoundStructuredTable,
    snapshots: BoundStructuredTable,
    run_id: str,
) -> int:
    """Fence future writes and delete current token state in one transaction."""
    await fence_token_snapshot_writes(runs, run_id)
    return await erase_token_snapshot_for_run(snapshots, run_id)


async def redact_run(runs: BoundStructuredTable, run_id: str) -> bool:
    """Null a run's PII-bearing output columns while keeping the row.

    The run row survives for chain and thread continuity while its plaintext
    payloads are gone. ``artifacts`` and ``metadata`` are NOT NULL columns, so
    they reset to the empty-object sentinel rather than NULL. Idempotent;
    returns True if the run existed.
    """
    existing = await runs.select_one(where={"run_id": run_id}, columns=("run_id",))
    if existing is None:
        return False
    await runs.update(
        {
            "final_output": None,
            "artifacts": "{}",
            "metadata": "{}",
            "error": None,
            "execution_history": "[]",
            "failure_state": None,
            "condition_results": "[]",
            "channels": "{}",
            "pending_approval": None,
        },
        where={"run_id": run_id},
    )
    return True


async def erasure_payloads(
    runs: BoundStructuredTable,
    checkpoints: BoundStructuredTable,
    snapshots: BoundStructuredTable,
    run_id: str,
    *,
    decrypt: Callable[[str], str] | None = None,
) -> list[Any]:
    """Load database-resident run and checkpoint payloads before erasure.

    ``decrypt`` reverses at-rest checkpoint encryption before parsing —
    ``state_json`` is Fernet-encrypted when the database has an encryption key
    (mirrors the checkpoint read path). Without it the harvest raised
    ``JSONDecodeError`` and rolled the whole erasure back to a no-op on every
    encrypted-at-rest deployment (audit F1).
    """
    payloads: list[Any] = []
    run = await runs.select_one(
        where={"run_id": run_id},
        columns=("final_output", "artifacts", "metadata", "error"),
    )
    if run is not None:
        for column in ("final_output", "artifacts", "metadata"):
            raw = run[column]
            if raw is not None:
                payloads.append(from_json_value(raw))
        if run["error"] is not None:
            payloads.append(run["error"])
    checkpoint_rows = await checkpoints.select(where={"run_id": run_id}, columns=("state_json",))
    payloads.extend(
        from_json_value(decrypt(row["state_json"]) if decrypt is not None else row["state_json"])
        for row in checkpoint_rows
    )
    token_snapshot = await snapshots.select_one(
        where={"run_id": run_id}, columns=("snapshot_json",)
    )
    if token_snapshot is not None:
        raw = token_snapshot["snapshot_json"]
        payloads.append(from_json_value(decrypt(raw) if decrypt is not None else raw))
    return payloads


async def tenant_id_for_run(runs: BoundStructuredTable, run_id: str) -> str | None:
    """Resolve a run's persisted tenant inside a caller transaction."""
    row = await runs.select_one(where={"run_id": run_id}, columns=("tenant_id",))
    return None if row is None else str(row["tenant_id"])


async def select_erasable_run_ids(
    runs: BoundStructuredTable,
    older_than: datetime,
    *,
    terminal_statuses: frozenset[RunStatus] | set[RunStatus] | None = None,
) -> list[str]:
    """Select TTL-erasable run ids: terminal status AND stale ``updated_at``.

    Selection is an unlocked snapshot; the destructive path must re-check via
    :func:`lock_and_recheck_erasable_run` inside its own transaction.
    """
    terminal = terminal_statuses or TERMINAL_STATUSES
    statuses = sorted(status.value for status in terminal)
    cutoff = older_than.astimezone(UTC).isoformat()
    rows = await runs.select(
        columns=("run_id",),
        where_in={"status": tuple(statuses)},
        where_lt={"updated_at": cutoff},
        order_by=("updated_at", "run_id"),
    )
    return [str(row["run_id"]) for row in rows]


async def lock_and_recheck_erasable_run(
    runs: BoundStructuredTable,
    run_id: str,
    cutoff: datetime,
    *,
    terminal_statuses: frozenset[RunStatus] | set[RunStatus] | None = None,
    lock: bool = False,
) -> str | None:
    """Lock the run row and re-verify TTL eligibility before destruction.

    ``lock`` adds ``FOR UPDATE``, which only PostgreSQL needs; on SQLite the
    caller's write transaction already serializes writers. Returns ``None``
    when a replay, resume, or update between selection and erasure made the
    run ineligible (wrong tenant, non-terminal status, or fresh
    ``updated_at``).
    """
    statuses = {status.value for status in (terminal_statuses or TERMINAL_STATUSES)}
    row = await runs.select_one(
        where={"run_id": run_id},
        columns=("run_id", "status", "updated_at"),
        for_update=lock,
    )
    if row is None:
        return None
    if str(row["status"]) not in statuses:
        return None
    if str(row["updated_at"]) >= cutoff.astimezone(UTC).isoformat():
        return None
    return run_id
