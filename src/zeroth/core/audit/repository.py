"""Async database-backed storage for audit records.

Provides the AuditRepository class that handles saving and querying
NodeAuditRecord objects using an async database.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from zeroth.core.audit.models import AuditQuery, NodeAuditRecord
from zeroth.core.audit.verifier import _compute_pii_commitments, compute_chained_record
from zeroth.core.storage import AsyncDatabase
from zeroth.core.storage.json import load_typed_value, to_json_value

if TYPE_CHECKING:
    from zeroth.core.signing import SigningKeyProvider

# The concrete "empty" each PII payload field is reset to on crypto-erasure,
# matching the model defaults so the nulled record still validates. The digest
# is over ``pii_commitments`` (v2), so these substitutions never change it.
_ERASED_PII_VALUES: dict[str, object] = {
    "input_snapshot": {},
    "output_snapshot": {},
    "validation_results": {},
    "execution_metadata": {},
    "stdout": None,
    "stderr": None,
    "error": None,
    "tool_calls": [],
    "memory_interactions": [],
}


class AuditRepository:
    """Saves and retrieves audit records from an async database.

    Use this class to store audit records when nodes run and to look them
    up later for debugging, compliance, or building timelines.
    """

    def __init__(
        self,
        database: AsyncDatabase,
        signer: SigningKeyProvider | None = None,
    ):
        self._database: AsyncDatabase = database
        # WS-D signer: signs each record's digest under the SAME chain lock that
        # fixes the chain head, so the digest and its signature are committed
        # atomically. None -> records stay unsigned-legacy (injected post-build
        # by bootstrap once the shared secret provider exists).
        self._signer = signer
        # Chain writes read the current head then insert; concurrent fan-out
        # branches on separate connections would otherwise both chain off the
        # same predecessor and silently fork the tamper-evident digest chain.
        # A process-wide lock serializes the read-head+insert critical section;
        # multi-process deployments additionally need DB-level enforcement.
        self._chain_lock = asyncio.Lock()
        # created_at orders both head selection and chain verification, so it
        # must be monotonic in WRITE order. Node start times are not (a
        # fan-out source node starts before the branch records it is written
        # after), which used to fork the chain.
        self._last_created_at: datetime | None = None

    async def write(self, record: NodeAuditRecord) -> NodeAuditRecord:
        """Save an audit record to the database.

        Writes are append-only. Duplicate audit IDs are rejected so history
        cannot be silently rewritten.
        """
        async with self._chain_lock, self._database.transaction() as connection:
            previous = await self._latest_for_run(connection, record.run_id)
            # WS-E: stamp digest_version=2 + per-field PII commitments BEFORE the
            # digest is computed, so the digest folds in the commitments and stays
            # identical after a later crypto-erasure nulls the plaintext. Always
            # populated for a v2 write — never left None (an empty-commitment v2
            # record would still "verify" while binding no PII).
            prepared = record.model_copy(
                update={
                    "digest_version": 2,
                    "pii_commitments": _compute_pii_commitments(record),
                }
            )
            chained = compute_chained_record(
                prepared,
                previous.record_digest if previous is not None else None,
                self._signer,
            )
            existing = await connection.fetch_one(
                "SELECT 1 FROM node_audits WHERE audit_id = ?",
                (chained.audit_id,),
            )
            if existing is not None:
                raise ValueError(f"audit_id {record.audit_id!r} already exists")
            created_at = datetime.now(UTC)
            if self._last_created_at is not None and created_at <= self._last_created_at:
                created_at = self._last_created_at + timedelta(microseconds=1)
            self._last_created_at = created_at
            await connection.execute(
                """
                INSERT INTO node_audits (
                    audit_id,
                    run_id,
                    thread_id,
                    node_id,
                    graph_version_ref,
                    deployment_ref,
                    tenant_id,
                    workspace_id,
                    created_at,
                    record_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chained.audit_id,
                    chained.run_id,
                    chained.thread_id,
                    chained.node_id,
                    chained.graph_version_ref,
                    chained.deployment_ref,
                    chained.tenant_id,
                    chained.workspace_id,
                    created_at.isoformat(),
                    to_json_value(chained.model_dump(mode="json")),
                ),
            )
        return await self.get(record.audit_id)

    async def get(self, audit_id: str) -> NodeAuditRecord | None:
        """Look up a single audit record by its ID. Returns None if not found."""
        async with self._database.transaction() as connection:
            row = await connection.fetch_one(
                "SELECT record_json FROM node_audits WHERE audit_id = ?",
                (audit_id,),
            )
        if row is None:
            return None
        return NodeAuditRecord.model_validate(load_typed_value(row["record_json"], dict))

    async def list(self, query: AuditQuery | None = None) -> list[NodeAuditRecord]:
        """Return audit records matching the given filters, ordered by time.

        Pass an AuditQuery to filter by run, thread, node, etc. If no query
        is given, all records are returned.
        """
        query = query or AuditQuery()
        clauses: list[str] = []
        params: list[str] = []
        for field in (
            "run_id",
            "thread_id",
            "node_id",
            "graph_version_ref",
            "deployment_ref",
            "tenant_id",  # WS-B: tenant filter (node_audits.tenant_id column)
        ):
            value = getattr(query, field)
            if value is None:
                continue
            clauses.append(f"{field} = ?")
            params.append(value)
        sql = "SELECT record_json FROM node_audits"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, audit_id"
        async with self._database.transaction() as connection:
            rows = await connection.fetch_all(sql, tuple(params))
        return [
            NodeAuditRecord.model_validate(load_typed_value(row["record_json"], dict))
            for row in rows
        ]

    async def list_by_run(self, run_id: str) -> list[NodeAuditRecord]:
        """Return all audit records for a specific run."""
        return await self.list(AuditQuery(run_id=run_id))

    async def list_by_thread(self, thread_id: str) -> list[NodeAuditRecord]:
        """Return all audit records for a specific thread."""
        return await self.list(AuditQuery(thread_id=thread_id))

    async def list_by_node(self, node_id: str) -> list[NodeAuditRecord]:
        """Return all audit records for a specific node."""
        return await self.list(AuditQuery(node_id=node_id))

    async def list_by_graph_version(self, graph_version_ref: str) -> list[NodeAuditRecord]:
        """Return all audit records for a specific graph version."""
        return await self.list(AuditQuery(graph_version_ref=graph_version_ref))

    async def list_by_deployment(self, deployment_ref: str) -> list[NodeAuditRecord]:
        """Return all audit records for a specific deployment."""
        return await self.list(AuditQuery(deployment_ref=deployment_ref))

    async def write_many(self, records: Sequence[NodeAuditRecord]) -> list[NodeAuditRecord]:
        """Save multiple audit records at once. Returns all saved records."""
        return [await self.write(record) for record in records]

    async def crypto_erase(self, audit_id: str, *, reason: str) -> NodeAuditRecord | None:
        """Crypto-erase a single record's PII while keeping the chain verifiable.

        A SANCTIONED, append-only-preserving single-row UPDATE: it nulls the PII
        payload fields (``input_snapshot``, ``output_snapshot``, ``stdout``, tool
        calls, memory interactions, …), keeps ``pii_commitments`` and the digest,
        and stamps ``erased``/``erased_at``/``erasure_reason``. Because a v2
        digest is computed over the commitments (not the plaintext), the record
        digest, its signature, and the whole hash-chain still verify afterwards.

        ``created_at``, ``audit_id``, ``previous_record_digest`` and
        ``record_digest`` are NEVER touched — re-chaining history is itself a
        tamper event. digest_version=1 (legacy) records are un-erasable and raise;
        an already-erased or missing record is a no-op (idempotent).
        """
        record = await self.get(audit_id)
        if record is None:
            return None
        if (record.digest_version or 1) < 2:
            raise ValueError(
                f"audit_id {audit_id!r} is digest_version=1 (legacy) and cannot be "
                "crypto-erased; legacy whole-payload digests are grandfathered"
            )
        if record.erased:
            return record  # idempotent: already erased, commitments/digest intact
        erased = record.model_copy(
            update={
                **_ERASED_PII_VALUES,
                "erased": True,
                "erased_at": datetime.now(UTC),
                "erasure_reason": reason,
            }
        )
        async with self._database.transaction() as connection:
            await connection.execute(
                "UPDATE node_audits SET record_json = ? WHERE audit_id = ?",
                (to_json_value(erased.model_dump(mode="json")), audit_id),
            )
        return await self.get(audit_id)

    async def list_erasable(
        self,
        tenant_id: str,
        older_than: datetime,
        *,
        exclude_run_ids: Sequence[str] | None = None,
    ) -> list[NodeAuditRecord]:
        """Return erasable (digest_version=2) records older than a cutoff.

        Scoped to one tenant and to records created before ``older_than``
        (compared as UTC isoformat, matching the write path). Legacy v1 records
        are excluded (un-erasable), as are records for any run in
        ``exclude_run_ids`` (legal-hold protected).
        """
        excluded = set(exclude_run_ids or ())
        cutoff = older_than.astimezone(UTC).isoformat()
        async with self._database.transaction() as connection:
            rows = await connection.fetch_all(
                "SELECT record_json FROM node_audits "
                "WHERE tenant_id = ? AND created_at < ? "
                "ORDER BY created_at, audit_id",
                (tenant_id, cutoff),
            )
        records = [
            NodeAuditRecord.model_validate(load_typed_value(row["record_json"], dict))
            for row in rows
        ]
        return [
            record
            for record in records
            if (record.digest_version or 1) >= 2 and record.run_id not in excluded
        ]

    async def _latest_for_run(self, connection, run_id: str) -> NodeAuditRecord | None:  # noqa: ANN001
        row = await connection.fetch_one(
            """
            SELECT record_json
            FROM node_audits
            WHERE run_id = ?
            ORDER BY created_at DESC, audit_id DESC
            LIMIT 1
            """,
            (run_id,),
        )
        if row is None:
            return None
        return NodeAuditRecord.model_validate(load_typed_value(row["record_json"], dict))
