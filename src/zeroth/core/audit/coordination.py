"""Transactional coordination and ordering for per-run audit chains."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from zeroth.core.audit.models import NodeAuditRecord
from zeroth.platform.storage.coordination import ensure_and_lock_row
from zeroth.platform.storage.database import AsyncConnection
from zeroth.platform.storage.json import load_typed_value

# Keep the common pure-sequence path indexable. In particular, do not replace
# these with a CASE expression that forces SQLite to build a temporary B-tree.
SEQUENCED_RUN_ROWS_SQL = """
    SELECT record_json, chain_sequence
    FROM node_audits
    WHERE run_id = ? AND chain_sequence IS NOT NULL
    ORDER BY chain_sequence
"""

LEGACY_RUN_ROWS_SQL = """
    SELECT record_json, chain_sequence
    FROM node_audits
    WHERE run_id = ? AND chain_sequence IS NULL
    ORDER BY created_at, audit_id
"""

LEGACY_RUN_EXISTS_SQL = """
    SELECT 1
    FROM node_audits
    WHERE run_id = ? AND chain_sequence IS NULL
    LIMIT 1
"""


class AuditChainOrderingError(ValueError):
    """Raised when mixed-generation rows cannot form one unambiguous chain."""


@dataclass(frozen=True, slots=True)
class AuditChainHead:
    """The predecessor digest and sequence allocated to the next append."""

    digest: str | None
    next_sequence: int


def hydrate_audit_row(row: dict[str, object]) -> NodeAuditRecord:
    """Hydrate JSON while treating the dedicated sequence column as canonical."""
    payload = load_typed_value(row["record_json"], dict)
    payload["chain_sequence"] = row.get("chain_sequence")
    return NodeAuditRecord.model_validate(payload)


async def _fetch_sequenced_records(
    connection: AsyncConnection, run_id: str
) -> list[NodeAuditRecord]:
    rows = await connection.fetch_all(SEQUENCED_RUN_ROWS_SQL, (run_id,))
    return [hydrate_audit_row(row) for row in rows]


async def _fetch_legacy_records(connection: AsyncConnection, run_id: str) -> list[NodeAuditRecord]:
    rows = await connection.fetch_all(LEGACY_RUN_ROWS_SQL, (run_id,))
    return [hydrate_audit_row(row) for row in rows]


async def _has_legacy_records(connection: AsyncConnection, run_id: str) -> bool:
    return await connection.fetch_one(LEGACY_RUN_EXISTS_SQL, (run_id,)) is not None


def _reconstruct_linked_chain(records: list[NodeAuditRecord]) -> list[NodeAuditRecord]:
    by_digest: dict[str, NodeAuditRecord] = {}
    for record in records:
        digest = record.record_digest
        if digest is None:
            raise AuditChainOrderingError(f"audit {record.audit_id!r} has no record digest")
        if digest in by_digest:
            raise AuditChainOrderingError(f"duplicate record digest at audit {record.audit_id!r}")
        by_digest[digest] = record

    successors: dict[str, NodeAuditRecord] = {}
    roots: list[NodeAuditRecord] = []
    for record in records:
        previous = record.previous_record_digest
        if previous is None:
            roots.append(record)
            continue
        if previous not in by_digest:
            raise AuditChainOrderingError(
                f"audit {record.audit_id!r} references a missing predecessor"
            )
        if previous in successors:
            raise AuditChainOrderingError(f"audit chain forks after digest {previous!r}")
        successors[previous] = record

    if len(roots) != 1:
        raise AuditChainOrderingError(f"audit chain has {len(roots)} roots; expected exactly one")

    ordered: list[NodeAuditRecord] = []
    current: NodeAuditRecord | None = roots[0]
    while current is not None:
        ordered.append(current)
        current = successors.get(current.record_digest or "")
        if current is not None and current in ordered:
            raise AuditChainOrderingError("audit chain contains a cycle")
    if len(ordered) != len(records):
        raise AuditChainOrderingError("audit chain contains disconnected records")
    return ordered


def order_audit_records(
    records: list[NodeAuditRecord],
    *,
    strict: bool = False,
) -> list[NodeAuditRecord]:
    """Order one run across sequenced, legacy, and rolling-upgrade writers.

    Pure sequenced runs use their database-assigned sequence. Pure legacy runs
    retain repository ``created_at, audit_id`` query order. Mixed generations
    are reconstructed from the digest links because neither NULL-first nor
    sequence-first ordering can represent an interleaved rolling deployment.
    """
    if len(records) < 2:
        return list(records)
    has_legacy = any(record.chain_sequence is None for record in records)
    has_sequenced = any(record.chain_sequence is not None for record in records)
    if not has_legacy:
        return sorted(records, key=lambda record: record.chain_sequence or 0)
    if not has_sequenced:
        return list(records)
    try:
        return _reconstruct_linked_chain(records)
    except AuditChainOrderingError:
        if strict:
            raise
        # Reads remain deterministic for diagnostics; continuity verification
        # uses strict mode and reports the structural error explicitly.
        return list(records)


async def load_ordered_run_records(
    connection: AsyncConnection,
    run_id: str,
    *,
    strict: bool = False,
) -> list[NodeAuditRecord]:
    """Load one run via index-friendly sequence and legacy fallback queries."""
    sequenced = await _fetch_sequenced_records(connection, run_id)
    if not await _has_legacy_records(connection, run_id):
        return sequenced
    legacy = await _fetch_legacy_records(connection, run_id)
    if not sequenced:
        return legacy
    return order_audit_records([*sequenced, *legacy], strict=strict)


async def lock_audit_chain(
    connection: AsyncConnection,
    *,
    backend: Literal["sqlite", "postgres"],
    run_id: str,
) -> AuditChainHead:
    """Lock one run head and recover tails written by rolling legacy workers."""
    row = await ensure_and_lock_row(
        connection,
        backend=backend,
        table="audit_chain_heads",
        key_column="run_id",
        key=run_id,
    )
    if row is None:  # pragma: no cover - INSERT + SELECT is atomic by contract
        raise RuntimeError(f"audit chain head for {run_id!r} was not created")

    digest = row["head_digest"]
    next_sequence = int(row["next_sequence"])
    has_legacy = await _has_legacy_records(connection, run_id)
    if not has_legacy and (digest is not None or next_sequence != 1):
        return AuditChainHead(
            digest=str(digest) if digest is not None else None,
            next_sequence=next_sequence,
        )

    sequenced = await _fetch_sequenced_records(connection, run_id)
    legacy = await _fetch_legacy_records(connection, run_id) if has_legacy else []
    existing = (
        order_audit_records([*sequenced, *legacy], strict=True)
        if sequenced and legacy
        else sequenced or legacy
    )
    if not existing:
        return AuditChainHead(digest=None, next_sequence=1)

    latest = existing[-1]
    assigned_sequences = [
        record.chain_sequence for record in sequenced if record.chain_sequence is not None
    ]
    recovered_next = max(assigned_sequences, default=0) + 1
    return AuditChainHead(digest=latest.record_digest, next_sequence=recovered_next)


async def advance_audit_chain(
    connection: AsyncConnection,
    *,
    run_id: str,
    digest: str,
    next_sequence: int,
) -> None:
    """Persist the new digest and following sequence in the current transaction."""
    await connection.execute(
        """
        UPDATE audit_chain_heads
        SET head_digest = ?, next_sequence = ?, updated_at = ?
        WHERE run_id = ?
        """,
        (digest, next_sequence, datetime.now(UTC).isoformat(), run_id),
    )
