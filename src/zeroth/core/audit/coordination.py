"""Transactional coordination for per-run audit digest chains."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from zeroth.core.audit.models import NodeAuditRecord
from zeroth.core.storage.coordination import ensure_and_lock_row
from zeroth.core.storage.database import AsyncConnection
from zeroth.core.storage.json import load_typed_value


@dataclass(frozen=True, slots=True)
class AuditChainHead:
    """The predecessor digest and sequence allocated to the next append."""

    digest: str | None
    next_sequence: int


async def lock_audit_chain(
    connection: AsyncConnection,
    *,
    backend: Literal["sqlite", "postgres"],
    run_id: str,
) -> AuditChainHead:
    """Lock one run head and lazily recover it from legacy audit rows."""
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
    if digest is not None or next_sequence != 1:
        return AuditChainHead(
            digest=str(digest) if digest is not None else None,
            next_sequence=next_sequence,
        )

    existing = await connection.fetch_all(
        """
        SELECT record_json, chain_sequence
        FROM node_audits
        WHERE run_id = ?
        ORDER BY
            CASE WHEN chain_sequence IS NULL THEN 0 ELSE 1 END,
            chain_sequence,
            created_at,
            audit_id
        """,
        (run_id,),
    )
    if not existing:
        return AuditChainHead(digest=None, next_sequence=1)

    latest = NodeAuditRecord.model_validate(load_typed_value(existing[-1]["record_json"], dict))
    assigned_sequences = [
        int(existing_row["chain_sequence"])
        for existing_row in existing
        if existing_row["chain_sequence"] is not None
    ]
    recovered_next = max(len(existing) + 1, max(assigned_sequences, default=0) + 1)
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
