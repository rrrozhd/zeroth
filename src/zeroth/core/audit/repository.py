"""Async database-backed storage for audit records.

Provides the AuditRepository class that handles saving and querying
NodeAuditRecord objects using an async database.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from zeroth.core.audit.coordination import (
    advance_audit_chain,
    hydrate_audit_row,
    load_ordered_run_records,
    lock_audit_chain,
)
from zeroth.core.audit.erasure_schema import (
    ERASED_PII_VALUES,
    LATEST_DIGEST_VERSION,
    pii_commitment_fields,
)
from zeroth.core.audit.models import AuditQuery, NodeAuditRecord
from zeroth.core.audit.verifier import _compute_pii_commitments, compute_chained_record
from zeroth.core.storage import AsyncConnection, AsyncDatabase
from zeroth.core.storage.json import to_json_value

if TYPE_CHECKING:
    from zeroth.core.signing import SigningKeyProvider


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

    async def write(self, record: NodeAuditRecord) -> NodeAuditRecord:
        """Save an audit record to the database.

        Writes are append-only. Duplicate audit IDs are rejected so history
        cannot be silently rewritten.
        """
        async with self._database.transaction(write_lock=True) as connection:
            head = await lock_audit_chain(
                connection,
                backend=self._database.backend,
                run_id=record.run_id,
            )
            # WS-E: stamp the latest commitment digest version BEFORE the
            # digest is computed, so the digest folds in the commitments and stays
            # identical after a later crypto-erasure nulls the plaintext. Always
            # populated for a commitment write — never left None (an empty
            # record would still "verify" while binding no PII).
            versioned = record.model_copy(update={"digest_version": LATEST_DIGEST_VERSION})
            prepared = versioned.model_copy(
                update={
                    "pii_commitments": _compute_pii_commitments(versioned),
                    "chain_sequence": head.next_sequence,
                }
            )
            chained = compute_chained_record(
                prepared,
                head.digest,
                self._signer,
            )
            existing = await connection.fetch_one(
                "SELECT 1 FROM node_audits WHERE audit_id = ?",
                (chained.audit_id,),
            )
            if existing is not None:
                raise ValueError(f"audit_id {record.audit_id!r} already exists")
            created_at = datetime.now(UTC)
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
                    chain_sequence,
                    record_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    chained.chain_sequence,
                    to_json_value(chained.model_dump(mode="json")),
                ),
            )
            if chained.record_digest is None:  # pragma: no cover - compute contract
                raise RuntimeError("audit record digest was not computed")
            await advance_audit_chain(
                connection,
                run_id=record.run_id,
                digest=chained.record_digest,
                next_sequence=head.next_sequence + 1,
            )
        return await self.get(record.audit_id)

    async def get(self, audit_id: str) -> NodeAuditRecord | None:
        """Look up a single audit record by its ID. Returns None if not found."""
        async with self._database.transaction() as connection:
            row = await connection.fetch_one(
                "SELECT record_json, chain_sequence FROM node_audits WHERE audit_id = ?",
                (audit_id,),
            )
        if row is None:
            return None
        return self._hydrate(row)

    async def list(self, query: AuditQuery | None = None) -> list[NodeAuditRecord]:
        """Return audit records matching the given filters, ordered by time.

        Pass an AuditQuery to filter by run, thread, node, etc. If no query
        is given, all records are returned.
        """
        query = query or AuditQuery()
        if query.run_id is not None:
            async with self._database.transaction() as connection:
                records = await load_ordered_run_records(connection, query.run_id)
            filter_fields = (
                "thread_id",
                "node_id",
                "graph_version_ref",
                "deployment_ref",
                "tenant_id",
            )
            return [
                record
                for record in records
                if all(
                    getattr(query, field) is None or getattr(record, field) == getattr(query, field)
                    for field in filter_fields
                )
            ]
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
        sql = "SELECT record_json, chain_sequence FROM node_audits"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, audit_id"
        async with self._database.transaction() as connection:
            rows = await connection.fetch_all(sql, tuple(params))
        return [self._hydrate(row) for row in rows]

    async def list_by_run(self, run_id: str) -> list[NodeAuditRecord]:
        """Return all audit records for a specific run."""
        return await self.list(AuditQuery(run_id=run_id))

    async def list_by_run_in_transaction(
        self,
        connection: AsyncConnection,
        run_id: str,
    ) -> list[NodeAuditRecord]:
        """Return a run's records using the caller's database transaction."""
        return await load_ordered_run_records(connection, run_id)

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
        async with self._database.transaction() as connection:
            return await self.crypto_erase_in_transaction(
                connection,
                audit_id,
                reason=reason,
            )

    async def crypto_erase_in_transaction(
        self,
        connection: AsyncConnection,
        audit_id: str,
        *,
        reason: str,
        record: NodeAuditRecord | None = None,
    ) -> NodeAuditRecord | None:
        """Crypto-erase one audit through an existing transaction."""
        if record is None:
            row = await connection.fetch_one(
                "SELECT record_json, chain_sequence FROM node_audits WHERE audit_id = ?",
                (audit_id,),
            )
            record = None if row is None else self._hydrate(row)
        if record is None:
            return None
        if record.audit_id != audit_id:
            raise ValueError(
                f"supplied record audit_id {record.audit_id!r} does not match {audit_id!r}"
            )
        if (record.digest_version or 1) < 2:
            raise ValueError(
                f"audit_id {audit_id!r} is digest_version=1 (legacy) and cannot be "
                "crypto-erased; legacy whole-payload digests are grandfathered"
            )
        expected_commitments = _compute_pii_commitments(record)
        expected_keys = set(pii_commitment_fields(record.digest_version))
        stored_commitments = record.pii_commitments or {}
        if set(stored_commitments) != expected_keys:
            raise ValueError(f"audit_id {audit_id!r} has invalid pii_commitments field schema")
        if not record.erased and stored_commitments != expected_commitments:
            raise ValueError(
                f"audit_id {audit_id!r} has pii_commitments that do not match plaintext"
            )
        if record.digest_version == 2 and (record.condition_results or record.approval_actions):
            raise ValueError(
                f"audit_id {audit_id!r} is digest_version=2 and contains structured "
                "PII that predates expanded commitments; it cannot be crypto-erased"
            )
        if record.erased:
            return record  # idempotent: already erased, commitments/digest intact
        erased = record.model_copy(
            update={
                **ERASED_PII_VALUES,
                "erased": True,
                "erased_at": datetime.now(UTC),
                "erasure_reason": reason,
            }
        )
        await connection.execute(
            "UPDATE node_audits SET record_json = ? WHERE audit_id = ?",
            (to_json_value(erased.model_dump(mode="json")), audit_id),
        )
        return erased

    async def list_erasable(
        self,
        tenant_id: str,
        older_than: datetime,
        *,
        exclude_run_ids: Sequence[str] | None = None,
    ) -> list[NodeAuditRecord]:
        """Return erasable commitment-digest records older than a cutoff.

        Scoped to one tenant and to records created before ``older_than``
        (compared as UTC isoformat, matching the write path). Legacy v1 records
        are excluded (un-erasable), as are records for any run in
        ``exclude_run_ids`` (legal-hold protected).
        """
        excluded = set(exclude_run_ids or ())
        cutoff = older_than.astimezone(UTC).isoformat()
        async with self._database.transaction() as connection:
            rows = await connection.fetch_all(
                "SELECT record_json, chain_sequence FROM node_audits "
                "WHERE tenant_id = ? AND created_at < ? "
                "ORDER BY created_at, audit_id",
                (tenant_id, cutoff),
            )
        records = [self._hydrate(row) for row in rows]
        return [
            record
            for record in records
            if (record.digest_version or 1) >= 2 and record.run_id not in excluded
        ]

    @staticmethod
    def _hydrate(row: dict[str, object]) -> NodeAuditRecord:
        return hydrate_audit_row(row)
