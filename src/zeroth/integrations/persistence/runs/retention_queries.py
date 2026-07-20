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

from zeroth.platform.storage import AsyncConnection
from zeroth.platform.storage.json import from_json_value
from zeroth.runtime.runs import RunStatus

#: TTL erasure only ever considers runs that have finished. PENDING, RUNNING,
#: and the WAITING_* states are live work regardless of age.
TERMINAL_STATUSES: frozenset[RunStatus] = frozenset({RunStatus.COMPLETED, RunStatus.FAILED})


async def erase_checkpoints_for_run(connection: AsyncConnection, run_id: str) -> int:
    """Delete a run's checkpoints, returning how many rows were removed.

    ``run_checkpoints.state_json`` holds the full serialized run state — the
    richest plaintext PII surface — and neither deleting nor redacting the run
    row reaches it. Idempotent: a second call deletes nothing and returns 0.
    """
    rows = await connection.fetch_all(
        "SELECT checkpoint_id FROM run_checkpoints WHERE run_id = ?",
        (run_id,),
    )
    if rows:
        await connection.execute(
            "DELETE FROM run_checkpoints WHERE run_id = ?",
            (run_id,),
        )
    return len(rows)


async def redact_run(connection: AsyncConnection, run_id: str) -> bool:
    """Null a run's PII-bearing output columns while keeping the row.

    The run row survives for chain and thread continuity while its plaintext
    payloads are gone. ``artifacts`` and ``metadata`` are NOT NULL columns, so
    they reset to the empty-object sentinel rather than NULL. Idempotent;
    returns True if the run existed.
    """
    existing = await connection.fetch_one(
        "SELECT 1 FROM runs WHERE run_id = ?",
        (run_id,),
    )
    if existing is None:
        return False
    await connection.execute(
        # Clear every free-form column that can hold plaintext PII (audit F1 +
        # re-audit). execution_history holds each node's input/output snapshot;
        # failure_state holds the failure message/details (and `error` re-derives
        # FROM failure_state.message on read, so nulling error alone was not
        # enough — the plaintext resurfaced); condition_results.details and
        # channels are free-form dicts; pending_approval carries the requester's
        # free-form reason + metadata for an outstanding gate. NOT NULL columns
        # reset to their empty default ('[]' / '{}'); nullable ones to NULL.
        "UPDATE runs SET final_output = NULL, artifacts = '{}', "
        "metadata = '{}', error = NULL, execution_history = '[]', "
        "failure_state = NULL, condition_results = '[]', channels = '{}', "
        "pending_approval = NULL "
        "WHERE run_id = ?",
        (run_id,),
    )
    return True


async def erasure_payloads(
    connection: AsyncConnection,
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
    run = await connection.fetch_one(
        "SELECT final_output, artifacts, metadata, error FROM runs WHERE run_id = ?",
        (run_id,),
    )
    if run is not None:
        for column in ("final_output", "artifacts", "metadata"):
            raw = run[column]
            if raw is not None:
                payloads.append(from_json_value(raw))
        if run["error"] is not None:
            payloads.append(run["error"])
    checkpoints = await connection.fetch_all(
        "SELECT state_json FROM run_checkpoints WHERE run_id = ?",
        (run_id,),
    )
    payloads.extend(
        from_json_value(decrypt(row["state_json"]) if decrypt is not None else row["state_json"])
        for row in checkpoints
    )
    return payloads


async def tenant_id_for_run(connection: AsyncConnection, run_id: str) -> str | None:
    """Resolve a run's persisted tenant inside a caller transaction."""
    row = await connection.fetch_one(
        "SELECT tenant_id FROM runs WHERE run_id = ?",
        (run_id,),
    )
    return None if row is None else str(row["tenant_id"])


async def select_erasable_run_ids(
    connection: AsyncConnection,
    tenant_id: str,
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
    placeholders = ", ".join("?" for _ in statuses)
    cutoff = older_than.astimezone(UTC).isoformat()
    rows = await connection.fetch_all(
        f"SELECT run_id FROM runs WHERE tenant_id = ? AND status IN ({placeholders}) "
        "AND updated_at < ? ORDER BY updated_at, run_id",
        (tenant_id, *statuses, cutoff),
    )
    return [str(row["run_id"]) for row in rows]


async def lock_and_recheck_erasable_run(
    connection: AsyncConnection,
    run_id: str,
    tenant_id: str,
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
    suffix = " FOR UPDATE" if lock else ""
    row = await connection.fetch_one(
        f"SELECT run_id, tenant_id, status, updated_at FROM runs WHERE run_id = ?{suffix}",
        (run_id,),
    )
    if row is None:
        return None
    if str(row["tenant_id"]) != tenant_id:
        return None
    if str(row["status"]) not in statuses:
        return None
    if str(row["updated_at"]) >= cutoff.astimezone(UTC).isoformat():
        return None
    return run_id
